from typer.testing import CliRunner

import cli.tcp as tcp


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_nodes_command_shows_resource_capacity(monkeypatch):
    runner = CliRunner()

    monkeypatch.setenv("TCP_OPERATOR_TOKEN", "test-operator-token")
    monkeypatch.setattr(tcp, "API", "http://test-controller")

    def fake_get(url, headers=None):
        assert url == "http://test-controller/nodes"
        return _Resp(
            {
                "node-a": {
                    "healthy": True,
                    "version": "0.1.1",
                    "state": {"cpu": 0.25, "mem": 0.5},
                    "total_cpu": 8,
                    "total_mem_mb": 16384,
                    "address": "http://10.0.0.1:9000",
                }
            }
        )

    monkeypatch.setattr(tcp.httpx, "get", fake_get)

    result = runner.invoke(tcp.app, ["nodes"])
    assert result.exit_code == 0
    assert "capacity" in result.stdout
    assert "8 CPU / 16384 MiB" in result.stdout
