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

# ---------------------------------------------------------------------------
# Controller address
#
# The CLI resolves the controller address in this order:
#   1. TCP_CONTROLLER environment variable
#   2. http://localhost:8000 (development default)
#
# When running the CLI on a machine other than the controller, set:
#   export TCP_CONTROLLER=http://10.0.0.5:8000
# ---------------------------------------------------------------------------

API = os.environ.get("TCP_CONTROLLER", "http://localhost:8000")

app          = typer.Typer(help="Tiny Control Plane — cluster management CLI.")
describe_app = typer.Typer(help="Inspect cluster objects in detail.")
app.add_typer(describe_app, name="describe")
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
    data = r.json()

    table = Table(show_header=True, header_style="bold")
    table.add_column("node",    no_wrap=True)
    table.add_column("version", no_wrap=True, style="dim")
    table.add_column("health",  no_wrap=True)
    table.add_column("cpu",     justify="right", no_wrap=True)
    table.add_column("mem",     justify="right", no_wrap=True)
    table.add_column("capacity", justify="right", no_wrap=True)
    table.add_column("address", no_wrap=True, style="dim")

    for node_id in sorted(data):
        info    = data[node_id]
        healthy = info.get("healthy", False)
        health  = "[green]healthy[/green]" if healthy else "[red]unhealthy[/red]"
        state   = info.get("state", {})
        cpu     = f"{state.get('cpu', 0):.0%}" if state else "-"
        mem     = f"{state.get('mem', 0):.0%}" if state else "-"
        version   = info.get("version") or "[dim]unknown[/dim]"
        total_cpu = info.get("total_cpu", 1)
        total_mem = info.get("total_mem_mb", 512)
        capacity  = f"{total_cpu} CPU / {total_mem} MiB"
        address   = info.get("address") or "-"

        table.add_row(node_id, version, health, cpu, mem, capacity, address)

    console.print(table)


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
        version   = info.get("version") or "unknown"
        stats     = f"[dim]v{version}  cpu={cpu:.2f} mem={mem:.2f} jobs={job_count}[/dim]"
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
    node:    str = typer.Argument(..., help="Node ID to run the job on."),
    command: str = typer.Argument(..., help="Command to execute."),
    image:   str = typer.Option(...,  help="Docker image to run the command in."),
):
    """
    Submit a one-shot job to a specific node.

    All jobs run inside a Docker container. --image is required.

    Example:
        tcp exec node1 'echo hello' --image alpine
    """
    r = httpx.post(
        f"{API}/jobs",
        json={"node": node, "command": command, "image": image},
        headers=_operator_headers(),
    )
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
def workloads():
    """List all workloads with their replica counts and image."""
    r = httpx.get(f"{API}/workloads", headers=_operator_headers())
    r.raise_for_status()
    data = r.json()

    if not data:
        console.print("[dim]no workloads[/dim]")
        return

    # Fetch jobs to compute live replica counts.
    jobs_r = httpx.get(f"{API}/jobs", headers=_operator_headers())
    job_list = jobs_r.json()

    # Count active jobs per workload.
    active_by_workload: dict[str, int] = {}
    for j in job_list:
        if j.get("status") in ACTIVE_STATUSES and j.get("workload_name"):
            active_by_workload[j["workload_name"]] = active_by_workload.get(j["workload_name"], 0) + 1

    table = Table(show_header=True, header_style="bold")
    table.add_column("name",         no_wrap=True)
    table.add_column("image",        no_wrap=True, style="dim")
    table.add_column("command",      no_wrap=True)
    table.add_column("desired",      justify="right", no_wrap=True)
    table.add_column("running",      justify="right", no_wrap=True)
    table.add_column("max-attempts", justify="right", no_wrap=True)
    table.add_column("constraints",  no_wrap=True, style="dim")

    for w in sorted(data, key=lambda x: x["name"]):
        desired      = w["replicas"]
        running      = active_by_workload.get(w["name"], 0)
        max_attempts = w.get("max_attempts", 3)
        rep_str      = f"[green]{running}[/green]" if running >= desired else f"[yellow]{running}[/yellow]"
        constraints  = ", ".join(f"{k}={v}" for k, v in (w.get("constraints") or {}).items()) or "-"
        att_str      = "[dim]unlimited[/dim]" if max_attempts == 0 else str(max_attempts)

        table.add_row(
            w["name"],
            w.get("image") or "-",
            (w.get("command") or "")[:50],
            str(desired),
            rep_str,
            att_str,
            constraints,
        )

    console.print(table)


