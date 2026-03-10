import subprocess
from unittest.mock import MagicMock
import sys
import os

# Ensure the agent module is importable without triggering argparse.
sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.agent as agent_module


def _mock_popen(output: bytes, returncode: int) -> MagicMock:
    """Build a minimal Popen mock that communicate() works on."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.communicate.return_value = (output, b"")
    proc.returncode = returncode
    proc._container_name = "tcp-test"
    return proc


def test_execute_docker_job(monkeypatch):
    proc = _mock_popen(b"hello\n", 0)

    def fake_run_docker(image, command, container_name=""):
        assert image == "alpine"
        assert "echo" in command
        return proc

    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    job = {"job": "test-job", "image": "alpine", "command": "echo hello"}
    status, result = agent_module.execute(job)

    assert status == "succeeded"
    assert result == "hello\n"


def test_execute_docker_job_failed(monkeypatch):
    proc = _mock_popen(b"container failed", 1)

    def fake_run_docker(image, command, container_name=""):
        return proc

    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    job = {"job": "test-job", "image": "alpine", "command": "bad-command"}
    status, result = agent_module.execute(job)

    assert status == "failed"
    assert "container failed" in result


def test_execute_always_uses_docker(monkeypatch):
    """Every job must go through run_docker regardless of the command."""
    calls = []

    def fake_run_docker(image, command, container_name=""):
        calls.append((image, command))
        return _mock_popen(b"output", 0)

    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    agent_module.execute({"job": "test-job", "image": "alpine", "command": "echo hello"})
    assert len(calls) == 1
    assert calls[0][0] == "alpine"


def test_execute_container_name_derived_from_job_id(monkeypatch):
    """Container name must be 'tcp-' + first 16 chars of job ID."""
    container_names = []

    def fake_run_docker(image, command, container_name=""):
        container_names.append(container_name)
        return _mock_popen(b"", 0)

    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    job_id = "abcdef1234567890xxxx"
    agent_module.execute({"job": job_id, "image": "alpine", "command": "true"})

    assert container_names[0] == f"tcp-{job_id[:16]}"


def test_kill_job_calls_docker_stop(monkeypatch):
    """kill_job must invoke 'docker stop <container_name>'."""
    stopped = []

    def fake_run(cmd, timeout=None):
        stopped.append(cmd)

    monkeypatch.setattr(agent_module.subprocess, "run", fake_run)

    job_id         = "test-kill-job"
    container_name = f"tcp-{job_id[:16]}"

    with agent_module._processes_lock:
        agent_module._processes[job_id] = container_name

    agent_module.kill_job(job_id)

    assert stopped == [["docker", "stop", container_name]]


def test_kill_job_noop_when_not_running(monkeypatch):
    """kill_job on an unknown job ID must not raise or call docker stop."""
    stopped = []
    monkeypatch.setattr(agent_module.subprocess, "run", lambda cmd, **kw: stopped.append(cmd))

    agent_module.kill_job("nonexistent-job-id")

    assert stopped == []
