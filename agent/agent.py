import socket, psutil, httpx, subprocess, argparse, time

parser = argparse.ArgumentParser()
parser.add_argument("--node-id", default=socket.gethostname())
parser.add_argument("--port", type=int, default=9000)
args = parser.parse_args()

CONTROLLER = "http://localhost:8000"
node = args.node_id

# Mirror the controller's status strings so we speak the same language
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED    = "failed"


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
        json={"node": node, "address": f"http://localhost:{args.port}"}
    )


def run_shell(command: str) -> str:
    return subprocess.check_output(
        command,
        shell=True,
        stderr=subprocess.STDOUT,
    ).decode()


def run_docker(image: str, command: str) -> str:
    """
    Run a container to completion and return its stdout+stderr.
    --rm cleans up automatically.
    No network, no privilege -- keeps it simple and safe-ish.
    """
    cmd = ["docker", "run", "--rm", "--network", "none", image]
    if command:
        cmd += command.split()

    return subprocess.check_output(
        cmd,
        stderr=subprocess.STDOUT,
    ).decode()


def execute(job: dict) -> tuple[str, str]:
    """
    Returns (status, output).
    Uses Docker if an image is specified, shell otherwise.
    """
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
                timeout=2,
            )
        except Exception:
            pass


def loop():
    while True:
        try:
            httpx.post(f"{CONTROLLER}/state", json=state())

            r = httpx.get(f"{CONTROLLER}/agent/jobs/{node}")
            data = r.json()

            if "job" in data:
                job_id = data["job"]
                image = data.get("image")
                command = data.get("command")

                if image:
                    print(f"[{node}] running job {job_id}: docker run {image} {command}")
                else:
                    print(f"[{node}] running job {job_id}: shell: {command}")

                status, result = execute(data)

                send_logs(job_id, result)

                print(f"[{node}] job {job_id}: {status}")

                httpx.post(
                    f"{CONTROLLER}/agent/result",
                    json={
                        "job": data["job"],
                        "status": status,
                        "result": result,
                    }
                )

        except Exception as e:
            print(f"[agent] error: {e}")

        time.sleep(2)


if __name__ == "__main__":
    register()
    loop()
