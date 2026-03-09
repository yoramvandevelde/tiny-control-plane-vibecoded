
from fastapi import FastAPI
import asyncio
from controller.store import *

app = FastAPI()

@app.on_event("startup")
async def start():
    init_db()
    asyncio.create_task(reconcile_loop())


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
    jid = create_job(data["node"], data["command"])
    return {"job": jid}


@app.get("/jobs")
def jobs():
    return list_jobs()


@app.get("/agent/jobs/{node}")
def agent_job(node: str):

    job = get_pending_job(node)

    if not job:
        return {}

    jid, cmd = job
    start_job(jid)

    return {"job": jid, "command": cmd}


@app.post("/agent/result")
def agent_result(data: dict):
    finish_job(data["job"], data["status"], data["result"])
    return {"ok": True}


@app.post("/workloads")
def workload(data: dict):
    create_workload(data["name"], data["command"], data["replicas"])
    return {"ok": True}


async def reconcile_loop():
    while True:

        workloads = list_workloads()
        jobs = list_jobs()

        for w in workloads:

            running = 0

            for j in jobs:
                if j["command"] == w["command"] and j["status"] != "finished":
                    running += 1

            missing = w["replicas"] - running

            for _ in range(missing):
                nodes = list_nodes()
                if not nodes:
                    continue

                node = list(nodes.keys())[0]
                create_job(node, w["command"])

        await asyncio.sleep(5)
