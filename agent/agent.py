import argparse
import os
import pathlib
import queue
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx
import psutil

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--node-id", default=socket.gethostname())
parser.add_argument("--port",    type=int, default=9000)
parser.add_argument("--label",   action="append", help="Node label in key=value format")
args = parser.parse_args()

CONTROLLER         = "http://localhost:8000"
HEARTBEAT_INTERVAL = 15  # seconds between heartbeat bursts while jobs are running

node = args.node_id

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED    = "failed"

# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _token_path() -> pathlib.Path:
    """Return the path to the persisted node token file."""
    d = pathlib.Path.home() / ".tcp"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"node-{node}.token"


def _load_token() -> str | None:
    """Read the persisted node token from disk, or return None if absent/empty."""
    p = _token_path()
    try:
        token = p.read_text().strip()
        return token if token else None
    except FileNotFoundError:
        return None


def _save_token(token: str):
    """Write the node token to disk with restricted permissions."""
    p = _token_path()
    p.write_text(token)
    p.chmod(0o600)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

# Completed jobs waiting to be posted back to the controller.
_results: queue.Queue = queue.Queue()

# Job IDs currently executing — used to send heartbeats.
_running_jobs: set     = set()
_running_lock          = threading.Lock()

# Per-job process handles for cancellation.
# Values are ("shell", Popen) or ("docker", container_name).
_processes:      dict  = {}
_processes_lock        = threading.Lock()

# Buffered log lines that could not be shipped due to controller unavailability.
# Entries are (job_id, line). Flushed at the start of each poll iteration.
_log_buffer:      list = []
_log_buffer_lock        = threading.Lock()

# Set after successful registration.
_node_token: str | None = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _headers() -> dict:
    """Return auth headers for requests to the controller."""
    if _node_token:
        return {"X-Node-Token": _node_token}
    return {}


def _handle_revocation():
    """Called when the controller rejects our token. Clears the token file, logs and exits."""
    try:
        _token_path().unlink(missing_ok=True)
    except Exception:
        pass
    print(f"[{node}] node token rejected by controller — this node has been revoked")
    print(f"[{node}] shutting down; restart the agent to re-register")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def parse_labels(label_args) -> dict:
    """Parse a list of 'key=value' strings into a dict."""
    labels = {}
    for label in label_args or []:
        k, _, v = label.partition("=")
        labels[k] = v
    return labels


def _do_register() -> str:
    """
    Perform registration with the controller using the bootstrap token.
    Returns the issued node token. Exits immediately if the bootstrap
    token is missing or rejected.
    """
    bootstrap_token = os.environ.get("TCP_BOOTSTRAP_TOKEN", "")
    if not bootstrap_token:
        print("[agent] TCP_BOOTSTRAP_TOKEN is not set, cannot register")
        sys.exit(1)

    r = httpx.post(
        f"{CONTROLLER}/register",
        json={
            "node":    node,
            "address": f"http://localhost:{args.port}",
            "labels":  parse_labels(args.label),
        },
        headers={"X-Bootstrap-Token": bootstrap_token},
    )
    if r.status_code == 401:
        print("[agent] registration rejected: invalid bootstrap token")
        sys.exit(1)

    token = r.json()["token"]
    _save_token(token)
    print(f"[agent] registered as {node}")
    return token


def register():
    """
    Set the node token before starting the poll loop.

    If a token file exists from a previous run, use it and skip registration.
    If the token is later rejected (401), fall back to re-registering.
    If no token file exists, register now and persist the issued token.
    """
    global _node_token
    saved = _load_token()
    if saved:
        _node_token = saved
        print(f"[agent] resuming as {node} using saved token")
    else:
        _node_token = _do_register()


# ---------------------------------------------------------------------------
# State reporting
# ---------------------------------------------------------------------------

