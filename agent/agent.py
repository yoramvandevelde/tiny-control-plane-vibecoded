
import socket, psutil, httpx, subprocess, argparse, time

parser = argparse.ArgumentParser()
parser.add_argument("--node-id", default=socket.gethostname())
parser.add_argument("--port", type=int, default=9000)
args = parser.parse_args()

CONTROLLER = "http://localhost:8000"
node = args.node_id


def state():
    disk = psutil.disk_usage("/")
    return {
        "node": node,
        "cpu": psutil.cpu_percent()/100,
        "mem": psutil.virtual_memory().percent/100,
        "disk_free": disk.free/disk.total
    }


def register():
    httpx.post(
        f"{CONTROLLER}/register",
        json={"node": node, "address": f"http://localhost:{args.port}"}
    )


def loop():

    while True:

        try:

            httpx.post(f"{CONTROLLER}/state", json=state())

            r = httpx.get(f"{CONTROLLER}/agent/jobs/{node}")
            data = r.json()

            if "job" in data:

                out = subprocess.check_output(
                    data["command"],
                    shell=True,
                    stderr=subprocess.STDOUT
                ).decode()

                httpx.post(
                    f"{CONTROLLER}/agent/result",
                    json={
                        "job": data["job"],
                        "status": "finished",
                        "result": out
                    }
                )

        except Exception:
            pass

        time.sleep(2)


if __name__ == "__main__":
    register()
    loop()
