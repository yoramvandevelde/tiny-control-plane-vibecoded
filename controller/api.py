
from fastapi import FastAPI
import asyncio
import random
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
    mark_lost,
    renew_lease,
    expire_lost_jobs,
    create_workload,
    list_workloads,
    count_active_workload_jobs,
    count_active_node_jobs,
    get_excess_workload_jobs,
    update_workload_replicas,
    delete_workload,
    store_log,
    get_logs,
    JobStatus,
)

NODE_STALE_SECONDS = 30


def pick_node(nodes: dict, constraints: dict, resources: dict) -> str | None:
    """
    Select a node using:
    1. Only healthy, recently-seen nodes
    2. Label constraint matching
    3. Least active jobs (primary) + lowest CPU (secondary)
    4. Random shuffle before sorting so equal scores are broken randomly
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

        job_count = count_active_node_jobs(node_id)
        cpu_used = info.get("state", {}).get("cpu", 0)
        candidates.append((job_count, cpu_used, node_id))

    if not candidates:
        return None

    random.shuffle(candidates)
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def reconcile_once():
    # Expire leases before counting active jobs so lost jobs don't block
    # workload replica targets
    expire_lost_jobs()

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


@app.post("/agent/heartbeat/{job_id}")
def agent_heartbeat(job_id: str):
    renew_lease(job_id)
    return {"ok": True}


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


@app.post("/workloads/{name}/scale")
def scale_workload(name: str, data: dict):
    replicas = data["replicas"]
    updated = update_workload_replicas(name, replicas)
    if not updated:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="workload not found")

    excess = get_excess_workload_jobs(name, replicas)
    for job_id in excess:
        mark_lost(job_id)

    return {"ok": True, "replicas": replicas, "cancelled": len(excess)}


@app.delete("/workloads/{name}")
def remove_workload(name: str):
    delete_workload(name)
    return {"ok": True}


@app.post("/agent/log")
def agent_log(data: dict):
    store_log(data["job"], data["line"])
    return {"ok": True}


@app.get("/jobs/{job_id}/logs")
def job_logs(job_id: str):
    return get_logs(job_id)
