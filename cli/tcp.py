
import typer, httpx, time
from rich import print

API = "http://localhost:8000"
app = typer.Typer()


@app.command()
def nodes():
    r = httpx.get(f"{API}/nodes")
    print(r.json())


@app.command()
def watch():
    while True:
        nodes()
        time.sleep(2)


@app.command()
def exec(node: str, command: str):
    r = httpx.post(f"{API}/jobs", json={"node": node, "command": command})
    print(r.json())


@app.command()
def jobs():
    r = httpx.get(f"{API}/jobs")
    print(r.json())


@app.command()
def deploy(name: str, command: str, replicas: int):
    r = httpx.post(
        f"{API}/workloads",
        json={"name": name, "command": command, "replicas": replicas}
    )
    print(r.json())


if __name__ == "__main__":
    app()
