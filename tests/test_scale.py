import os

from controller.store import (
    init_db,
    create_workload,
    create_job,
    start_job,
    register_node,
    list_jobs,
    count_active_workload_jobs,
    update_workload_replicas,
    get_excess_workload_jobs,
    list_workloads,
    JobStatus,
)
from controller.api import reconcile_once


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_update_workload_replicas(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 3)
    updated = update_workload_replicas("workers", 1)

    assert updated is True
    assert list_workloads()[0]["replicas"] == 1


def test_update_workload_replicas_returns_false_when_not_found(tmp_path):
    _setup(tmp_path)

    result = update_workload_replicas("nonexistent", 1)
    assert result is False


def test_get_excess_workload_jobs_returns_empty_when_within_target(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 3)
    create_job("node1", "uptime", workload_name="workers")
    create_job("node1", "uptime", workload_name="workers")

    excess = get_excess_workload_jobs("workers", 3)
    assert excess == []


def test_get_excess_workload_jobs_returns_correct_count(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 3)
    create_job("node1", "uptime", workload_name="workers")
    create_job("node1", "uptime", workload_name="workers")
    create_job("node1", "uptime", workload_name="workers")

    excess = get_excess_workload_jobs("workers", 1)
    assert len(excess) == 2


def test_get_excess_workload_jobs_prefers_pending_over_running(tmp_path):
    """Pending jobs should be cancelled before running ones."""
    _setup(tmp_path)

    create_workload("workers", "uptime", 3)
    running_id = create_job("node1", "uptime", workload_name="workers")
    pending_id = create_job("node1", "uptime", workload_name="workers")
    start_job(running_id)

    # Scale to 1 — one job should be cancelled, prefer the pending one
    excess = get_excess_workload_jobs("workers", 1)
    assert len(excess) == 1
    assert excess[0][0] == pending_id


def test_scale_down_marks_excess_jobs_lost(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 3)
    reconcile_once()

    assert count_active_workload_jobs("workers") == 3

    # Scale down to 1
    update_workload_replicas("workers", 1)
    excess = get_excess_workload_jobs("workers", 1)
    from controller.store import mark_lost
    for job_id, node_id in excess:
        mark_lost(job_id)

    assert count_active_workload_jobs("workers") == 1
    lost_jobs = [j for j in list_jobs() if j["status"] == JobStatus.LOST]
    assert len(lost_jobs) == 2


def test_scale_down_reconciler_does_not_replace_excess(tmp_path):
    """After scaling down, reconciler should not schedule replacements for lost jobs."""
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 3)
    reconcile_once()

    assert count_active_workload_jobs("workers") == 3

    update_workload_replicas("workers", 1)
    excess = get_excess_workload_jobs("workers", 1)
    from controller.store import mark_lost
    for job_id, node_id in excess:
        mark_lost(job_id)

    reconcile_once()

    assert count_active_workload_jobs("workers") == 1
    assert len(list_jobs()) == 3  # no new jobs created


def test_scale_up_reconciler_creates_new_jobs(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 1)
    reconcile_once()

    assert count_active_workload_jobs("workers") == 1

    update_workload_replicas("workers", 3)
    reconcile_once()

    assert count_active_workload_jobs("workers") == 3


def test_scale_to_zero_cancels_all_jobs(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    create_workload("workers", "uptime", 3)
    reconcile_once()

    assert count_active_workload_jobs("workers") == 3

    update_workload_replicas("workers", 0)
    excess = get_excess_workload_jobs("workers", 0)
    from controller.store import mark_lost
    for job_id, node_id in excess:
        mark_lost(job_id)

    assert count_active_workload_jobs("workers") == 0
    reconcile_once()
    assert count_active_workload_jobs("workers") == 0