def collect_state() -> dict:
    """Collect current node metrics to report to the controller."""
    disk = psutil.disk_usage("/")
    return {
        "node":      node,
        "cpu":       psutil.cpu_percent() / 100,
        "mem":       psutil.virtual_memory().percent / 100,
        "disk_free": disk.free / disk.total,
    }


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def run_docker(image: str, command: str, container_name: str = "") -> subprocess.Popen:
    """
    Start a Docker container and return the Popen handle immediately.
    The container name is set deterministically so kill_job can always
    stop it by name, without needing to read a cidfile.
    The caller is responsible for calling communicate() to wait for exit.
    """
    cmd = ["docker", "run", "--rm", "--network", "none"]
    if container_name:
        cmd += ["--name", container_name]
    cmd.append(image)
    if command:
        cmd += shlex.split(command)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    proc._container_name = container_name
    return proc


def execute(job: dict) -> tuple:
    """
    Execute a job and return (status, output).

    Docker jobs: run in an isolated container with no network access.
    Shell jobs:  run directly in a subprocess on the host.

    In both cases the process handle is stored in _processes before
    blocking on output, so kill_job can reach it while the job is running.
    """
    job_id = job["job"]
    try:
        if job.get("image"):
            container_name = f"tcp-{job_id[:16]}"
            proc = run_docker(job["image"], job["command"], container_name=container_name)

            # Register before waiting so kill_job can find the container.
            with _processes_lock:
                _processes[job_id] = ("docker", container_name)

            out, _ = proc.communicate()
            if proc.returncode != 0:
                return STATUS_FAILED, out.decode()
            return STATUS_SUCCEEDED, out.decode()

        else:
            proc = subprocess.Popen(
                job["command"],
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            with _processes_lock:
                _processes[job_id] = ("shell", proc)

            out, _ = proc.communicate()
            if proc.returncode != 0:
                return STATUS_FAILED, out.decode()
            return STATUS_SUCCEEDED, out.decode()

    except Exception as e:
        return STATUS_FAILED, str(e)


def kill_job(job_id: str):
    """
    Terminate a running job.
    Shell jobs receive SIGTERM, then SIGKILL after 5 seconds if still alive.
    Docker jobs are stopped with 'docker stop', which sends SIGTERM and waits
    for the container to exit before force-killing it.
    """
    with _processes_lock:
        entry = _processes.pop(job_id, None)

    if entry is None:
        return

    kind, handle = entry

    if kind == "shell":
        try:
            handle.terminate()
            try:
                handle.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handle.kill()
        except Exception as e:
            print(f"[{node}] failed to kill shell job {job_id}: {e}")

    elif kind == "docker":
        if handle:
            try:
                subprocess.run(["docker", "stop", handle], timeout=10)
            except Exception as e:
                print(f"[{node}] failed to stop container {handle}: {e}")

    print(f"[{node}] cancelled job {job_id}")


# ---------------------------------------------------------------------------
# Background job thread
# ---------------------------------------------------------------------------

def run_job_thread(job: dict):
    """
    Runs in a background thread per job.
    Executes the job, ships its output as logs, then queues the result
    for the main loop to post back to the controller.
    """
    job_id = job["job"]

    with _running_lock:
        _running_jobs.add(job_id)

    try:
        status, result = execute(job)
        send_logs(job_id, result)
        _results.put((job_id, status, result))
    finally:
        with _running_lock:
            _running_jobs.discard(job_id)
        with _processes_lock:
            _processes.pop(job_id, None)


# ---------------------------------------------------------------------------
# Controller communication
# ---------------------------------------------------------------------------

def send_logs(job_id: str, output: str):
    """
    Post each line of output to the controller's log endpoint.
    Lines that cannot be shipped (controller unreachable) are added to
    the in-memory buffer and retried by flush_log_buffer on the next
    poll iteration.
    """
    for line in output.splitlines():
        try:
            httpx.post(
                f"{CONTROLLER}/agent/log",
                json={"job": job_id, "line": line, "node": node},
                headers=_headers(),
                timeout=1,
            )
        except Exception:
            with _log_buffer_lock:
                _log_buffer.append((job_id, line))


def flush_log_buffer():
    """
    Attempt to ship any log lines that failed to send in a previous iteration.
    Lines are retried in order. On the first failure the remainder are kept
    buffered and the flush stops — preserving line order and avoiding a
    flood of requests against an unavailable controller.
    """
    with _log_buffer_lock:
        if not _log_buffer:
            return
        pending = list(_log_buffer)

    shipped = 0
    for job_id, line in pending:
        try:
            httpx.post(
                f"{CONTROLLER}/agent/log",
                json={"job": job_id, "line": line, "node": node},
                headers=_headers(),
                timeout=1,
            )
            shipped += 1
        except Exception:
            break

    if shipped:
        with _log_buffer_lock:
            del _log_buffer[:shipped]
        print(f"[{node}] flushed {shipped} buffered log line(s)")


def send_heartbeats():
    """Send a heartbeat to the controller for every currently running job."""
    with _running_lock:
        job_ids = list(_running_jobs)

    for job_id in job_ids:
        try:
            httpx.post(
                f"{CONTROLLER}/agent/heartbeat/{job_id}",
                json={"node": node},
                headers=_headers(),
                timeout=1,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def loop():
    """
    Poll the controller in a tight loop (1 second sleep):

    0. Flush any log lines buffered during a previous controller outage.
    1. Post node state. Re-register on 401 if token was loaded from file; exit on explicit revocation.
    2. Send heartbeats for running jobs on the configured interval.
    3. Check for cancellation requests and kill matching jobs.
    4. Drain the result queue and post completed job results.
    5. Pick up one new pending job and start it in a background thread.
    """
    global _node_token
    last_heartbeat = 0.0

    while True:
        try:
            # 0. Flush any log lines buffered during a previous controller outage
            flush_log_buffer()

            # 1. Report state
            r = httpx.post(f"{CONTROLLER}/state", json=collect_state(), headers=_headers())
            if r.status_code == 401:
                print(f"[{node}] saved token rejected — re-registering")
                _node_token = _do_register()
                continue

            # 2. Heartbeats
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_heartbeats()
                last_heartbeat = now

            # 3. Cancellations
            try:
                r = httpx.get(
                    f"{CONTROLLER}/agent/cancel/{node}",
                    headers=_headers(),
                    timeout=2,
                )
                for job_id in r.json().get("cancel", []):
                    print(f"[{node}] received cancel for job {job_id}")
                    kill_job(job_id)
            except Exception:
                pass

            # 4. Results
            while not _results.empty():
                job_id, status, result = _results.get_nowait()
                print(f"[{node}] job {job_id}: {status}")
                try:
                    httpx.post(
                        f"{CONTROLLER}/agent/result",
                        json={"job": job_id, "status": status, "result": result, "node": node},
                        headers=_headers(),
                    )
                except Exception as e:
                    print(f"[{node}] failed to post result for {job_id}: {e}")

            # 5. Pick up new job
            r = httpx.get(f"{CONTROLLER}/agent/jobs/{node}", headers=_headers())
            if r.status_code == 401:
                _handle_revocation()

            data = r.json()
            if "job" in data:
                job_id  = data["job"]
                image   = data.get("image")
                command = data.get("command")
                if image:
                    print(f"[{node}] starting job {job_id}: docker run {image} {command}")
                else:
                    print(f"[{node}] starting job {job_id}: shell: {command}")

                t = threading.Thread(target=run_job_thread, args=(data,), daemon=True)
                t.start()

        except Exception as e:
            print(f"[agent] error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    register()
    loop()


def _ack_cancel(controller, job_id, node_id, token):
    import requests
    try:
        requests.post(
            f"{controller}/agent/cancel/ack",
            params={"job_id": job_id, "node_id": node_id},
            headers={"X-Node-Token": token},
            timeout=5,
        )
    except Exception:
        pass
