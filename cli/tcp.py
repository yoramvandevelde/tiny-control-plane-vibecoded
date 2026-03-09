
import typer, httpx, time
from rich import print
from rich.table import Table
from rich.console import Console
from typing import Optional

API = "http://localhost:8000"
app = typer.Typer()
console = Console()

ACTIVE_STATUSES = {"pending", "running"}

STATUS_COLOUR = {
    "pending":   "yellow",
    "running":   "cyan",
    "succeeded": "green",
    "failed":    "red",
    "lost":      "magenta",
}


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


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
def status():
    """Show all jobs with node, status, elapsed time, and workload."""
    r = httpx.get(f"{API}/jobs")
    job_list = r.json()

    now = time.time()

    table = Table(show_header=True, header_style="bold")
    table.add_column("job id",    style="dim",  no_wrap=True)
    table.add_column("node",                    no_wrap=True)
    table.add_column("workload",                no_wrap=True)
    table.add_column("command",                 no_wrap=True)
    table.add_column("status",                  no_wrap=True)
    table.add_column("elapsed",   justify="right", no_wrap=True)

    for j in sorted(job_list, key=lambda x: x.get("created") or 0):
        status_val = j["status"]
        colour = STATUS_COLOUR.get(status_val, "white")

        # For active jobs show time since creation; for terminal jobs show
        # how long they ran (created → updated)
        created = j.get("created") or now
        updated = j.get("updated") or now
        if status_val in ACTIVE_STATUSES:
            elapsed = _fmt_elapsed(now - created)
        else:
            elapsed = _fmt_elapsed(updated - created)

        table.add_row(
            j["id"][:8],
            j.get("node") or "-",
            j.get("workload_name") or "-",
            (j.get("command") or "")[:30],
            f"[{colour}]{status_val}[/{colour}]",
            elapsed,
        )

    console.print(table)


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


@app.command()
def scale(name: str, replicas: int):
    r = httpx.post(f"{API}/workloads/{name}/scale", json={"replicas": replicas})
    print(r.json())


@app.command()
def undeploy(name: str):
    r = httpx.delete(f"{API}/workloads/{name}")
    print(r.json())

@app.command()
def logs(job: str):
    r = httpx.get(f"{API}/jobs/{job}/logs")
    for line in r.json():
        print(line["line"])


if __name__ == "__main__":
    app()
