import os
import time
from controller.store import (
    init_db, create_job, list_jobs, get_pending_job,
    start_job, finish_job, mark_lost, JobStatus,
)


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_job_creation(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")

    jobs = list_jobs()
    assert jobs[0]["id"] == jid
    assert jobs[0]["command"] == "uptime"
    assert jobs[0]["status"] == JobStatus.PENDING
    assert jobs[0]["image"] is None
    assert jobs[0]["workload_name"] is None


def test_job_creation_with_image(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "echo hello", image="alpine")

    jobs = list_jobs()
    assert jobs[0]["id"] == jid
    assert jobs[0]["image"] == "alpine"


def test_job_creation_with_workload_name(tmp_path):
    _setup(tmp_path)

    create_job("node1", "uptime", workload_name="workers")

    assert list_jobs()[0]["workload_name"] == "workers"


def test_job_creation_records_timestamps(tmp_path):
    _setup(tmp_path)

    before = time.time()
    create_job("node1", "uptime")
    after = time.time()

    job = list_jobs()[0]
    assert job["created"] is not None
    assert job["updated"] is not None
    assert before <= job["created"] <= after


def test_job_lifecycle_succeeded(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")

    row = get_pending_job("node1")
    assert row is not None
    assert row[0] == jid

    start_job(jid)
    assert list_jobs()[0]["status"] == JobStatus.RUNNING

    assert get_pending_job("node1") is None

    finish_job(jid, JobStatus.SUCCEEDED, "some output")
    job = list_jobs()[0]
    assert job["status"] == JobStatus.SUCCEEDED
    assert job["result"] == "some output"


def test_job_lifecycle_failed(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "bad-command")
    start_job(jid)
    finish_job(jid, JobStatus.FAILED, "command not found")

    job = list_jobs()[0]
    assert job["status"] == JobStatus.FAILED
    assert job["result"] == "command not found"


def test_job_lifecycle_lost(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    start_job(jid)
    mark_lost(jid)

    job = list_jobs()[0]
    assert job["status"] == JobStatus.LOST


def test_finish_job_normalises_finished_string(tmp_path):
    """Agent previously sent 'finished'; ensure it still maps to succeeded."""
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    finish_job(jid, "finished", "output")

    assert list_jobs()[0]["status"] == JobStatus.SUCCEEDED


def test_pending_job_returns_image(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "echo hello", image="alpine")

    row = get_pending_job("node1")
    job_id, command, image = row
    assert job_id == jid
    assert image == "alpine"


def test_pending_job_only_returns_for_correct_node(tmp_path):
    _setup(tmp_path)

    create_job("node1", "uptime")

    assert get_pending_job("node2") is None
    assert get_pending_job("node1") is not None


def test_lost_job_not_counted_as_active(tmp_path):
    _setup(tmp_path)

    from controller.store import count_active_workload_jobs
    create_job("node1", "uptime", workload_name="workers")
    jid = create_job("node1", "uptime", workload_name="workers")

    assert count_active_workload_jobs("workers") == 2

    mark_lost(jid)
    assert count_active_workload_jobs("workers") == 1


def test_list_jobs_includes_timestamps(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    start_job(jid)

    job = list_jobs()[0]
    assert "created" in job
    assert "updated" in job
    assert job["created"] is not None
    assert job["updated"] is not None
    # updated should be >= created after start_job
    assert job["updated"] >= job["created"]
