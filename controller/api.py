import asyncio
import os
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from sse_starlette.sse import EventSourceResponse

from controller.store import (
    JobStatus,
    count_active_node_jobs,
    count_active_workload_jobs,
    create_job,
    create_workload,
    delete_workload,
    enqueue_cancel,
    expire_lost_jobs,
    finish_job,
    get_events_since,
    get_excess_workload_jobs,
    get_logs,
    get_pending_job,
    init_db,
    list_events,
    list_jobs,
    list_nodes,
    list_workloads,
    mark_cancelled,
    mark_lost,
    get_pending_cancels,
    ack_cancel,
    record_event,
    register_node,
    renew_lease,
    revoke_node,
    start_job,
    store_log,
    update_state,
    update_workload_replicas,
    verify_node_token,
)

# A node is considered stale if it has not reported state within this window.
NODE_STALE_SECONDS = 30

# Lease expiry is suppressed for this many seconds after controller startup,
# so a brief restart does not incorrectly mark running jobs as lost.
STARTUP_GRACE_SECONDS = 60

_startup_time: float = 0.0

# Terminal statuses at which a log stream should close.
_STREAM_TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.LOST, JobStatus.CANCELLED}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_bootstrap_token() -> str:
    """Read the bootstrap token from the environment. Raises if not set."""
    token = os.environ.get("TCP_BOOTSTRAP_TOKEN", "")
    if not token:
        raise RuntimeError("TCP_BOOTSTRAP_TOKEN environment variable is not set")
    return token


def get_operator_token() -> str:
    """Read the operator token from the environment. Raises if not set."""
    token = os.environ.get("TCP_OPERATOR_TOKEN", "")
    if not token:
        raise RuntimeError("TCP_OPERATOR_TOKEN environment variable is not set")
    return token


def require_agent_auth(node_id: str, x_node_token: str | None):
    """Raise HTTP 401 if the per-node token is missing or invalid."""
    if not x_node_token or not verify_node_token(node_id, x_node_token):
        raise HTTPException(status_code=401, detail="invalid or missing node token")


def require_operator_auth(x_operator_token: str | None):
    """Raise HTTP 401 if the operator token is missing or does not match."""
    if not x_operator_token or x_operator_token != get_operator_token():
        raise HTTPException(status_code=401, detail="invalid or missing operator token")


def require_image(image: str | None):
    """
    Raise HTTP 422 if no Docker image was provided.
    All jobs and workloads must run inside a container — bare host
    execution is not permitted.
    """
    if not image:
        raise HTTPException(
            status_code=422,
            detail="image is required: all jobs must run inside a Docker container",
        )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def pick_node(nodes: dict, constraints: dict, resources: dict) -> str | None:
    """
    Select the best node for a new job using a scored candidate list:

    1. Exclude unhealthy nodes and nodes not seen within NODE_STALE_SECONDS.
    2. Exclude nodes whose labels do not satisfy the workload constraints.
    3. Score by (active job count, CPU usage) — lowest score wins.
    4. Shuffle before sorting so ties are broken randomly.

    Returns None if no eligible node is available.
    """
    now        = time.time()
    candidates = []

    for node_id, info in nodes.items():
        if not info["healthy"]:
            continue

        if now - (info.get("last_seen") or 0) > NODE_STALE_SECONDS:
            continue

        labels = info.get("labels", {})
        if not all(labels.get(k) == v for k, v in constraints.items()):
            continue

        job_count = count_active_node_jobs(node_id)
        cpu_used  = info.get("state", {}).get("cpu", 0)
        candidates.append((job_count, cpu_used, node_id))

    if not candidates:
        return None

    random.shuffle(candidates)
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------

def cancel_job(job_id: str, node_id: str):
    """Mark a job as CANCELLED due to explicit operator action and enqueue a cancellation signal."""
    mark_cancelled(job_id, node_id)
    enqueue_cancel(job_id, node_id)


def reconcile_once():
    """
    Single reconciliation pass:

    1. Expire leases so lost jobs do not count toward replica targets.
       Skipped during the startup grace period to avoid marking jobs lost
       due to a brief controller restart.
    2. For each workload, schedule new jobs on available nodes until the
       active job count matches the desired replica count.
    """
    if time.time() - _startup_time > STARTUP_GRACE_SECONDS:
        expire_lost_jobs()

    workloads = list_workloads()
    nodes     = list_nodes()

    for w in workloads:
        running = count_active_workload_jobs(w["name"])
        missing = w["replicas"] - running

        for _ in range(missing):
            node_id = pick_node(nodes, w.get("constraints", {}), w.get("resources", {}))
            if node_id is None:
                break
            create_job(
                node_id,
                w["command"],
                image=w.get("image"),
                workload_name=w["name"],
            )


