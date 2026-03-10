import subprocess
from unittest.mock import MagicMock
import sys
import os

# Ensure the agent module is importable without triggering argparse.
# --address is included to exercise explicit address handling in tests
# that need it; other tests are unaffected since address resolution only
# runs during registration.
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


def test_execute_shell_job():
    job = {"job": "test-job", "image": None, "command": "echo hello"}
    status, result = agent_module.execute(job)

    assert status == "succeeded"
    assert "hello" in result


def test_execute_shell_job_failed():
    job = {"job": "test-job", "image": None, "command": "exit 1"}
    status, result = agent_module.execute(job)

    assert status == "failed"


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


def test_execute_uses_shell_when_no_image(monkeypatch):
    calls = []

    def fake_run_docker(image, command, container_name=""):
        calls.append(("docker", image, command))
        return _mock_popen(b"output", 0)

    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    agent_module.execute({"job": "test-job", "image": None, "command": "echo shell"})
    assert len(calls) == 0


def test_execute_uses_docker_when_image_present(monkeypatch):
    calls = []

    def fake_run_docker(image, command, container_name=""):
        calls.append(("docker", image, command))
        return _mock_popen(b"output", 0)

    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    agent_module.execute({"job": "test-job", "image": "alpine", "command": "echo hello"})
    assert calls[0][0] == "docker"
    assert calls[0][1] == "alpine"
