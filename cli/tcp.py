
import typer, httpx, time
from rich import print
from typing import Optional

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
def exec(node: str, command: str, image: Optional[str] = typer.Option(None, help="Docker image")):
    r = httpx.post(f"{API}/jobs", json={"node": node, "command": command, "image": image})
    print(r.json())


@app.command()
def jobs():
    r = httpx.get(f"{API}/jobs")
    print(r.json())


@app.command()
def deploy(
    name: str,
    command: str,
    replicas: int,
    image: Optional[str] = typer.Option(None, help="Docker image to run"),
    constraint: Optional[list[str]] = typer.Option(None, help="Label constraints: key=value"),
):
    constraints = {}
    for c in (constraint or []):
        k, _, v = c.partition("=")
        constraints[k] = v

    r = httpx.post(
        f"{API}/workloads",
        json={
            "name": name,
            "command": command,
            "replicas": replicas,
            "image": image,
            "constraints": constraints or None,
        }
    )
    print(r.json())


if __name__ == "__main__":
    app()
