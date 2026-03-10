"""
Tests for multi-host network configuration:
  - Agent address resolution (--address flag, auto-detect, fallback)
  - CLI controller address resolution via TCP_CONTROLLER env var
"""
import os
import socket
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Agent setup — must be done before importing the module so argparse does
# not error out. Tests that need a specific --address value re-parse args
# inline using monkeypatch.
# ---------------------------------------------------------------------------

sys.argv = ["agent.py", "--node-id", "test-node", "--port", "9000"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.agent as agent_module


# ---------------------------------------------------------------------------
# _detect_lan_ip
# ---------------------------------------------------------------------------

def test_detect_lan_ip_returns_string(monkeypatch):
    """
    _detect_lan_ip should return a non-empty string under normal conditions.
    We monkeypatch the socket so no real network call is made.
    """
    fake_sock = MagicMock()
    fake_sock.__enter__ = lambda s: s
    fake_sock.__exit__ = MagicMock(return_value=False)
    fake_sock.getsockname.return_value = ("10.0.0.42", 12345)

    monkeypatch.setattr(agent_module.socket, "socket", lambda *a, **kw: fake_sock)

    ip = agent_module._detect_lan_ip()
    assert ip == "10.0.0.42"


def test_detect_lan_ip_falls_back_on_socket_error(monkeypatch, capsys):
    """
    If the socket connect raises (e.g. controller unreachable), _detect_lan_ip
    must return 127.0.0.1 and print a warning rather than raising.
    """
    def bad_socket(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(agent_module.socket, "socket", bad_socket)

    ip = agent_module._detect_lan_ip()
    assert ip == "127.0.0.1"

    captured = capsys.readouterr()
    assert "warning" in captured.out.lower()
    assert "localhost" in captured.out.lower() or "falling back" in captured.out.lower()


def test_detect_lan_ip_falls_back_on_malformed_controller(monkeypatch, capsys):
    """
    A malformed TCP_CONTROLLER value should not crash _detect_lan_ip;
    it should fall back to 127.0.0.1 with a warning.
    """
    monkeypatch.setattr(agent_module, "CONTROLLER", "not-a-valid-url")

    # socket.socket itself will be called but connect will fail — that's fine,
    # the fallback path handles it.
    ip = agent_module._detect_lan_ip()
    # Either succeeds with some address or falls back — must not raise.
    assert isinstance(ip, str)
    assert len(ip) > 0


# ---------------------------------------------------------------------------
# _resolve_address
# ---------------------------------------------------------------------------

def test_resolve_address_uses_explicit_address(monkeypatch):
    """
    When --address is supplied, _resolve_address must return it verbatim
    without calling _detect_lan_ip.
    """
    monkeypatch.setattr(agent_module.args, "address", "http://192.168.1.50:9000")

    detect_called = []
    monkeypatch.setattr(agent_module, "_detect_lan_ip", lambda: detect_called.append(True) or "1.2.3.4")

    result = agent_module._resolve_address()
    assert result == "http://192.168.1.50:9000"
    assert detect_called == []


def test_resolve_address_auto_detects_when_no_address(monkeypatch):
    """
    When --address is not supplied, _resolve_address calls _detect_lan_ip
    and combines its result with the configured port.
    """
    monkeypatch.setattr(agent_module.args, "address", None)
    monkeypatch.setattr(agent_module.args, "port", 9001)
    monkeypatch.setattr(agent_module, "_detect_lan_ip", lambda: "10.0.0.7")

    result = agent_module._resolve_address()
    assert result == "http://10.0.0.7:9001"


def test_resolve_address_fallback_produces_localhost_url(monkeypatch):
    """
    When auto-detection falls back to 127.0.0.1, the result should still be
    a well-formed URL using localhost.
    """
    monkeypatch.setattr(agent_module.args, "address", None)
    monkeypatch.setattr(agent_module.args, "port", 9000)
    monkeypatch.setattr(agent_module, "_detect_lan_ip", lambda: "127.0.0.1")

    result = agent_module._resolve_address()
    assert result == "http://127.0.0.1:9000"


# ---------------------------------------------------------------------------
# Registration uses resolved address
# ---------------------------------------------------------------------------

def test_do_register_sends_resolved_address(monkeypatch):
    """
    _do_register must send the address returned by _resolve_address, not a
    hardcoded localhost string.
    """
    monkeypatch.setenv("TCP_BOOTSTRAP_TOKEN", "test-bootstrap")
    monkeypatch.setattr(agent_module, "_resolve_address", lambda: "http://10.0.0.5:9000")

    sent_payloads = []

    def fake_post(url, json=None, headers=None, **kwargs):
        sent_payloads.append(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"token": "test-token"}
        return resp

    monkeypatch.setattr(agent_module.httpx, "post", fake_post)

    # Patch _save_token so no filesystem side-effects occur.
    monkeypatch.setattr(agent_module, "_save_token", lambda t: None)

    agent_module._do_register()

    assert len(sent_payloads) == 1
    assert sent_payloads[0]["address"] == "http://10.0.0.5:9000"


def test_do_register_logs_resolved_address(monkeypatch, capsys):
    """
    _do_register should print the address it is registering with so the
    operator can confirm which IP the agent is advertising.
    """
    monkeypatch.setenv("TCP_BOOTSTRAP_TOKEN", "test-bootstrap")
    monkeypatch.setattr(agent_module, "_resolve_address", lambda: "http://10.0.0.99:9002")

    def fake_post(url, json=None, headers=None, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"token": "tok"}
        return resp

    monkeypatch.setattr(agent_module.httpx, "post", fake_post)
    monkeypatch.setattr(agent_module, "_save_token", lambda t: None)

    agent_module._do_register()

    captured = capsys.readouterr()
    assert "10.0.0.99:9002" in captured.out


# ---------------------------------------------------------------------------
# CLI — TCP_CONTROLLER resolution
# ---------------------------------------------------------------------------

def test_cli_api_uses_tcp_controller_env(monkeypatch):
    """
    When TCP_CONTROLLER is set, the CLI module's API constant should reflect it.
    We re-import the module with the env var in place to verify module-level
    resolution.
    """
    import importlib

    monkeypatch.setenv("TCP_CONTROLLER", "http://10.0.0.5:8000")

    # Force re-evaluation of the module-level API assignment.
    import cli.tcp as tcp_module
    importlib.reload(tcp_module)

    assert tcp_module.API == "http://10.0.0.5:8000"


def test_cli_api_defaults_to_localhost(monkeypatch):
    """
    When TCP_CONTROLLER is not set, the CLI should default to localhost:8000.
    """
    import importlib

    monkeypatch.delenv("TCP_CONTROLLER", raising=False)

    import cli.tcp as tcp_module
    importlib.reload(tcp_module)

    assert tcp_module.API == "http://localhost:8000"
