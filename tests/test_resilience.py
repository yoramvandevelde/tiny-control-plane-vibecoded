import os
import sys
import time
import threading
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Startup grace period (api.py)
# ---------------------------------------------------------------------------

def _setup_store(tmp_path):
    os.chdir(tmp_path)
    from controller.store import init_db
    init_db(str(tmp_path / "cluster.db"))


def test_reconcile_skips_expire_during_grace_period(tmp_path, monkeypatch):
    """expire_lost_jobs should not be called within STARTUP_GRACE_SECONDS of startup."""
    _setup_store(tmp_path)

    import controller.api as api
    monkeypatch.setattr(api, "_startup_time", time.time())  # just started

    called = []
    monkeypatch.setattr(api, "expire_lost_jobs", lambda: called.append(True))

    api.reconcile_once()

    assert called == []


def test_reconcile_calls_expire_after_grace_period(tmp_path, monkeypatch):
    """expire_lost_jobs should be called once the grace period has elapsed."""
    _setup_store(tmp_path)

    import controller.api as api
    # Simulate startup 90 seconds ago — well past the grace period
    monkeypatch.setattr(api, "_startup_time", time.time() - 90)

    called = []
    monkeypatch.setattr(api, "expire_lost_jobs", lambda: called.append(True))

    api.reconcile_once()

    assert called == [True]


def test_grace_period_boundary(tmp_path, monkeypatch):
    """At exactly STARTUP_GRACE_SECONDS the grace period should have ended."""
    _setup_store(tmp_path)

    import controller.api as api
    monkeypatch.setattr(
        api, "_startup_time",
        time.time() - api.STARTUP_GRACE_SECONDS - 1
    )

    called = []
    monkeypatch.setattr(api, "expire_lost_jobs", lambda: called.append(True))

    api.reconcile_once()

    assert called == [True]


# ---------------------------------------------------------------------------
# Agent log buffer
# ---------------------------------------------------------------------------

# Set up argv before importing agent so argparse doesn't fail
sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.agent as agent_module


def _reset_buffer():
    with agent_module._log_buffer_lock:
        agent_module._log_buffer.clear()


def test_send_logs_buffers_on_failure(monkeypatch):
    _reset_buffer()

    def failing_post(*args, **kwargs):
        raise Exception("controller unreachable")

    monkeypatch.setattr(agent_module.httpx, "post", failing_post)

    agent_module.send_logs("job-1", "line one\nline two")

    with agent_module._log_buffer_lock:
        buffered = list(agent_module._log_buffer)

    assert len(buffered) == 2
    assert buffered[0] == ("job-1", "line one")
    assert buffered[1] == ("job-1", "line two")


def test_send_logs_does_not_buffer_on_success(monkeypatch):
    _reset_buffer()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    monkeypatch.setattr(agent_module.httpx, "post", lambda *a, **kw: mock_resp)

    agent_module.send_logs("job-1", "line one\nline two")

    with agent_module._log_buffer_lock:
        assert agent_module._log_buffer == []


def test_flush_log_buffer_ships_buffered_lines(monkeypatch):
    _reset_buffer()

    with agent_module._log_buffer_lock:
        agent_module._log_buffer.extend([
            ("job-1", "line one"),
            ("job-1", "line two"),
        ])

    shipped = []

    def fake_post(url, json=None, headers=None, timeout=None):
        shipped.append(json)
        return MagicMock(status_code=200)

    monkeypatch.setattr(agent_module.httpx, "post", fake_post)

    agent_module.flush_log_buffer()

    assert len(shipped) == 2
    assert shipped[0]["line"] == "line one"
    assert shipped[1]["line"] == "line two"

    with agent_module._log_buffer_lock:
        assert agent_module._log_buffer == []


def test_flush_log_buffer_stops_on_first_failure(monkeypatch):
    _reset_buffer()

    with agent_module._log_buffer_lock:
        agent_module._log_buffer.extend([
            ("job-1", "line one"),
            ("job-1", "line two"),
            ("job-1", "line three"),
        ])

    call_count = [0]

    def flaky_post(url, json=None, headers=None, timeout=None):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise Exception("still down")
        return MagicMock(status_code=200)

    monkeypatch.setattr(agent_module.httpx, "post", flaky_post)

    agent_module.flush_log_buffer()

    # Only the first line should have been removed
    with agent_module._log_buffer_lock:
        remaining = list(agent_module._log_buffer)

    assert len(remaining) == 2
    assert remaining[0] == ("job-1", "line two")


def test_flush_log_buffer_preserves_order(monkeypatch):
    _reset_buffer()

    lines = [(f"job-{i}", f"line {i}") for i in range(5)]
    with agent_module._log_buffer_lock:
        agent_module._log_buffer.extend(lines)

    received = []

    def fake_post(url, json=None, headers=None, timeout=None):
        received.append((json["job"], json["line"]))
        return MagicMock(status_code=200)

    monkeypatch.setattr(agent_module.httpx, "post", fake_post)

    agent_module.flush_log_buffer()

    assert received == lines


def test_flush_log_buffer_noop_when_empty(monkeypatch):
    _reset_buffer()

    called = []
    monkeypatch.setattr(agent_module.httpx, "post", lambda *a, **kw: called.append(True))

    agent_module.flush_log_buffer()

    assert called == []