async def reconcile_loop():
    """Run reconcile_once every 5 seconds in the background."""
    while True:
        try:
            reconcile_once()
        except Exception as e:
            print(f"[reconcile] error: {e}")
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app):
    global _startup_time
    _startup_time = time.time()
    init_db()
    asyncio.create_task(reconcile_loop())
    yield


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Agent endpoints
# These are called by agents, not by operators. All require a per-node token.
# ---------------------------------------------------------------------------

@app.post("/register")
def register(data: dict, x_bootstrap_token: str | None = Header(None)):
    """
    Register a new node. Requires the shared bootstrap token.
    Returns a per-node token that the agent must use for all subsequent calls.
    """
    if x_bootstrap_token != get_bootstrap_token():
        raise HTTPException(status_code=401, detail="invalid bootstrap token")
    token = register_node(
        data["node"],
        data["address"],
        data.get("labels"),
        data.get("capacity"),
    )
    return {"ok": True, "token": token}


@app.post("/state")
def state(data: dict, x_node_token: str | None = Header(None)):
    """Receive a state report (CPU, memory, etc.) from an agent."""
    require_agent_auth(data["node"], x_node_token)
    update_state(data["node"], data)
    return {"ok": True}


@app.get("/agent/jobs/{node}")
def agent_job(node: str, x_node_token: str | None = Header(None)):
    """
    Return the next pending job for a node and mark it as RUNNING.
    Returns an empty object if there is nothing to do.
    """
    require_agent_auth(node, x_node_token)
    job = get_pending_job(node)
    if not job:
        return {}
    jid, cmd, image = job
    start_job(jid)
    return {"job": jid, "command": cmd, "image": image}


@app.post("/agent/heartbeat/{job_id}")
def agent_heartbeat(job_id: str, data: dict, x_node_token: str | None = Header(None)):
    """Renew the lease of a running job to prevent it from being marked lost."""
    require_agent_auth(data["node"], x_node_token)
    renew_lease(job_id)
    return {"ok": True}


@app.post("/agent/result")
def agent_result(data: dict, x_node_token: str | None = Header(None)):
    """Receive the final status and output of a completed job."""
    require_agent_auth(data["node"], x_node_token)
    finish_job(data["job"], data["status"], data["result"])
    return {"ok": True}


@app.post("/agent/log")
def agent_log(data: dict, x_node_token: str | None = Header(None)):
    """Receive a single log line from an agent for a running or completed job."""
    require_agent_auth(data["node"], x_node_token)
    store_log(data["job"], data["line"])
    return {"ok": True}


@app.get("/agent/cancel/{node}")
def agent_cancel(node: str, x_node_token: str | None = Header(None)):
    """
    Return and clear all pending cancellations for a node.
    The agent calls this on every poll cycle and kills the listed jobs.
    """
    require_agent_auth(node, x_node_token)
    return {"cancel": get_pending_cancels(node)}


# ---------------------------------------------------------------------------
# Operator endpoints
# These are called by the CLI. All require the shared operator token via
# the X-Operator-Token header. Set TCP_OPERATOR_TOKEN on the controller,
# and provide the same value to the CLI via TCP_OPERATOR_TOKEN or
# ~/.tcp/operator.token.
# ---------------------------------------------------------------------------

@app.get("/nodes")
def nodes(x_operator_token: str | None = Header(None)):
    """Return all registered nodes and their current state."""
    require_operator_auth(x_operator_token)
    return list_nodes()


@app.delete("/nodes/{node_id}")
def revoke(node_id: str, x_operator_token: str | None = Header(None)):
    """
    Revoke a node's token. The node will be rejected on its next poll and
    will shut down. The node record and its job history are preserved.
    """
    require_operator_auth(x_operator_token)
    revoke_node(node_id)
    return {"ok": True}


@app.post("/jobs")
def job(data: dict, x_operator_token: str | None = Header(None)):
    """
    Submit a one-shot job directly to a specific node.
    A Docker image is required — bare host execution is not permitted.
    """
    require_operator_auth(x_operator_token)
    require_image(data.get("image"))
    jid = create_job(data["node"], data["command"], image=data["image"])
    return {"job": jid}


