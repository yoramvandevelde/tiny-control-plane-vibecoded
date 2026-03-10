import os
from controller.store import (
    init_db,
    register_node,
    create_job,
    start_job,
    enqueue_cancel,
    pop_cancel_jobs,
    mark_lost,
)
from controller.api import reconcile_once, cancel_job


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_enqueue_and_pop_cancel(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100")
    start_job(job_id)

    enqueue_cancel(job_id, "node1")

    result = pop_cancel_jobs("node1")
    assert job_id in result


def test_pop_cancel_clears_queue(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100")
    start_job(job_id)

    enqueue_cancel(job_id, "node1")
    pop_cancel_jobs("node1")

    # Second pop should be empty
    assert pop_cancel_jobs("node1") == []


def test_pop_cancel_only_returns_for_correct_node(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    _ = register_node("node2", "http://localhost:9001")
    job_id = create_job("node1", "sleep 100")
    start_job(job_id)

    enqueue_cancel(job_id, "node1")

    assert pop_cancel_jobs("node2") == []
    assert job_id in pop_cancel_jobs("node1")


def test_cancel_job_marks_cancelled_and_enqueues(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100")
    start_job(job_id)

    cancel_job(job_id, "node1")

    from controller.store import list_jobs, JobStatus
    job = list_jobs()[0]
    assert job["status"] == JobStatus.CANCELLED

    queued = pop_cancel_jobs("node1")
    assert job_id in queued


def test_enqueue_multiple_cancels(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    id1 = create_job("node1", "sleep 100")
    id2 = create_job("node1", "sleep 100")
    start_job(id1)
    start_job(id2)

    enqueue_cancel(id1, "node1")
    enqueue_cancel(id2, "node1")

    result = pop_cancel_jobs("node1")
    assert set(result) == {id1, id2}


def test_reconciler_does_not_replace_cancelled_jobs(tmp_path):
    """Cancelled jobs are marked cancelled; reconciler should fill back to replica count."""
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    from controller.store import create_workload, count_active_workload_jobs, list_jobs
    create_workload("workers", "sleep 100", 2)

    reconcile_once()
    assert count_active_workload_jobs("workers") == 2

    # Cancel one job — reconciler should schedule a replacement
    job_id = list_jobs()[0]["id"]
    cancel_job(job_id, "node1")

    reconcile_once()
    assert count_active_workload_jobs("workers") == 2
    assert len(list_jobs()) == 3  # original 2 + 1 replacement


def test_undeploy_cancels_active_jobs(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 8192})
    from controller.store import create_workload, list_jobs, JobStatus, delete_workload
    from controller.api import get_excess_workload_jobs
    create_workload("workers", "sleep 100", 2)

    reconcile_once()
    assert len(list_jobs()) == 2

    # Simulate what remove_workload does
    excess = get_excess_workload_jobs("workers", 0)
    for job_id, node_id in excess:
        cancel_job(job_id, node_id)
    delete_workload("workers")

    jobs = list_jobs()
    assert all(j["status"] == JobStatus.CANCELLED for j in jobs)
    queued = pop_cancel_jobs("node1")
    assert len(queued) == 2
