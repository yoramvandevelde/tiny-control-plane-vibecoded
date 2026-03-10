import os
import pytest
from controller.api import reconcile_once
from controller.store import init_db, register_node, create_workload, list_jobs


def test_resource_aware_scheduling(tmp_path):
    """A job requiring 4 CPUs must land on the large node, not the small one."""
    init_db(str(tmp_path / "cluster.db"))

    register_node("small-node", "http://1.1.1.1", capacity={"cpu": 1, "mem": 512})
    register_node("large-node", "http://2.2.2.2", capacity={"cpu": 8, "mem": 8192})

    create_workload("heavy-work", "uptime", replicas=1, req_cpu=4, req_mem_mb=1024)

    reconcile_once()

    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["node"] == "large-node"


def test_insufficient_resources_stays_pending(tmp_path):
    """If no node has enough resources the reconciler creates no job."""
    init_db(str(tmp_path / "cluster.db"))

    register_node("tiny-node", "http://1.1.1.1", capacity={"cpu": 1, "mem": 512})

    # Needs 2 CPUs; only node has 1
    create_workload("too-big", "uptime", replicas=1, req_cpu=2)

    reconcile_once()

    assert len(list_jobs()) == 0
