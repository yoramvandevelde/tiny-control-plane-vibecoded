import socket, psutil, httpx, subprocess, argparse, time, threading, queue, os, sys, signal, shlex

parser = argparse.ArgumentParser()
parser.add_argument("--node-id", default=socket.gethostname())
parser.add_argument("--port", type=int, default=9000)
parser.add_argument("--label", action="append", help="Node labels: key=value")
args = parser.parse_args()

CONTROLLER = "http://localhost:8000"
HEARTBEAT_INTERVAL = 15  # seconds between heartbeats while a job is running

node = args.node_id

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED    = "failed"

# Queue for completed jobs: (job_id, status, result)
_results: queue.Queue = queue.Queue()

# Set of job_ids currently being heartbeated
_running_jobs: set = set()
_running_lock = threading.Lock()

# Set after successful registration
_node_token: str | None = None

# Set to True if the controller rejects our token — triggers clean exit
_revoked = False


def _handle_revocation():
    print(f"[{node}] node token rejected by controller — this node has been revoked")
    print(f"[{node}] shutting down; restart the agent to re-register")
    sys.exit(1)

# Map of job_id -> subprocess.Popen (shell) or container_id str (docker)
_processes: dict = {}
_processes_lock = threading.Lock()


def _headers() -> dict:
    if _node_token:
        return {"X-Node-Token": _node_token}
    return {}


def parse_labels(label_args):
    labels = {}
    for l in label_args or []:
        k, _, v = l.partition("=")
        labels[k] = v
    return labels


def state():
    disk = psutil.disk_usage("/")
    return {
        "node": node,
        "cpu": psutil.cpu_percent() / 100,
        "mem": psutil.virtual_memory().percent / 100,
        "disk_free": disk.free / disk.total,
    }


def register():
    global _node_token
    bootstrap_token = os.environ.get("TCP_BOOTSTRAP_TOKEN", "")
    if not bootstrap_token:
        print("[agent] TCP_BOOTSTRAP_TOKEN is not set, cannot register")
        sys.exit(1)

    r = httpx.post(
        f"{CONTROLLER}/register",
        json={
            "node": node,
            "address": f"http://localhost:{args.port}",
            "labels": parse_labels(args.label),
        },
        headers={"X-Bootstrap-Token": bootstrap_token},
    )
    if r.status_code == 401:
        print("[agent] registration rejected: invalid bootstrap token")
        sys.exit(1)

    _node_token = r.json()["token"]
    print(f"[agent] registered as {node}")


def run_docker(image: str, command: str, container_name: str = "") -> subprocess.Popen:
    """Start a docker container and return the Popen handle. Caller must wait()."""
    cmd = ["docker", "run", "--rm", "--network", "none"]
    if container_name:
        cmd += ["--name", container_name]
    cmd.append(image)
    if command:
        cmd += shlex.split(command)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    proc._container_name = container_name
    return proc


def execute(job: dict) -> tuple[str, str]:
    job_id = job["job"]
    try:
        if job.get("image"):
            # Use a deterministic container name so kill_job can always find it
            container_name = f"tcp-{job_id[:16]}"
            proc = run_docker(job["image"], job["command"], container_name=container_name)

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


def send_logs(job_id: str, output: str):
    for line in output.splitlines():
        try:
            httpx.post(
                f"{CONTROLLER}/agent/log",
                json={"job": job_id, "line": line, "node": node},
                headers=_headers(),
                timeout=1,
            )
        except Exception:
            pass


def run_job_thread(job: dict):
    """
    Runs in a background thread. Executes the job, sends logs, then puts
    the result on the queue for the main loop to post back to the controller.
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


def send_heartbeats():
    """Send a heartbeat for every job currently running."""
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


def kill_job(job_id: str):
    """Terminate a running job by job_id. SIGTERM first, SIGKILL if still alive."""
    with _processes_lock:
        entry = _processes.pop(job_id, None)

    if entry is None:
        return

    kind, handle = entry

    if kind == "shell":
        proc = handle
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:
            print(f"[{node}] failed to kill shell job {job_id}: {e}")

    elif kind == "docker":
        container_id = handle
        if container_id:
            try:
                subprocess.run(["docker", "stop", container_id], timeout=10)
            except Exception as e:
                print(f"[{node}] failed to stop container {container_id}: {e}")

    print(f"[{node}] cancelled job {job_id}")


def loop():
    last_heartbeat = 0.0

    while True:
        try:
            r = httpx.post(
                f"{CONTROLLER}/state",
                json=state(),
                headers=_headers(),
            )
            if r.status_code == 401:
                _handle_revocation()

            # Send heartbeats on interval
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_heartbeats()
                last_heartbeat = now

            # Check for cancellation requests
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

            # Drain completed jobs and post results
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

            # Pick up a new pending job (one per poll cycle is fine)
            r = httpx.get(
                f"{CONTROLLER}/agent/jobs/{node}",
                headers=_headers(),
            )
            if r.status_code == 401:
                _handle_revocation()

            data = r.json()

            if "job" in data:
                job_id = data["job"]
                image = data.get("image")
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
