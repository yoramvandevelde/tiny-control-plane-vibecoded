"""
Edge case tests for store functions not covered elsewhere.
"""
import os

from controller.store import (
    init_db,
    register_node,
    create_job,
    start_job,
    list_jobs,
    list_nodes,
    get_pending_job,
    get_logs,
    store_log,
    pop_cancel_jobs,
    create_workload,
    list_workloads,
    update_workload_replicas,
    delete_workload,
    JobStatus,
)


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


# ---------------------------------------------------------------------------
# get_pending_job — empty table
# ---------------------------------------------------------------------------

def test_get_pending_job_returns_none_when_no_jobs(tmp_path):
    _setup(tmp_path)
    assert get_pending_job("node1") is None


def test_get_pending_job_returns_none_after_all_started(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    start_job(jid)

    assert get_pending_job("node1") is None


# ---------------------------------------------------------------------------
# list_nodes — empty table
# ---------------------------------------------------------------------------

def test_list_nodes_returns_empty_dict_when_none_registered(tmp_path):
    _setup(tmp_path)
    assert list_nodes() == {}


# ---------------------------------------------------------------------------
# pop_cancel_jobs — empty table
# ---------------------------------------------------------------------------

def test_pop_cancel_jobs_returns_empty_list_when_none_queued(tmp_path):
    _setup(tmp_path)
    assert pop_cancel_jobs("node1") == []


# ---------------------------------------------------------------------------
# update_workload_replicas — silent=True
# ---------------------------------------------------------------------------

def test_update_workload_replicas_silent_does_not_emit_event(tmp_path):
    _setup(tmp_path)

    from controller.store import list_events
    create_workload("workers", "uptime", 3)
    events_before = len(list_events())

    update_workload_replicas("workers", 0, silent=True)

    events_after = len(list_events())
    # Only the workload.created event should exist — no workload.scaled
    kinds = [e["kind"] for e in list_events()]
    assert "workload.scaled" not in kinds
    assert events_after == events_before


# ---------------------------------------------------------------------------
# create_workload — image and constraints persisted
# ---------------------------------------------------------------------------

def test_create_workload_persists_image(tmp_path):
    _setup(tmp_path)

    create_workload("containers", "echo hello", 2, image="alpine")

    w = list_workloads()[0]
    assert w["image"] == "alpine"


def test_create_workload_persists_constraints(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 2, constraints={"region": "homelab"})

    w = list_workloads()[0]
    assert w["constraints"]["region"] == "homelab"


def test_create_workload_persists_resources(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 2, resources={"cpu": 2})

    w = list_workloads()[0]
    assert w["resources"]["cpu"] == 2


def test_create_workload_defaults_to_empty_constraints(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 2)

    w = list_workloads()[0]
    assert w["constraints"] == {}
    assert w["resources"] == {}
    assert w["image"] is None


# ---------------------------------------------------------------------------
# list_workloads — empty table
# ---------------------------------------------------------------------------

def test_list_workloads_returns_empty_when_none(tmp_path):
    _setup(tmp_path)
    assert list_workloads() == []


# ---------------------------------------------------------------------------
# get_logs — ordering across interleaved jobs
# ---------------------------------------------------------------------------

def test_get_logs_ordering_preserved(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    for i in range(5):
        store_log(jid, f"line {i}")

    logs = get_logs(jid)
    assert [entry["line"] for entry in logs] == [f"line {i}" for i in range(5)]


def test_get_logs_isolated_across_jobs(tmp_path):
    _setup(tmp_path)

    jid1 = create_job("node1", "uptime")
    jid2 = create_job("node1", "uptime")

    # Interleave log writes from two jobs
    store_log(jid1, "job1 line 1")
    store_log(jid2, "job2 line 1")
    store_log(jid1, "job1 line 2")
    store_log(jid2, "job2 line 2")

    logs1 = [e["line"] for e in get_logs(jid1)]
    logs2 = [e["line"] for e in get_logs(jid2)]

    assert logs1 == ["job1 line 1", "job1 line 2"]
    assert logs2 == ["job2 line 1", "job2 line 2"]


def test_get_logs_returns_empty_for_unknown_job(tmp_path):
    _setup(tmp_path)
    assert get_logs("nonexistent-job-id") == []


# ---------------------------------------------------------------------------
# Agent helpers — parse_labels
# ---------------------------------------------------------------------------

def test_parse_labels_parses_key_value_pairs():
    import sys
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module

    result = agent_module.parse_labels(["region=homelab", "tier=worker"])
    assert result == {"region": "homelab", "tier": "worker"}


def test_parse_labels_handles_empty_input():
    import sys
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module

    assert agent_module.parse_labels(None) == {}
    assert agent_module.parse_labels([]) == {}


def test_parse_labels_handles_value_with_equals():
    """Values containing '=' should be handled correctly — only the first = splits."""
    import sys
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module

    result = agent_module.parse_labels(["key=val=ue"])
    assert result == {"key": "val=ue"}


# ---------------------------------------------------------------------------
# Agent helpers — token persistence
# ---------------------------------------------------------------------------

def test_save_and_load_token(tmp_path, monkeypatch):
    import sys
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module

    monkeypatch.setattr(agent_module.pathlib.Path, "home", lambda: tmp_path)

    agent_module._save_token("my-test-token")
    loaded = agent_module._load_token()
    assert loaded == "my-test-token"


def test_load_token_returns_none_when_missing(tmp_path, monkeypatch):
    import sys
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module

    monkeypatch.setattr(agent_module.pathlib.Path, "home", lambda: tmp_path)

    assert agent_module._load_token() is None


def test_save_token_sets_restricted_permissions(tmp_path, monkeypatch):
    import stat
    import sys
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module

    monkeypatch.setattr(agent_module.pathlib.Path, "home", lambda: tmp_path)

    agent_module._save_token("my-test-token")
    token_file = agent_module._token_path()
    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600


def test_load_token_returns_none_for_empty_file(tmp_path, monkeypatch):
    import sys
    sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
    import agent.agent as agent_module

    monkeypatch.setattr(agent_module.pathlib.Path, "home", lambda: tmp_path)

    # Write an empty token file
    agent_module._save_token("")
    assert agent_module._load_token() is None
