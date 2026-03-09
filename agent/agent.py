import socket, psutil, httpx, subprocess, argparse, time, threading, queue

parser = argparse.ArgumentParser()
parser.add_argument("--node-id", default=socket.gethostname())
parser.add_argument("--port", type=int, default=9000)
parser.add_argument("--label",action="append",help="Node labels: key=value")
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
    httpx.post(
        f"{CONTROLLER}/register",
        json={
            "node": node,
            "address": f"http://localhost:{args.port}",
            "labels": parse_labels(args.label),
        }
    )

def run_shell(command: str) -> str:
    return subprocess.check_output(
        command,
        shell=True,
        stderr=subprocess.STDOUT,
    ).decode()


def run_docker(image: str, command: str) -> str:
    cmd = ["docker", "run", "--rm", "--network", "none", image]
    if command:
        cmd += command.split()

    return subprocess.check_output(
        cmd,
        stderr=subprocess.STDOUT,
    ).decode()


def execute(job: dict) -> tuple[str, str]:
    try:
        if job.get("image"):
            out = run_docker(job["image"], job["command"])
        else:
            out = run_shell(job["command"])
        return STATUS_SUCCEEDED, out
    except subprocess.CalledProcessError as e:
        return STATUS_FAILED, e.output.decode() if e.output else str(e)


def send_logs(job_id: str, output: str):
    for line in output.splitlines():
        try:
            httpx.post(
                f"{CONTROLLER}/agent/log",
                json={"job": job_id, "line": line},
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


def send_heartbeats():
    """Send a heartbeat for every job currently running."""
    with _running_lock:
        job_ids = list(_running_jobs)

    for job_id in job_ids:
        try:
            httpx.post(f"{CONTROLLER}/agent/heartbeat/{job_id}", timeout=1)
        except Exception:
            pass


def loop():
    last_heartbeat = 0.0

    while True:
        try:
            httpx.post(f"{CONTROLLER}/state", json=state())

            # Send heartbeats on interval
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_heartbeats()
                last_heartbeat = now

            # Drain completed jobs and post results
            while not _results.empty():
                job_id, status, result = _results.get_nowait()
                print(f"[{node}] job {job_id}: {status}")
                try:
                    httpx.post(
                        f"{CONTROLLER}/agent/result",
                        json={"job": job_id, "status": status, "result": result},
                    )
                except Exception as e:
                    print(f"[{node}] failed to post result for {job_id}: {e}")

            # Pick up a new pending job (one per poll cycle is fine)
            r = httpx.get(f"{CONTROLLER}/agent/jobs/{node}")
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
