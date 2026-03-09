
import os

from controller.store import (
    init_db,
    create_workload,
    register_node,
    list_jobs,
    count_active_workload_jobs,
)
from controller.api import reconcile_once


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_reconcile_creates_correct_replica_count(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 3)

    reconcile_once()

    jobs = list_jobs()
    assert len(jobs) == 3
    assert all(j["workload_name"] == "workers" for j in jobs)


def test_reconcile_is_idempotent(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 3)

    reconcile_once()
    reconcile_once()  # should not create extra jobs

    assert len(list_jobs()) == 3


def test_reconcile_two_workloads_dont_interfere(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 2)
    create_workload("checkers", "uptime", 1)  # same command, different workload

    reconcile_once()

    assert count_active_workload_jobs("workers") == 2
    assert count_active_workload_jobs("checkers") == 1


def test_reconcile_respects_label_constraints(tmp_path):
    _setup(tmp_path)

    register_node("eu-node", "http://localhost:9000", labels={"region": "eu"}, capacity={"cpu": 4, "mem": 8192})
    register_node("us-node", "http://localhost:9001", labels={"region": "us"}, capacity={"cpu": 4, "mem": 8192})

    create_workload("eu-workers", "uptime", 2, constraints={"region": "eu"})

    reconcile_once()

    jobs = list_jobs()
    assert len(jobs) == 2
    assert all(j["node"] == "eu-node" for j in jobs)


def test_reconcile_skips_when_no_matching_node(tmp_path):
    _setup(tmp_path)

    register_node("us-node", "http://localhost:9001", labels={"region": "us"}, capacity={"cpu": 4, "mem": 8192})
    create_workload("eu-workers", "uptime", 2, constraints={"region": "eu"})

    reconcile_once()

    assert list_jobs() == []