@app.get("/jobs")
def jobs(x_operator_token: str | None = Header(None)):
    """Return all jobs."""
    require_operator_auth(x_operator_token)
    return list_jobs()


@app.get("/jobs/{job_id}/logs")
def job_logs(job_id: str, x_operator_token: str | None = Header(None)):
    """Return all log lines for a job."""
    require_operator_auth(x_operator_token)
    return get_logs(job_id)


@app.get("/jobs/{job_id}/logs/stream")
async def job_logs_stream(job_id: str, x_operator_token: str | None = Header(None)):
    """
    Stream log lines for a job using Server-Sent Events.
    Replays all existing lines first, then tails new ones as they arrive.
    The stream closes automatically when the job reaches any terminal state:
    succeeded, failed, lost, or cancelled.
    """
    require_operator_auth(x_operator_token)

    async def generator():
        cursor = 0
        while True:
            lines = get_logs(job_id)
            for entry in lines[cursor:]:
                yield {"data": entry["line"]}
            cursor = len(lines)

            # Stop streaming once the job is terminal and we've sent all lines.
            job_list = list_jobs()
            job = next((j for j in job_list if j["id"] == job_id), None)
            if job and job["status"] in _STREAM_TERMINAL:
                break

            await asyncio.sleep(0.5)

    return EventSourceResponse(generator())


@app.post("/workloads")
def workload(data: dict, x_operator_token: str | None = Header(None)):
    """
    Create or replace a workload. A Docker image is required — bare host
    execution is not permitted. The reconciler will schedule jobs to meet
    the replica count.
    """
    require_operator_auth(x_operator_token)
    require_image(data.get("image"))
    create_workload(
        data["name"],
        data["command"],
        data["replicas"],
        image=data["image"],
        constraints=data.get("constraints"),
        resources=data.get("resources"),
    )
    return {"ok": True}


@app.get("/workloads")
def workloads(x_operator_token: str | None = Header(None)):
    """Return all workload definitions."""
    require_operator_auth(x_operator_token)
    return list_workloads()


@app.post("/workloads/{name}/scale")
def scale_workload(name: str, data: dict, x_operator_token: str | None = Header(None)):
    """
    Adjust the replica count of a running workload.
    Excess jobs are cancelled immediately; new jobs are scheduled by the reconciler.
    """
    require_operator_auth(x_operator_token)
    replicas = data["replicas"]
    updated  = update_workload_replicas(name, replicas)
    if not updated:
        raise HTTPException(status_code=404, detail="workload not found")

    excess = get_excess_workload_jobs(name, replicas)
    for job_id, node_id in excess:
        cancel_job(job_id, node_id)

    return {"ok": True, "replicas": replicas, "cancelled": len(excess)}


@app.delete("/workloads/{name}")
def remove_workload(name: str, x_operator_token: str | None = Header(None)):
    """
    Remove a workload and cancel all its active jobs.
    Replicas are zeroed first so the reconciler cannot schedule new jobs
    in the window between cancellation and deletion.
    """
    require_operator_auth(x_operator_token)
    update_workload_replicas(name, 0, silent=True)
    excess = get_excess_workload_jobs(name, 0)
    node_count = len({node_id for _, node_id in excess})
    record_event("workload.deleting", f"workload {name} deleting — cancelling {len(excess)} job(s) across {node_count} node(s)")
    for job_id, node_id in excess:
        cancel_job(job_id, node_id)
    delete_workload(name)
    return {"ok": True, "cancelled": len(excess)}


@app.get("/events")
def events(x_operator_token: str | None = Header(None)):
    """Return the most recent cluster events in chronological order."""
    require_operator_auth(x_operator_token)
    return list_events()


@app.get("/events/stream")
async def events_stream(x_operator_token: str | None = Header(None)):
    """
    Stream cluster events using Server-Sent Events.
    Replays recent history first, then tails new events as they occur.
    """
    require_operator_auth(x_operator_token)

    async def generator():
        existing = list_events()
        cursor   = 0
        for entry in existing:
            yield {"data": f"{entry['ts']}|{entry['kind']}  {entry['message']}"}
            cursor = entry["id"]

        while True:
            new_events = get_events_since(cursor)
            for entry in new_events:
                yield {"data": f"{entry['ts']}|{entry['kind']}  {entry['message']}"}
                cursor = entry["id"]
            await asyncio.sleep(0.5)

    return EventSourceResponse(generator())
