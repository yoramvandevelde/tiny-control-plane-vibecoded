import os
import time

from controller.store import (
    init_db,
    register_node,
    create_job,
    start_job,
    renew_lease,
    expire_lost_jobs,
    list_jobs,
    JobStatus,
    get_db,
    _db_lock,
)


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def _force_lease_expiry(job_id: str):
    """Set a job's lease_expires to the past so expire_lost_jobs picks it up."""
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET lease_expires=? WHERE id=?",
            (time.time() - 1, job_id),
        )
        get_db().commit()


# ---------------------------------------------------------------------------
# renew_lease
# ---------------------------------------------------------------------------

def test_renew_lease_extends_expiry(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    jid = create_job("node1", "sleep 100")
    start_job(jid)

    # Force the lease close to expiry
    _force_lease_expiry(jid)

    # Renew it
    renew_lease(jid)

    row = get_db().execute(
        "SELECT lease_expires FROM jobs WHERE id=?", (jid,)
    ).fetchone()

    assert row[0] > time.time()


def test_renew_lease_only_affects_running_jobs(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    jid = create_job("node1", "sleep 100")
    # Job is PENDING — renew should be a no-op

    before = get_db().execute(
        "SELECT lease_expires FROM jobs WHERE id=?", (jid,)
    ).fetchone()[0]

    renew_lease(jid)

    after = get_db().execute(
        "SELECT lease_expires FROM jobs WHERE id=?", (jid,)
    ).fetchone()[0]

    assert before == after


# ---------------------------------------------------------------------------
# expire_lost_jobs
# ---------------------------------------------------------------------------

def test_expire_lost_jobs_marks_expired_lease_as_lost(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    jid = create_job("node1", "sleep 100")
    start_job(jid)

    _force_lease_expiry(jid)
    expire_lost_jobs()

    assert list_jobs()[0]["status"] == JobStatus.LOST


def test_expire_lost_jobs_ignores_valid_lease(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    jid = create_job("node1", "sleep 100")
    start_job(jid)
    # lease_expires is set well into the future by start_job

    expire_lost_jobs()

    assert list_jobs()[0]["status"] == JobStatus.RUNNING


def test_expire_lost_jobs_ignores_pending_jobs(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    create_job("node1", "sleep 100")
    # PENDING jobs have no lease_expires

    expire_lost_jobs()

    assert list_jobs()[0]["status"] == JobStatus.PENDING


def test_expire_lost_jobs_only_expires_running(tmp_path):
    """A job that already succeeded should not be touched even if lease_expires is in the past."""
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    jid = create_job("node1", "uptime")
    start_job(jid)

    from controller.store import finish_job
    finish_job(jid, JobStatus.SUCCEEDED, "ok")
    _force_lease_expiry(jid)

    expire_lost_jobs()

    assert list_jobs()[0]["status"] == JobStatus.SUCCEEDED


def test_expire_lost_jobs_handles_multiple_expired(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    ids = [create_job("node1", "sleep 100") for _ in range(3)]
    for jid in ids:
        start_job(jid)
        _force_lease_expiry(jid)

    expire_lost_jobs()

    statuses = {j["status"] for j in list_jobs()}
    assert statuses == {JobStatus.LOST}


def test_expire_lost_jobs_is_selective(tmp_path):
    """Only jobs with expired leases should be marked lost."""
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    expired_id = create_job("node1", "sleep 100")
    healthy_id = create_job("node1", "sleep 100")

    start_job(expired_id)
    start_job(healthy_id)
    _force_lease_expiry(expired_id)

    expire_lost_jobs()

    jobs = {j["id"]: j["status"] for j in list_jobs()}
    assert jobs[expired_id] == JobStatus.LOST
    assert jobs[healthy_id] == JobStatus.RUNNING
