
import asyncio
import os

from controller.store import (
    init_db,
    create_workload,
    register_node,
    list_jobs,
    count_active_workload_jobs,
)
from controller.api import reconcile_loop


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


async def _run_reconcile_once():
    task = asyncio.create_task(reconcile_loop())
    await asyncio.sleep(1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_reconcile_creates_correct_replica_count(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 3)

    asyncio.run(_run_reconcile_once())

    jobs = list_jobs()
    assert len(jobs) == 3
    assert all(j["workload_name"] == "workers" for j in jobs)


def test_reconcile_two_workloads_dont_interfere(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 2)
    create_workload("checkers", "uptime", 1)  # same command, different workload

    asyncio.run(_run_reconcile_once())

    assert count_active_workload_jobs("workers") == 2
    assert count_active_workload_jobs("checkers") == 1


def test_reconcile_respects_label_constraints(tmp_path):
    _setup(tmp_path)

    register_node("eu-node", "http://localhost:9000", labels={"region": "eu"}, capacity={"cpu": 4, "mem": 8192})
    register_node("us-node", "http://localhost:9001", labels={"region": "us"}, capacity={"cpu": 4, "mem": 8192})

    create_workload("eu-workers", "uptime", 2, constraints={"region": "eu"})

    asyncio.run(_run_reconcile_once())

    jobs = list_jobs()
    assert all(j["node"] == "eu-node" for j in jobs)


def test_reconcile_skips_when_no_matching_node(tmp_path):
    _setup(tmp_path)

    register_node("us-node", "http://localhost:9001", labels={"region": "us"}, capacity={"cpu": 4, "mem": 8192})
    create_workload("eu-workers", "uptime", 2, constraints={"region": "eu"})

    asyncio.run(_run_reconcile_once())

    assert list_jobs() == []
