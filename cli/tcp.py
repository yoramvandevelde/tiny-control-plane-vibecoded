import os
import pathlib
import sys
import time
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

API     = "http://localhost:8000"
app     = typer.Typer(help="Tiny Control Plane — cluster management CLI.")
console = Console()

ACTIVE_STATUSES = {"pending", "running"}

STATUS_COLOUR = {
    "pending":   "yellow",
    "running":   "cyan",
    "succeeded": "green",
    "failed":    "red",
    "cancelled": "yellow",
    "lost":      "magenta",
}


# ---------------------------------------------------------------------------
# Operator token
#
# The CLI authenticates to the controller using a shared operator token.
# Resolution order:
#   1. ~/.tcp/operator.token  — written manually or by a future login command
#   2. TCP_OPERATOR_TOKEN     — environment variable fallback
#
# If neither is present the CLI exits immediately with a clear message.
# To rotate the token, update the file or env var — no CLI command needed.
# ---------------------------------------------------------------------------

def _operator_token_path() -> pathlib.Path:
    """Return the path to the persisted operator token file."""
    d = pathlib.Path.home() / ".tcp"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / "operator.token"


def _load_operator_token() -> str:
    """
    Load the operator token used to authenticate CLI requests to the controller.

    Checks ~/.tcp/operator.token first, then falls back to the TCP_OPERATOR_TOKEN
    environment variable. Exits with a clear error message if neither is present,
    mirroring how the agent handles a missing bootstrap token.
    """
    # Try the token file first.
    p = _operator_token_path()
    try:
        token = p.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass

    # Fall back to the environment variable.
    token = os.environ.get("TCP_OPERATOR_TOKEN", "").strip()
    if token:
        return token

    console.print(
        "[red]error:[/red] no operator token found.\n"
        "Set TCP_OPERATOR_TOKEN or write the token to ~/.tcp/operator.token"
    )
    sys.exit(1)


