"""
Tests for the cancel acknowledgement path:
  - POST /agent/cancel/ack marks a cancel as acked
  - Acked cancels are not redelivered
  - Unacked cancels continue to be redelivered after the timeout
  - agent.ack_cancels() calls the endpoint for each job
"""
import os
import time

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

import controller.store as store
import controller.api as api
from controller.store import (
    init_db,
    register_node,
    create_job,
    start_job,
    enqueue_cancel,
    get_pending_cancels,
    ack_cancel,
    get_db,
    CANCEL_REDELIVER_SECONDS,
)

BOOTSTRAP = "test-bootstrap-token"
OPERATOR  = "test-operator-token"


# ---------------------------------------------------------------------------
# Store-level ack tests (no HTTP)
# ---------------------------------------------------------------------------

def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_ack_cancel_stops_redelivery(tmp_path):
    """Once acked, a cancel must not appear in get_pending_cancels even after the redeliver timeout."""
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100")
    start_job(job_id)

    enqueue_cancel(job_id, "node1")
    get_pending_cancels("node1")  # first delivery

    # Backdate delivery so it would normally be redelivered.
    get_db().execute(
        "UPDATE cancel_jobs SET delivered=? WHERE job_id=?",
        (time.time() - CANCEL_REDELIVER_SECONDS - 1, job_id),
    )
    get_db().commit()

    # Ack it — should not reappear.
    ack_cancel(job_id, "node1")

    retried = get_pending_cancels("node1")
    assert job_id not in retried


def test_ack_cancel_is_idempotent(tmp_path):
    """Calling ack_cancel twice on the same job must not raise."""
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    job_id = create_job("node1", "sleep 100")
    start_job(job_id)

    enqueue_cancel(job_id, "node1")
    get_pending_cancels("node1")

    ack_cancel(job_id, "node1")
    ack_cancel(job_id, "node1")  # second call must be a no-op

    assert get_pending_cancels("node1") == []


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cluster.db")
    monkeypatch.setenv("TCP_BOOTSTRAP_TOKEN", BOOTSTRAP)
    monkeypatch.setenv("TCP_OPERATOR_TOKEN", OPERATOR)
    store.DB_PATH = db_path
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
    return r.json()["token"]


def test_cancel_ack_endpoint_returns_ok(client, registered):
    job_id = store.create_job("node1", "sleep 100")
    store.start_job(job_id)
    store.enqueue_cancel(job_id, "node1")
    store.get_pending_cancels("node1")  # mark delivered

    r = client.post(
        "/agent/cancel/ack",
        json={"job": job_id, "node": "node1"},
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_cancel_ack_prevents_redelivery(client, registered):
    """After acking, the cancel must not reappear even past the redeliver window."""
    job_id = store.create_job("node1", "sleep 100")
    store.start_job(job_id)
    store.enqueue_cancel(job_id, "node1")
    store.get_pending_cancels("node1")

    # Backdate delivery timestamp.
    store.get_db().execute(
        "UPDATE cancel_jobs SET delivered=? WHERE job_id=?",
        (time.time() - CANCEL_REDELIVER_SECONDS - 1, job_id),
    )
    store.get_db().commit()

    client.post(
        "/agent/cancel/ack",
        json={"job": job_id, "node": "node1"},
        headers={"X-Node-Token": registered},
    )

    r = client.get("/agent/cancel/node1", headers={"X-Node-Token": registered})
    assert job_id not in r.json()["cancel"]


def test_cancel_ack_rejected_with_wrong_token(client, registered):
    job_id = store.create_job("node1", "sleep 100")
    store.start_job(job_id)
    store.enqueue_cancel(job_id, "node1")

    r = client.post(
        "/agent/cancel/ack",
        json={"job": job_id, "node": "node1"},
        headers={"X-Node-Token": "bad-token"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Agent ack_cancels() unit test
# ---------------------------------------------------------------------------

def test_agent_ack_cancels_posts_for_each_job(monkeypatch):
    """ack_cancels() must POST to /agent/cancel/ack for every job ID in the list."""
    import sys
    sys.argv = ["agent.py", "--node-id", "node1", "--port", "9000"]
    import agent.agent as agent_module

    posted = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append((url, json))
        return MagicMock(status_code=200)

    monkeypatch.setattr(agent_module.httpx, "post", fake_post)
    monkeypatch.setattr(agent_module, "_node_token", "tok")

    agent_module.ack_cancels(["job-1", "job-2"])

    assert len(posted) == 2
    urls    = [p[0] for p in posted]
    job_ids = [p[1]["job"] for p in posted]
    assert all("/agent/cancel/ack" in u for u in urls)
    assert set(job_ids) == {"job-1", "job-2"}


def test_agent_ack_cancels_swallows_errors(monkeypatch):
    """A failed ack must not propagate — the controller will redeliver."""
    import sys
    sys.argv = ["agent.py", "--node-id", "node1", "--port", "9000"]
    import agent.agent as agent_module

    def failing_post(*args, **kwargs):
        raise Exception("controller unreachable")

    monkeypatch.setattr(agent_module.httpx, "post", failing_post)

    # Must not raise.
    agent_module.ack_cancels(["job-1"])