@describe_app.command("job")
def describe_job(
    job_id: str = typer.Argument(..., help="Job ID or prefix."),
):
    """Show detailed information about a job."""
    headers  = _operator_headers()
    job_list = httpx.get(f"{API}/jobs", headers=headers).json()

    matches = [j for j in job_list if j["id"].startswith(job_id)]
    if not matches:
        console.print(f"[red]error:[/red] no job found matching '{job_id}'")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(f"[yellow]ambiguous prefix '{job_id}' — matches {len(matches)} jobs:[/yellow]")
        for j in matches:
            console.print(f"  {j['id']}")
        raise typer.Exit(1)

    j       = matches[0]
    now     = time.time()
    created = j.get("created") or now
    updated = j.get("updated") or now
    status  = j["status"]
    elapsed = _fmt_elapsed(now - created) if status in ACTIVE_STATUSES else _fmt_elapsed(updated - created)

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("field", style="bold", no_wrap=True)
    table.add_column("value")

    table.add_row("id",       j["id"])
    table.add_row("status",   _coloured(status))
    table.add_row("node",     j.get("node") or "-")
    table.add_row("workload", j.get("workload_name") or "-")
    table.add_row("image",    j.get("image") or "-")
    table.add_row("command",  j.get("command") or "-")
    table.add_row("elapsed",  elapsed)
    if j.get("result"):
        table.add_row("result", j["result"])
    req_cpu = j.get("req_cpu") or 0
    req_mem = j.get("req_mem_mb") or 0
    if req_cpu or req_mem:
        table.add_row("cpu request", str(req_cpu))
        table.add_row("mem request", f"{req_mem} MiB")

    console.print(table)

    logs_r    = httpx.get(f"{API}/jobs/{j['id']}/logs", headers=headers)
    log_lines = logs_r.json()
    if log_lines:
        console.print(f"\n[bold]logs[/bold] ({len(log_lines)} lines):")
        for entry in log_lines[-10:]:
            console.print(f"  {entry['line']}")
        if len(log_lines) > 10:
            console.print(f"  [dim]… {len(log_lines) - 10} earlier lines — use `tcp logs {j['id'][:8]}` to see all[/dim]")


@describe_app.command("node")
def describe_node(
    node_id: str = typer.Argument(..., help="Node ID."),
):
    """Show detailed information about a node."""
    headers    = _operator_headers()
    nodes_data = httpx.get(f"{API}/nodes", headers=headers).json()

    if node_id not in nodes_data:
        console.print(f"[red]error:[/red] node '{node_id}' not found")
        raise typer.Exit(1)

    info    = nodes_data[node_id]
    healthy = info.get("healthy", False)
    state   = info.get("state") or {}
    labels  = info.get("labels") or {}
    last    = info.get("last_seen")
    ago     = _fmt_elapsed(time.time() - last) + " ago" if last else "-"

    job_list  = httpx.get(f"{API}/jobs", headers=headers).json()
    node_jobs = [j for j in job_list if j.get("node") == node_id]
    active    = [j for j in node_jobs if j["status"] in ACTIVE_STATUSES]

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("field", style="bold", no_wrap=True)
    table.add_column("value")

    table.add_row("node",      node_id)
    table.add_row("healthy",   "[green]yes[/green]" if healthy else "[red]no[/red]")
    table.add_row("version",   info.get("version") or "-")
    table.add_row("address",   info.get("address") or "-")
    table.add_row("last seen", ago)
    table.add_row("capacity",  f"{info.get('total_cpu', 1)} CPU / {info.get('total_mem_mb', 512)} MiB")
    if state:
        table.add_row("cpu usage", f"{state.get('cpu', 0):.0%}")
        table.add_row("mem usage", f"{state.get('mem', 0):.0%}")
    table.add_row("jobs",      f"{len(active)} active / {len(node_jobs)} total")
    if labels:
        table.add_row("labels", ", ".join(f"{k}={v}" for k, v in labels.items()))

    console.print(table)

    if active:
        console.print()
        console.print(_build_status_table(active, time.time()))


