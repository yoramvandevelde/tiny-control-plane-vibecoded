"""
Tests for garbage collection:
  - store.prune_jobs / prune_events / preview_gc
  - store.prune_stale_cancel_jobs (automatic, called from reconciler)
  - POST /gc and GET /gc/preview API endpoints
  - reconcile_once() prunes cancel_jobs automatically
"""
import os
import time

import pytest
from fastapi.testclient import TestClient

import controller.store as store
import controller.api as api
from controller.store import (
    JobStatus,
    ack_cancel,
    create_job,
    enqueue_cancel,
    get_logs,
    get_pending_cancels,
    init_db,
    list_events,
    preview_gc,
    prune_events,
    prune_jobs,
    prune_stale_cancel_jobs,
    record_event,
    register_node,
    start_job,
    finish_job,
    store_log,
    CANCEL_REDELIVER_SECONDS,
)

BOOTSTRAP = "test-bootstrap-token"
OPERATOR  = "test-operator-token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cluster.db")
    monkeypatch.setenv("TCP_BOOTSTRAP_TOKEN", BOOTSTRAP)
    monkeypatch.setenv("TCP_OPERATOR_TOKEN", OPERATOR)
    store.DB_PATH     = db_path
    store._local.conn = None
    store.init_db(db_path)
    monkeypatch.setattr(api, "_startup_time", 0.0)
    yield
    store._local.conn = None


@pytest.fixture()
def client():
    with TestClient(api.app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def registered(client):
    r = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 200
    return r.json()["token"]


def _op(extra: dict = None) -> dict:
    h = {"X-Operator-Token": OPERATOR}
    if extra:
        h.update(extra)
    return h


def _age_job(job_id: str, age_seconds: float):
    """Back-date a job's updated timestamp to simulate age."""
    store.get_db().execute(
        "UPDATE jobs SET updated = ? WHERE id = ?",
        (time.time() - age_seconds, job_id),
    )
    store.get_db().commit()


def _age_event(event_id: int, age_seconds: float):
    """Back-date an event's ts to simulate age."""
    store.get_db().execute(
        "UPDATE events SET ts = ? WHERE id = ?",
        (time.time() - age_seconds, event_id),
    )
    store.get_db().commit()


# ---------------------------------------------------------------------------
# prune_jobs
# ---------------------------------------------------------------------------

def test_prune_jobs_removes_old_terminal_jobs():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "echo hi", image="alpine")
    finish_job(job_id, "succeeded", "hi")
    _age_job(job_id, 8 * 86400)

    result = prune_jobs(7)

    assert result["jobs"] == 1
    assert store.list_jobs() == []


def test_prune_jobs_keeps_recent_terminal_jobs():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "echo hi", image="alpine")
    finish_job(job_id, "succeeded", "hi")

    result = prune_jobs(7)

    assert result["jobs"] == 0
    assert len(store.list_jobs()) == 1


def test_prune_jobs_does_not_remove_active_jobs():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100", image="alpine")
    start_job(job_id)
    _age_job(job_id, 8 * 86400)

    prune_jobs(7)

    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == JobStatus.RUNNING


def test_prune_jobs_cascades_to_logs():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "echo hi", image="alpine")
    store_log(job_id, "line one")
    store_log(job_id, "line two")
    finish_job(job_id, "succeeded", "hi")
    _age_job(job_id, 8 * 86400)

    prune_jobs(7)

    assert get_logs(job_id) == []


def test_prune_jobs_cascades_to_cancel_jobs():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100", image="alpine")
    start_job(job_id)
    enqueue_cancel(job_id, "node1")
    finish_job(job_id, "cancelled", "")
    _age_job(job_id, 8 * 86400)

    prune_jobs(7)

    row = store.get_db().execute(
        "SELECT COUNT(*) FROM cancel_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    assert row[0] == 0


def test_prune_jobs_removes_all_terminal_statuses():
    register_node("node1", "http://localhost:9000")
    for status, result in [("succeeded", "ok"), ("failed", "err"), ("cancelled", ""), ("lost", "")]:
        jid = create_job("node1", "echo", image="alpine")
        finish_job(jid, status, result)
        _age_job(jid, 8 * 86400)

    result = prune_jobs(7)

    assert result["jobs"] == 4
    assert store.list_jobs() == []


# ---------------------------------------------------------------------------
# prune_events
# ---------------------------------------------------------------------------

def test_prune_events_removes_old_events():
    record_event("test.event", "old event")
    events = list_events()
    _age_event(events[0]["id"], 8 * 86400)

    deleted = prune_events(7)

    assert deleted == 1
    assert list_events() == []


def test_prune_events_keeps_recent_events():
    record_event("test.event", "recent event")

    deleted = prune_events(7)

    assert deleted == 0
    assert len(list_events()) == 1


def test_prune_events_mixed_ages():
    record_event("test.old",    "old")
    record_event("test.recent", "recent")
    events = list_events()
    _age_event(events[0]["id"], 8 * 86400)

    deleted = prune_events(7)

    assert deleted == 1
    remaining = list_events()
    assert len(remaining) == 1
    assert remaining[0]["kind"] == "test.recent"


# ---------------------------------------------------------------------------
# preview_gc
# ---------------------------------------------------------------------------

def test_preview_gc_returns_correct_counts():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "echo hi", image="alpine")
    finish_job(job_id, "succeeded", "hi")
    _age_job(job_id, 8 * 86400)

    record_event("test.event", "old")
    events = list_events()
    _age_event(events[0]["id"], 8 * 86400)

    result = preview_gc(7)

    assert result["jobs"]   >= 1
    assert result["events"] >= 1
    assert result["days"]   == 7


