
from fastapi import FastAPI
import asyncio
import time
from contextlib import asynccontextmanager
from controller.store import (
    init_db,
    register_node,
    update_state,
    list_nodes,
    create_job,
    list_jobs,
    get_pending_job,
    start_job,
    finish_job,
    create_workload,
    list_workloads,
    count_active_workload_jobs,
)

NODE_STALE_SECONDS = 30


def pick_node(nodes: dict, constraints: dict, resources: dict) -> str | None:
    """
    Select a node using:
    1. Only healthy, recently-seen nodes
    2. Label constraint matching
    3. Least-loaded (by CPU) among candidates
    """
    now = time.time()
    candidates = []

    for node_id, info in nodes.items():
        if not info["healthy"]:
            continue

        if now - (info.get("last_seen") or 0) > NODE_STALE_SECONDS:
            continue

        labels = info.get("labels", {})
        if not all(labels.get(k) == v for k, v in constraints.items()):
            continue

        cpu_used = info.get("state", {}).get("cpu", 0)
        candidates.append((cpu_used, node_id))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def reconcile_once():
    """Run a single reconcile pass. Extracted so tests can call it directly."""
    workloads = list_workloads()
    nodes = list_nodes()

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
    while True:
        try:
            reconcile_once()
        except Exception as e:
            print(f"[reconcile] error: {e}")

        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    init_db()
    asyncio.create_task(reconcile_loop())
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/register")
def register(data: dict):
    register_node(data["node"], data["address"], data.get("labels"), data.get("capacity"))
    return {"ok": True}


@app.post("/state")
def state(data: dict):
    update_state(data["node"], data)
    return {"ok": True}


@app.get("/nodes")
def nodes():
    return list_nodes()


@app.post("/jobs")
def job(data: dict):
    jid = create_job(data["node"], data["command"], image=data.get("image"))
    return {"job": jid}


@app.get("/jobs")
def jobs():
    return list_jobs()


@app.get("/agent/jobs/{node}")
def agent_job(node: str):
    job = get_pending_job(node)

    if not job:
        return {}

    jid, cmd, image = job
    start_job(jid)

    return {"job": jid, "command": cmd, "image": image}


@app.post("/agent/result")
def agent_result(data: dict):
    finish_job(data["job"], data["status"], data["result"])
    return {"ok": True}


@app.post("/workloads")
def workload(data: dict):
    create_workload(
        data["name"],
        data["command"],
        data["replicas"],
        image=data.get("image"),
        constraints=data.get("constraints"),
        resources=data.get("resources"),
    )
    return {"ok": True}


@app.get("/workloads")
def workloads():
    return list_workloads()
