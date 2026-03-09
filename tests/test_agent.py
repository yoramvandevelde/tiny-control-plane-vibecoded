import subprocess
from unittest.mock import patch, MagicMock
import sys
import os

# Ensure the agent module is importable without triggering argparse
sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.agent as agent_module


def test_execute_shell_job():
    job = {"image": None, "command": "echo hello"}
    status, result = agent_module.execute(job)

    assert status == "finished"
    assert "hello" in result


def test_execute_shell_job_failed():
    job = {"image": None, "command": "exit 1"}
    status, result = agent_module.execute(job)

    assert status == "failed"


def test_execute_docker_job(monkeypatch):
    def fake_check_output(cmd, stderr):
        assert "docker" in cmd
        assert "run" in cmd
        assert "--rm" in cmd
        assert "--network" in cmd
        assert "none" in cmd
        assert "alpine" in cmd
        assert "echo" in cmd
        return b"hello\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    job = {"image": "alpine", "command": "echo hello"}
    status, result = agent_module.execute(job)

    assert status == "finished"
    assert result == "hello\n"


def test_execute_docker_job_failed(monkeypatch):
    def fake_check_output(cmd, stderr):
        raise subprocess.CalledProcessError(1, cmd, output=b"container failed")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    job = {"image": "alpine", "command": "bad-command"}
    status, result = agent_module.execute(job)

    assert status == "failed"
    assert "container failed" in result


def test_execute_uses_shell_when_no_image(monkeypatch):
    calls = []

    def fake_run_shell(command):
        calls.append(("shell", command))
        return "output"

    def fake_run_docker(image, command):
        calls.append(("docker", image, command))
        return "output"

    monkeypatch.setattr(agent_module, "run_shell", fake_run_shell)
    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    agent_module.execute({"image": None, "command": "uptime"})
    assert calls[0][0] == "shell"


def test_execute_uses_docker_when_image_present(monkeypatch):
    calls = []

    def fake_run_shell(command):
        calls.append(("shell", command))
        return "output"

    def fake_run_docker(image, command):
        calls.append(("docker", image, command))
        return "output"

    monkeypatch.setattr(agent_module, "run_shell", fake_run_shell)
    monkeypatch.setattr(agent_module, "run_docker", fake_run_docker)

    agent_module.execute({"image": "alpine", "command": "echo hello"})
    assert calls[0][0] == "docker"
    assert calls[0][1] == "alpine"
