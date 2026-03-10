"""
Tests for agent version reporting through registration and state polling.
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

import controller.store as store
import controller.api as api

BOOTSTRAP = "test-bootstrap-token"
OPERATOR  = "test-operator-token"


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


def _op():
    return {"X-Operator-Token": OPERATOR}


# ---------------------------------------------------------------------------
# Registration stores version
# ---------------------------------------------------------------------------

def test_register_with_version_stores_it(client):
    r = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000", "version": "1.2.3"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 200

    nodes = client.get("/nodes", headers=_op()).json()
    assert nodes["node1"]["version"] == "1.2.3"


def test_register_without_version_stores_none(client):
    r = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 200

    nodes = client.get("/nodes", headers=_op()).json()
    assert nodes["node1"]["version"] is None


# ---------------------------------------------------------------------------
# State polling updates version
# ---------------------------------------------------------------------------

def test_state_report_updates_version(client):
    # Register without a version first.
    token = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    ).json()["token"]

    # Post state with a version.
    client.post(
        "/state",
        json={"node": "node1", "cpu": 0.1, "mem": 0.1, "version": "0.1.0"},
        headers={"X-Node-Token": token},
    )

    nodes = client.get("/nodes", headers=_op()).json()
    assert nodes["node1"]["version"] == "0.1.0"


def test_state_report_without_version_does_not_clear_existing(client):
    # Register with a version.
    token = client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000", "version": "0.1.0"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    ).json()["token"]

    # Post state without a version field.
    client.post(
        "/state",
        json={"node": "node1", "cpu": 0.1, "mem": 0.1},
        headers={"X-Node-Token": token},
    )

    nodes = client.get("/nodes", headers=_op()).json()
    assert nodes["node1"]["version"] == "0.1.0"


# ---------------------------------------------------------------------------
# Multiple nodes with different versions
# ---------------------------------------------------------------------------

def test_multiple_nodes_have_independent_versions(client):
    client.post(
        "/register",
        json={"node": "node1", "address": "http://localhost:9000", "version": "0.1.0"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    client.post(
        "/register",
        json={"node": "node2", "address": "http://localhost:9001", "version": "0.2.0"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )

    nodes = client.get("/nodes", headers=_op()).json()
    assert nodes["node1"]["version"] == "0.1.0"
    assert nodes["node2"]["version"] == "0.2.0"


# ---------------------------------------------------------------------------
# Agent module has VERSION constant
# ---------------------------------------------------------------------------

def test_agent_module_exposes_version():
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module
    assert hasattr(agent_module, "VERSION")
    assert isinstance(agent_module.VERSION, str)
    assert len(agent_module.VERSION) > 0


def test_agent_collect_state_includes_version():
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module
    state = agent_module.collect_state()
    assert "version" in state
    assert state["version"] == agent_module.VERSION


def test_agent_register_payload_includes_version(monkeypatch):
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module
    from unittest.mock import MagicMock

    monkeypatch.setenv("TCP_BOOTSTRAP_TOKEN", "bootstrap")
    monkeypatch.setattr(agent_module, "_resolve_address", lambda: "http://10.0.0.1:9000")
    monkeypatch.setattr(agent_module, "_save_token", lambda t: None)
    monkeypatch.setattr(agent_module.psutil, "cpu_count", lambda logical=True: 8)

    class _Mem:
        total = 16 * 1024 * 1024 * 1024

    monkeypatch.setattr(agent_module.psutil, "virtual_memory", lambda: _Mem())

    sent = []

    def fake_post(url, json=None, headers=None, **kwargs):
        sent.append(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"token": "tok"}
        return resp

    monkeypatch.setattr(agent_module.httpx, "post", fake_post)

    agent_module._do_register()

    assert sent[0]["version"] == agent_module.VERSION
    assert sent[0]["total_cpu"] == 8
    assert sent[0]["total_mem_mb"] == 16384


# ---------------------------------------------------------------------------
# DB migration — existing nodes table gains version column
# ---------------------------------------------------------------------------

def test_init_db_migrates_existing_table_without_version(tmp_path):
    """
    If an existing cluster.db lacks the version column, init_db must add it
    without losing existing data.
    """
    import sqlite3

    db_path = str(tmp_path / "cluster.db")

    # Create a legacy-style nodes table without the version column.
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS nodes")
    conn.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, address TEXT, labels TEXT, capacity TEXT,
            healthy INTEGER, last_seen REAL, state_json TEXT, token TEXT
        )
    """)
    conn.execute(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)",
        ("node1", "http://localhost:9000", "{}", "{}", 1, 0.0, None, "tok"),
    )
    conn.commit()
    conn.close()

    # init_db should migrate without error.
    store._local.conn = None
    store.init_db(db_path)

    # The existing row should still be there, with version=None.
    store._local.conn = None
    store.init_db(db_path)
    nodes = store.list_nodes()
    assert "node1" in nodes
    assert nodes["node1"]["version"] is None