@describe_app.command("workload")
def describe_workload(
    name: str = typer.Argument(..., help="Workload name."),
):
    """Show detailed information about a workload."""
    headers       = _operator_headers()
    workload_list = httpx.get(f"{API}/workloads", headers=headers).json()

    w = next((x for x in workload_list if x["name"] == name), None)
    if not w:
        console.print(f"[red]error:[/red] workload '{name}' not found")
        raise typer.Exit(1)

    job_list = httpx.get(f"{API}/jobs", headers=headers).json()
    wjobs    = [j for j in job_list if j.get("workload_name") == name]
    active   = [j for j in wjobs if j["status"] in ACTIVE_STATUSES]
    constraints = w.get("constraints") or {}

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("field", style="bold", no_wrap=True)
    table.add_column("value")

    table.add_row("name",        w["name"])
    table.add_row("image",       w.get("image") or "-")
    table.add_row("command",     w.get("command") or "-")
    max_att = w.get("max_attempts", 3)
    att_str = "unlimited" if max_att == 0 else str(max_att)
    table.add_row("replicas",     f"{len(active)}/{w['replicas']}")
    table.add_row("max-attempts", att_str)
    table.add_row("cpu request",  str(w.get("req_cpu") or 0))
    table.add_row("mem request",  f"{w.get('req_mem_mb') or 0} MiB")
    table.add_row("constraints",  ", ".join(f"{k}={v}" for k, v in constraints.items()) or "-")

    console.print(table)

    if wjobs:
        console.print()
        console.print(_build_status_table(wjobs, time.time()))


@app.command()
def deploy(
    name:         str                 = typer.Argument(..., help="Unique name for the workload."),
    command:      str                 = typer.Argument(..., help="Command each replica will run."),
    replicas:     int                 = typer.Argument(..., help="Number of replicas to maintain."),
    image:        str                 = typer.Option(...,   help="Docker image to run the command in."),
    constraint:   Optional[list[str]] = typer.Option(None, help="Node label constraint in key=value format. Repeatable."),
    max_attempts: int                 = typer.Option(0,     help="Max scheduling attempts per replica before the workload is considered exhausted. Defaults to unlimited (0). Set to a positive integer to cap retries."),
):
    """
    Deploy a workload — the reconciler will schedule and maintain the requested number of replicas.

    All replicas run inside Docker containers. --image is required.

    --max-attempts caps how many times the reconciler will reschedule a
    failing replica. Omit the flag (or pass 0) for unlimited retries.

    Example:
        tcp deploy workers 'python worker.py' 4 --image python:3.12 --constraint region=eu
        tcp deploy workers 'python worker.py' 4 --image python:3.12 --max-attempts 0
    """
    constraints = {}
    for c in (constraint or []):
        k, _, v = c.partition("=")
        constraints[k] = v

    r = httpx.post(
        f"{API}/workloads",
        json={
            "name":         name,
            "command":      command,
            "replicas":     replicas,
            "image":        image,
            "constraints":  constraints or None,
            "max_attempts": max_attempts,
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


# =============================================================================
# PATCH — cli/tcp.py
#
# Paste the gc() command below before the final:
#   if __name__ == "__main__":
#       app()
# =============================================================================


@app.command()
def gc(
    days:    float = typer.Option(7.0,   "--days",    help="Delete rows older than this many days."),
    dry_run: bool  = typer.Option(False, "--dry-run", help="Preview what would be deleted without removing anything."),
):
    """
    Garbage-collect old terminal jobs and events from the database.

    Removes jobs (succeeded/failed/cancelled/lost) and events older than
    --days days. Log lines and cancel signal entries for pruned jobs are
    removed automatically.

    Use --dry-run to see what would be deleted without making any changes.
    """
    if dry_run:
        r = httpx.get(
            f"{API}/gc/preview",
            params={"days": days},
            headers=_operator_headers(),
        )
        r.raise_for_status()
        data = r.json()

        table = Table(show_header=True, header_style="bold", title=f"GC preview — older than {data['days']} days")
        table.add_column("table",          no_wrap=True)
        table.add_column("rows to delete", justify="right", no_wrap=True)
        table.add_row("jobs (+ logs, cancel_jobs)", str(data["jobs"]))
        table.add_row("events",                     str(data["events"]))
        console.print(table)
        console.print("[dim]dry run — nothing deleted[/dim]")
    else:
        r = httpx.post(
            f"{API}/gc",
            json={"days": days},
            headers=_operator_headers(),
        )
        r.raise_for_status()
        data = r.json()

        table = Table(show_header=True, header_style="bold", title=f"GC complete — older than {data['days']} days")
        table.add_column("table",        no_wrap=True)
        table.add_column("rows deleted", justify="right", no_wrap=True)
        table.add_row("jobs (+ logs, cancel_jobs)", str(data["jobs"]))
        table.add_row("events",                     str(data["events"]))
        console.print(table)


if __name__ == "__main__":
    app()
