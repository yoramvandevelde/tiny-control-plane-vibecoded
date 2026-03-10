"""
Integration tests for the FastAPI HTTP layer.

Uses Starlette's TestClient so the full request/response cycle runs,
including header parsing, auth checks, and JSON serialisation.

The database is isolated per test via a tmp_path fixture that patches
DB_PATH before the app handles any request.
"""
import os
import pytest

from fastapi.testclient import TestClient

import controller.store as store
import controller.api as api


BOOTSTRAP = "test-bootstrap-token"
OPERATOR  = "test-operator-token"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point the store at a fresh per-test database and set the bootstrap and operator tokens."""
    db_path = str(tmp_path / "cluster.db")
    monkeypatch.setenv("TCP_BOOTSTRAP_TOKEN", BOOTSTRAP)
    monkeypatch.setenv("TCP_OPERATOR_TOKEN", OPERATOR)
    # Re-initialise with a clean DB for every test
    store.DB_PATH = db_path
    store._local.conn = None
    store.init_db(db_path)
    # Reset startup time so grace period doesn't interfere
    monkeypatch.setattr(api, "_startup_time", 0.0)
    yield
    store._local.conn = None


@pytest.fixture()
def client():
    with TestClient(api.app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def registered(client):
    """Register node1 and return its token."""
    r = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 200
    return r.json()["token"]


def _op(extra: dict = None) -> dict:
    """Return operator auth headers, optionally merged with extra headers."""
    h = {"X-Operator-Token": OPERATOR}
    if extra:
        h.update(extra)
    return h


# ---------------------------------------------------------------------------
# /register
# ---------------------------------------------------------------------------

def test_register_returns_token(client):
    r = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "token" in data
    assert len(data["token"]) > 0


def test_register_rejects_wrong_bootstrap_token(client):
    r = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000"},
        headers={"X-Bootstrap-Token": "wrong"},
    )
    assert r.status_code == 401


def test_register_rejects_missing_bootstrap_token(client):
    r = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000"},
    )
    assert r.status_code == 401


def test_register_with_labels(client):
    r = client.post(
        "/register",
        json={
            "node": "node1",
            "address": "http://localhost:9000",
            "labels": {"region": "homelab"},
        },
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 200
    nodes = client.get("/nodes", headers=_op()).json()
    assert nodes["node1"]["labels"]["region"] == "homelab"


def test_register_with_total_resources(client):
    r = client.post(
        "/register",
        json={
            "node": "node1",
            "address": "http://localhost:9000",
            "total_cpu": 16,
            "total_mem_mb": 32768,
        },
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 200

    nodes = client.get("/nodes", headers=_op()).json()
    assert nodes["node1"]["total_cpu"] == 16
    assert nodes["node1"]["total_mem_mb"] == 32768


# ---------------------------------------------------------------------------
# /state
# ---------------------------------------------------------------------------

def test_state_accepted_with_valid_token(client, registered):
    r = client.post(
        "/state",
        json={"node": "node1", "cpu": 0.3, "mem": 0.4},
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_state_rejected_with_wrong_token(client):
    r = client.post(
        "/state",
        json={"node": "node1", "cpu": 0.3, "mem": 0.4},
        headers={"X-Node-Token": "not-a-valid-token"},
    )
    assert r.status_code == 401


def test_state_rejected_with_missing_token(client):
    r = client.post(
        "/state",
        json={"node": "node1", "cpu": 0.3, "mem": 0.4},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /agent/jobs/{node}
# ---------------------------------------------------------------------------

def test_agent_jobs_returns_empty_when_no_work(client, registered):
    r = client.get(
        "/agent/jobs/node1",
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    assert r.json() == {}


def test_agent_jobs_returns_pending_job(client, registered):
    store.create_job("node1", "uptime", image="alpine")
    r = client.get(
        "/agent/jobs/node1",
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    data = r.json()
    assert "job" in data
    assert data["command"] == "uptime"


def test_agent_jobs_marks_job_running(client, registered):
    store.create_job("node1", "uptime", image="alpine")
    client.get("/agent/jobs/node1", headers={"X-Node-Token": registered})
    jobs = store.list_jobs()
    assert jobs[0]["status"] == store.JobStatus.RUNNING


def test_agent_jobs_rejected_with_wrong_token(client):
    r = client.get(
        "/agent/jobs/node1",
        headers={"X-Node-Token": "bad"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /agent/heartbeat/{job_id}
# ---------------------------------------------------------------------------

def test_agent_heartbeat_renews_lease(client, registered):
    jid = store.create_job("node1", "sleep 100", image="alpine")
    store.start_job(jid)

    r = client.post(
        f"/agent/heartbeat/{jid}",
        json={"node": "node1"},
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_agent_heartbeat_rejected_with_wrong_token(client, registered):
    jid = store.create_job("node1", "sleep 100", image="alpine")
    store.start_job(jid)

    r = client.post(
        f"/agent/heartbeat/{jid}",
        json={"node": "node1"},
        headers={"X-Node-Token": "bad"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /agent/result
# ---------------------------------------------------------------------------

def test_agent_result_records_success(client, registered):
    jid = store.create_job("node1", "uptime", image="alpine")
    store.start_job(jid)

    r = client.post(
        "/agent/result",
        json={"job": jid, "status": "succeeded", "result": "output", "node": "node1"},
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    assert store.list_jobs()[0]["status"] == store.JobStatus.SUCCEEDED


def test_agent_result_rejected_with_wrong_token(client, registered):
    jid = store.create_job("node1", "uptime", image="alpine")
    store.start_job(jid)

    r = client.post(
        "/agent/result",
        json={"job": jid, "status": "succeeded", "result": "", "node": "node1"},
        headers={"X-Node-Token": "bad"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /agent/log
# ---------------------------------------------------------------------------

def test_agent_log_stores_line(client, registered):
    jid = store.create_job("node1", "uptime", image="alpine")

    r = client.post(
        "/agent/log",
        json={"job": jid, "line": "hello from job", "node": "node1"},
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    logs = store.get_logs(jid)
    assert len(logs) == 1
    assert logs[0]["line"] == "hello from job"


def test_agent_log_rejected_with_wrong_token(client, registered):
    jid = store.create_job("node1", "uptime", image="alpine")

    r = client.post(
        "/agent/log",
        json={"job": jid, "line": "line", "node": "node1"},
        headers={"X-Node-Token": "bad"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /agent/cancel/{node}
# ---------------------------------------------------------------------------

def test_agent_cancel_returns_queued_job(client, registered):
    jid = store.create_job("node1", "sleep 100", image="alpine")
    store.start_job(jid)
    store.enqueue_cancel(jid, "node1")

    r = client.get(
        "/agent/cancel/node1",
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 200
    assert jid in r.json()["cancel"]


def test_agent_cancel_is_destructive_read(client, registered):
    jid = store.create_job("node1", "sleep 100", image="alpine")
    store.start_job(jid)
    store.enqueue_cancel(jid, "node1")

    client.get("/agent/cancel/node1", headers={"X-Node-Token": registered})
    r = client.get("/agent/cancel/node1", headers={"X-Node-Token": registered})
    assert r.json()["cancel"] == []


def test_agent_cancel_rejected_with_wrong_token(client):
    r = client.get(
        "/agent/cancel/node1",
        headers={"X-Node-Token": "bad"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /nodes  /nodes/{node_id}
# ---------------------------------------------------------------------------

def test_get_nodes_returns_registered_node(client, registered):
    r = client.get("/nodes", headers=_op())
    assert r.status_code == 200
    assert "node1" in r.json()


def test_get_nodes_empty_when_none_registered(client):
    r = client.get("/nodes", headers=_op())
    assert r.status_code == 200
    assert r.json() == {}


def test_get_nodes_rejected_without_operator_token(client):
    r = client.get("/nodes")
    assert r.status_code == 401


def test_get_nodes_rejected_with_wrong_operator_token(client):
    r = client.get("/nodes", headers={"X-Operator-Token": "wrong"})
    assert r.status_code == 401


def test_revoke_node(client, registered):
    r = client.delete("/nodes/node1", headers=_op())
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Node token should now be rejected
    r = client.post(
        "/state",
        json={"node": "node1", "cpu": 0.1, "mem": 0.1},
        headers={"X-Node-Token": registered},
    )
    assert r.status_code == 401


def test_revoke_node_rejected_without_operator_token(client, registered):
    r = client.delete("/nodes/node1")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /jobs
# ---------------------------------------------------------------------------

def test_post_job_creates_job(client, registered):
    r = client.post(
        "/jobs",
        json={"node": "node1", "command": "uptime", "image": "alpine"},
        headers=_op(),
    )
    assert r.status_code == 200
    assert "job" in r.json()


def test_post_job_rejected_without_image(client, registered):
    """All jobs must specify a Docker image — missing image returns 422."""
    r = client.post(
        "/jobs",
        json={"node": "node1", "command": "uptime"},
        headers=_op(),
    )
    assert r.status_code == 422


def test_post_job_rejected_with_empty_image(client, registered):
    """An empty image string is equivalent to no image and must be rejected."""
    r = client.post(
        "/jobs",
        json={"node": "node1", "command": "uptime", "image": ""},
        headers=_op(),
    )
    assert r.status_code == 422


def test_post_job_rejected_without_operator_token(client, registered):
    r = client.post("/jobs", json={"node": "node1", "command": "uptime", "image": "alpine"})
    assert r.status_code == 401


def test_get_jobs_returns_list(client, registered):
    store.create_job("node1", "uptime", image="alpine")
    r = client.get("/jobs", headers=_op())
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["command"] == "uptime"


def test_get_jobs_empty_when_none(client):
    r = client.get("/jobs", headers=_op())
    assert r.status_code == 200
    assert r.json() == []


def test_get_jobs_rejected_without_operator_token(client):
    r = client.get("/jobs")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /jobs/{job_id}/logs
# ---------------------------------------------------------------------------

def test_get_job_logs(client, registered):
    jid = store.create_job("node1", "uptime", image="alpine")
    store.store_log(jid, "line one")
    store.store_log(jid, "line two")

    r = client.get(f"/jobs/{jid}/logs", headers=_op())
    assert r.status_code == 200
    lines = [entry["line"] for entry in r.json()]
    assert lines == ["line one", "line two"]


def test_get_job_logs_empty(client):
    jid = store.create_job("node1", "uptime", image="alpine")
    r = client.get(f"/jobs/{jid}/logs", headers=_op())
    assert r.status_code == 200
    assert r.json() == []


def test_get_job_logs_rejected_without_operator_token(client):
    jid = store.create_job("node1", "uptime", image="alpine")
    r = client.get(f"/jobs/{jid}/logs")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /workloads
# ---------------------------------------------------------------------------

def test_post_workload_creates_workload(client):
    r = client.post(
        "/workloads",
        json={"name": "workers", "command": "uptime", "replicas": 3, "image": "alpine"},
        headers=_op(),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_post_workload_rejected_without_image(client):
    """Workloads must specify a Docker image — missing image returns 422."""
    r = client.post(
        "/workloads",
        json={"name": "workers", "command": "uptime", "replicas": 3},
        headers=_op(),
    )
    assert r.status_code == 422


def test_post_workload_rejected_with_empty_image(client):
    """An empty image string is equivalent to no image and must be rejected."""
    r = client.post(
        "/workloads",
        json={"name": "workers", "command": "uptime", "replicas": 3, "image": ""},
        headers=_op(),
    )
    assert r.status_code == 422


def test_post_workload_rejected_without_operator_token(client):
    r = client.post(
        "/workloads",
        json={"name": "workers", "command": "uptime", "replicas": 3, "image": "alpine"},
    )
    assert r.status_code == 401


def test_get_workloads_returns_list(client):
    store.create_workload("workers", "uptime", 3, image="alpine")
    r = client.get("/workloads", headers=_op())
    assert r.status_code == 200
    workloads = r.json()
    assert len(workloads) == 1
    assert workloads[0]["name"] == "workers"
    assert workloads[0]["replicas"] == 3


def test_get_workloads_rejected_without_operator_token(client):
    r = client.get("/workloads")
    assert r.status_code == 401


def test_post_workload_with_image_and_constraints(client):
    r = client.post(
        "/workloads",
        json={
            "name": "containers",
            "command": "echo hello",
            "replicas": 2,
            "image": "alpine",
            "constraints": {"region": "homelab"},
        },
        headers=_op(),
    )
    assert r.status_code == 200

    workloads = client.get("/workloads", headers=_op()).json()
    w = workloads[0]
    assert w["image"] == "alpine"
    assert w["constraints"]["region"] == "homelab"


def test_post_workload_with_resource_requests(client):
    r = client.post(
        "/workloads",
        json={
            "name": "heavy-workers",
            "command": "echo hello",
            "replicas": 2,
            "image": "alpine",
            "req_cpu": 4,
            "req_mem_mb": 2048,
        },
        headers=_op(),
    )
    assert r.status_code == 200

    workloads = client.get("/workloads", headers=_op()).json()
    w = workloads[0]
    assert w["req_cpu"] == 4
    assert w["req_mem_mb"] == 2048


# ---------------------------------------------------------------------------
# /workloads/{name}/scale
# ---------------------------------------------------------------------------

def test_scale_workload(client, registered):
    store.create_workload("workers", "uptime", 3, image="alpine")

    r = client.post("/workloads/workers/scale", json={"replicas": 1}, headers=_op())
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["replicas"] == 1


def test_scale_workload_not_found(client):
    r = client.post("/workloads/nonexistent/scale", json={"replicas": 1}, headers=_op())
    assert r.status_code == 404


def test_scale_workload_rejected_without_operator_token(client):
    store.create_workload("workers", "uptime", 3, image="alpine")
    r = client.post("/workloads/workers/scale", json={"replicas": 1})
    assert r.status_code == 401


def test_scale_down_cancels_excess_jobs(client, registered):
    store.create_workload("workers", "uptime", 3, image="alpine")
    api.reconcile_once()

    r = client.post("/workloads/workers/scale", json={"replicas": 1}, headers=_op())
    assert r.json()["cancelled"] == 2


# ---------------------------------------------------------------------------
# /workloads/{name}  DELETE
# ---------------------------------------------------------------------------

def test_delete_workload(client, registered):
    store.create_workload("workers", "uptime", 2, image="alpine")
    api.reconcile_once()

    r = client.delete("/workloads/workers", headers=_op())
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["cancelled"] == 2

    assert client.get("/workloads", headers=_op()).json() == []


def test_delete_workload_rejected_without_operator_token(client, registered):
    store.create_workload("workers", "uptime", 2, image="alpine")
    r = client.delete("/workloads/workers")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /events
# ---------------------------------------------------------------------------

def test_get_events(client, registered):
    r = client.get("/events", headers=_op())
    assert r.status_code == 200
    events = r.json()
    # Registration should have produced an event
    kinds = [e["kind"] for e in events]
    assert "node.registered" in kinds


def test_get_events_empty_initially(client):
    r = client.get("/events", headers=_op())
    assert r.status_code == 200
    assert r.json() == []


def test_get_events_rejected_without_operator_token(client):
    r = client.get("/events")
    assert r.status_code == 401
