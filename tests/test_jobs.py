import os
from controller.store import init_db, create_job, list_jobs, get_pending_job, start_job, finish_job


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_job_creation(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")

    jobs = list_jobs()
    assert jobs[0]["id"] == jid
    assert jobs[0]["command"] == "uptime"
    assert jobs[0]["status"] == "pending"
    assert jobs[0]["image"] is None
    assert jobs[0]["workload_name"] is None


def test_job_creation_with_image(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "echo hello", image="alpine")

    jobs = list_jobs()
    assert jobs[0]["id"] == jid
    assert jobs[0]["image"] == "alpine"
    assert jobs[0]["command"] == "echo hello"


def test_job_creation_with_workload_name(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime", workload_name="workers")

    jobs = list_jobs()
    assert jobs[0]["workload_name"] == "workers"


def test_job_lifecycle(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")

    row = get_pending_job("node1")
    assert row is not None
    assert row[0] == jid

    start_job(jid)
    assert list_jobs()[0]["status"] == "running"

    # pending query should return nothing now
    assert get_pending_job("node1") is None

    finish_job(jid, "finished", "some output")
    job = list_jobs()[0]
    assert job["status"] == "finished"
    assert job["result"] == "some output"


def test_job_can_be_failed(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "bad-command")
    start_job(jid)
    finish_job(jid, "failed", "command not found")

    job = list_jobs()[0]
    assert job["status"] == "failed"
    assert job["result"] == "command not found"


def test_pending_job_returns_image(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "echo hello", image="alpine")

    row = get_pending_job("node1")
    assert row is not None
    job_id, command, image = row
    assert job_id == jid
    assert command == "echo hello"
    assert image == "alpine"


def test_pending_job_only_returns_for_correct_node(tmp_path):
    _setup(tmp_path)

    create_job("node1", "uptime")

    assert get_pending_job("node2") is None
    assert get_pending_job("node1") is not None