def _operator_headers() -> dict:
    """Return the auth header dict required by all operator endpoints."""
    return {"X-Operator-Token": _load_operator_token()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _coloured(status: str) -> str:
    colour = STATUS_COLOUR.get(status, "white")
    return f"[{colour}]{status}[/{colour}]"


def _build_status_table(job_list: list, now: float) -> Table:
    """Build the job status Rich table from a list of job dicts."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("job id",   style="dim", no_wrap=True)
    table.add_column("node",                  no_wrap=True)
    table.add_column("workload",              no_wrap=True)
    table.add_column("command",               no_wrap=True)
    table.add_column("status",                no_wrap=True)
    table.add_column("elapsed",  justify="right", no_wrap=True)

    for j in sorted(job_list, key=lambda x: x.get("created") or 0):
        status_val = j["status"]
        created    = j.get("created") or now
        updated    = j.get("updated") or now
        elapsed    = _fmt_elapsed(now - created) if status_val in ACTIVE_STATUSES \
                     else _fmt_elapsed(updated - created)

        table.add_row(
            j["id"][:8],
            j.get("node") or "-",
            j.get("workload_name") or "-",
            (j.get("command") or "")[:50],
            _coloured(status_val),
            elapsed,
        )

    return table


# ---------------------------------------------------------------------------
# Node commands
# ---------------------------------------------------------------------------

@app.command()
def nodes():
    """List all registered nodes and their current health state."""
    r = httpx.get(f"{API}/nodes", headers=_operator_headers())
    console.print(r.json())


@app.command()
def revoke(
    node_id: str = typer.Argument(..., help="ID of the node to revoke."),
):
    """
    Revoke a node's authentication token.

    The node will be rejected on its next poll and will shut down cleanly.
    The node record and its job history are preserved. Restart the agent
    to re-register the node with a fresh token.
    """
    r = httpx.delete(f"{API}/nodes/{node_id}", headers=_operator_headers())
    console.print(r.json())


def _build_topology(nodes_data: dict, job_list: list):
    """Build a cluster tree with nodes as branches and jobs as leaves."""

    by_node: dict = {node_id: [] for node_id in nodes_data}
    for j in job_list:
        if j["status"] in ACTIVE_STATUSES:
            nid = j.get("node")
            if nid and nid in by_node:
                by_node[nid].append(j)

    # Cluster summary
    total_jobs = sum(len(jobs) for jobs in by_node.values())
    cpu_values = [
        info.get("state", {}).get("cpu", 0)
        for info in nodes_data.values()
        if info.get("state")
    ]
    cpu_avg    = sum(cpu_values) / len(cpu_values) if cpu_values else 0.0
    # Build a single cluster tree with nodes as branches
    cluster = Tree(f"[bold]cluster[/bold]  [dim]nodes={len(nodes_data)}  jobs={total_jobs}  cpu_avg={cpu_avg:.2f}[/dim]")

    for node_id in sorted(nodes_data):
        info      = nodes_data[node_id]
        healthy   = info.get("healthy", False)
        health    = "[green]healthy[/green]" if healthy else "[red]unhealthy[/red]"
        state     = info.get("state", {})
        cpu       = state.get("cpu", 0)
        mem       = state.get("mem", 0)
        job_count = len(by_node.get(node_id, []))
        stats     = f"[dim]cpu={cpu:.2f} mem={mem:.2f} jobs={job_count}[/dim]"
        node_tree = cluster.add(f"[bold]{node_id}[/bold]  {health}  {stats}")

        active_jobs = by_node.get(node_id, [])
        if active_jobs:
            for j in active_jobs:
                workload = j.get("workload_name") or "exec"
                status   = _coloured(j["status"])
                label    = f"{j['id'][:8]}  [dim]{workload}[/dim]  {status}  [dim]{j.get('command', '')}[/dim]"
                node_tree.add(label)
        else:
            node_tree.add("[dim]no active jobs[/dim]")

    return cluster


@app.command()
def topology(
    follow: bool = typer.Option(False, "--follow", "-f", help="Continuously refresh the topology view."),
):
    """
    Show a tree view of the cluster: nodes with their active jobs grouped beneath them.

    Only PENDING and RUNNING jobs are shown. Completed jobs are omitted.
    Use --follow / -f to keep the view live, refreshing every second.
    """
    headers = _operator_headers()
    if follow:
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while True:
                nodes_data = httpx.get(f"{API}/nodes", headers=headers).json()
                job_list   = httpx.get(f"{API}/jobs",  headers=headers).json()
                live.update(_build_topology(nodes_data, job_list))
                time.sleep(1)
    else:
        nodes_data = httpx.get(f"{API}/nodes", headers=headers).json()
        job_list   = httpx.get(f"{API}/jobs",  headers=headers).json()
        console.print(_build_topology(nodes_data, job_list))


# ---------------------------------------------------------------------------
# Job commands
# ---------------------------------------------------------------------------

@app.command(name="exec")
def exec_cmd(
    node:    str            = typer.Argument(..., help="Node ID to run the job on."),
    command: str            = typer.Argument(..., help="Command to execute."),
    image:   Optional[str]  = typer.Option(None, help="Docker image to run the command in. Omit for a shell job."),
):
    """Submit a one-shot job to a specific node."""
    r = httpx.post(f"{API}/jobs", json={"node": node, "command": command, "image": image}, headers=_operator_headers())
    console.print(r.json())


@app.command()
def status(
    follow: bool = typer.Option(False, "--follow", "-f", help="Continuously refresh the status table."),
):
    """
    Show all jobs with their node, workload, status, and elapsed time.

    Use --follow / -f to keep the table live, refreshing every second.
    """
    headers = _operator_headers()
    if follow:
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while True:
                r        = httpx.get(f"{API}/jobs", headers=headers)
                now      = time.time()
                live.update(_build_status_table(r.json(), now))
                time.sleep(1)
    else:
        r   = httpx.get(f"{API}/jobs", headers=headers)
        now = time.time()
        console.print(_build_status_table(r.json(), now))


@app.command()
def jobs():
    """List all jobs as raw JSON."""
    r = httpx.get(f"{API}/jobs", headers=_operator_headers())
    console.print(r.json())


@app.command()
def logs(
    job:    str  = typer.Argument(..., help="Full job UUID to fetch logs for."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new log lines as they arrive (SSE)."),
):
    """
    Print the output of a job.

    Use --follow / -f to tail the log in real time. The stream closes
    automatically when the job finishes.
    """
    headers = _operator_headers()
    if follow:
        # Connect to the SSE stream and print lines as they arrive.
        with httpx.stream("GET", f"{API}/jobs/{job}/logs/stream", headers=headers, timeout=None) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    console.print(line[6:])
    else:
        r = httpx.get(f"{API}/jobs/{job}/logs", headers=headers)
        for entry in r.json():
            console.print(entry["line"])


# ---------------------------------------------------------------------------
# Workload commands
# ---------------------------------------------------------------------------

@app.command()
def deploy(
    name:       str            = typer.Argument(..., help="Unique name for the workload."),
    command:    str            = typer.Argument(..., help="Command each replica will run."),
    replicas:   int            = typer.Argument(..., help="Number of replicas to maintain."),
    image:      Optional[str]  = typer.Option(None,  help="Docker image to run the command in. Omit for shell jobs."),
    constraint: Optional[list[str]] = typer.Option(None, help="Node label constraint in key=value format. Repeatable."),
):
    """
    Deploy a workload — the reconciler will schedule and maintain the requested number of replicas.

    Example:
        tcp deploy workers 'python worker.py' 4 --image python:3.12 --constraint region=eu
    """
    constraints = {}
    for c in (constraint or []):
        k, _, v = c.partition("=")
        constraints[k] = v

    r = httpx.post(
        f"{API}/workloads",
        json={
            "name":        name,
            "command":     command,
            "replicas":    replicas,
            "image":       image,
            "constraints": constraints or None,
        },
        headers=_operator_headers(),
    )
    console.print(r.json())


@app.command()
def scale(
    name:     str = typer.Argument(..., help="Name of the workload to scale."),
    replicas: int = typer.Argument(..., help="New target replica count."),
):
    """
    Adjust the replica count of a running workload.

    Scaling down cancels excess jobs immediately.
    Scaling up schedules new jobs on the next reconciler pass.
    """
    r = httpx.post(f"{API}/workloads/{name}/scale", json={"replicas": replicas}, headers=_operator_headers())
    console.print(r.json())


@app.command()
def undeploy(
    name: str = typer.Argument(..., help="Name of the workload to remove."),
):
    """
    Remove a workload and cancel all its active jobs.

    The workload definition is deleted immediately. Running containers
    will receive a stop signal within one agent poll cycle.
    """
    r = httpx.delete(f"{API}/workloads/{name}", headers=_operator_headers())
    console.print(r.json())


@app.command()
def events(
    follow: bool = typer.Option(False, "--follow", "-f", help="Tail new events as they occur (SSE)."),
):
    """
    Show recent cluster events: node registrations, workload changes, and job lifecycle transitions.

    Use --follow / -f to stream events in real time.
    """
    EVENT_COLOUR = {
        "node.registered":  "cyan",
        "node.revoked":     "red",
        "workload.created": "green",
        "workload.scaled":  "yellow",
        "workload.deleting":"yellow",
        "workload.removed": "red",
        "job.scheduled":    "dim",
        "job.started":      "cyan",
        "job.succeeded":    "green",
        "job.failed":       "red",
        "job.cancelled":    "yellow",
        "job.lost":         "magenta",
    }

    def _fmt_event(kind: str, message: str, ts: float = None) -> str:
        import datetime
        colour    = EVENT_COLOUR.get(kind, "white")
        time_str  = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "        "
        return f"[dim]{time_str}[/dim]  [{colour}]{kind:<20}[/{colour}]  {message}"

    headers = _operator_headers()
    if follow:
        with httpx.stream("GET", f"{API}/events/stream", headers=headers, timeout=None) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    raw    = line[6:]
                    # format: "ts|kind  message"
                    if "|" in raw:
                        ts_str, rest = raw.split("|", 1)
                        parts        = rest.split("  ", 1)
                        kind         = parts[0] if parts else rest
                        message      = parts[1] if len(parts) > 1 else ""
                        console.print(_fmt_event(kind.strip(), message, float(ts_str)))
                    else:
                        parts   = raw.split("  ", 1)
                        kind    = parts[0] if parts else raw
                        message = parts[1] if len(parts) > 1 else ""
                        console.print(_fmt_event(kind.strip(), message))
    else:
        r = httpx.get(f"{API}/events", headers=headers)
        for entry in r.json():
            console.print(_fmt_event(entry["kind"], entry["message"], entry.get("ts")))


if __name__ == "__main__":
    app()