def test_preview_gc_does_not_delete():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "echo hi", image="alpine")
    finish_job(job_id, "succeeded", "hi")
    _age_job(job_id, 8 * 86400)

    preview_gc(7)

    assert len(store.list_jobs()) == 1


# ---------------------------------------------------------------------------
# prune_stale_cancel_jobs
# ---------------------------------------------------------------------------

def test_prune_stale_cancel_jobs_removes_acked():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100", image="alpine")
    start_job(job_id)
    enqueue_cancel(job_id, "node1")
    ack_cancel(job_id, "node1")

    deleted = prune_stale_cancel_jobs()

    assert deleted == 1
    assert store.get_db().execute("SELECT COUNT(*) FROM cancel_jobs").fetchone()[0] == 0


def test_prune_stale_cancel_jobs_removes_old_unacked():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100", image="alpine")
    start_job(job_id)
    enqueue_cancel(job_id, "node1")
    store.get_db().execute(
        "UPDATE cancel_jobs SET created = ? WHERE job_id = ?",
        (time.time() - CANCEL_REDELIVER_SECONDS - 1, job_id),
    )
    store.get_db().commit()

    deleted = prune_stale_cancel_jobs()

    assert deleted == 1


def test_prune_stale_cancel_jobs_keeps_fresh_unacked():
    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100", image="alpine")
    start_job(job_id)
    enqueue_cancel(job_id, "node1")

    deleted = prune_stale_cancel_jobs()

    assert deleted == 0
    assert job_id in get_pending_cancels("node1")


def test_reconcile_prunes_cancel_jobs_automatically():
    register_node("node1", "http://localhost:9000", capacity={"cpu": 4, "mem": 4096})
    job_id = create_job("node1", "sleep 100", image="alpine")
    start_job(job_id)
    enqueue_cancel(job_id, "node1")
    ack_cancel(job_id, "node1")

    assert store.get_db().execute("SELECT COUNT(*) FROM cancel_jobs").fetchone()[0] == 1

    api.reconcile_once()

    assert store.get_db().execute("SELECT COUNT(*) FROM cancel_jobs").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# POST /gc
# ---------------------------------------------------------------------------

def test_gc_endpoint_deletes_old_jobs(client, registered):
    r = client.post(
        "/jobs",
        json={"node": "node1", "command": "echo hi", "image": "alpine"},
        headers=_op(),
    )
    job_id = r.json()["job"]
    client.post(
        "/agent/result",
        json={"node": "node1", "job": job_id, "status": "succeeded", "result": "hi"},
        headers={"X-Node-Token": registered},
    )
    _age_job(job_id, 8 * 86400)

    r = client.post("/gc", json={"days": 7}, headers=_op())
    assert r.status_code == 200
    data = r.json()
    assert data["ok"]   is True
    assert data["jobs"] >= 1


def test_gc_endpoint_requires_operator_auth(client):
    r = client.post("/gc", json={"days": 7})
    assert r.status_code == 401


def test_gc_endpoint_rejects_invalid_days(client):
    r = client.post("/gc", json={"days": -1}, headers=_op())
    assert r.status_code == 422


def test_gc_endpoint_defaults_to_7_days(client):
    r = client.post("/gc", json={}, headers=_op())
    assert r.status_code == 200
    assert r.json()["days"] == 7.0


# ---------------------------------------------------------------------------
# GET /gc/preview
# ---------------------------------------------------------------------------

def test_gc_preview_endpoint_returns_counts(client, registered):
    r = client.post(
        "/jobs",
        json={"node": "node1", "command": "echo hi", "image": "alpine"},
        headers=_op(),
    )
    job_id = r.json()["job"]
    client.post(
        "/agent/result",
        json={"node": "node1", "job": job_id, "status": "succeeded", "result": "hi"},
        headers={"X-Node-Token": registered},
    )
    _age_job(job_id, 8 * 86400)

    r = client.get("/gc/preview", params={"days": 7}, headers=_op())
    assert r.status_code == 200
    data = r.json()
    assert data["jobs"] >= 1
    assert data["days"] == 7.0


def test_gc_preview_endpoint_requires_operator_auth(client):
    r = client.get("/gc/preview", params={"days": 7})
    assert r.status_code == 401


def test_gc_preview_does_not_delete(client, registered):
    r = client.post(
        "/jobs",
        json={"node": "node1", "command": "echo hi", "image": "alpine"},
        headers=_op(),
    )
    job_id = r.json()["job"]
    client.post(
        "/agent/result",
        json={"node": "node1", "job": job_id, "status": "succeeded", "result": "hi"},
        headers={"X-Node-Token": registered},
    )
    _age_job(job_id, 8 * 86400)

    client.get("/gc/preview", params={"days": 7}, headers=_op())

    jobs = client.get("/jobs", headers=_op()).json()
    assert len(jobs) >= 1
