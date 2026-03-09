import os
import time
from controller.store import init_db, register_node, list_nodes, update_state


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_node_registration(tmp_path):
    _setup(tmp_path)

    _ = register_node(
        "node1",
        "http://localhost:9000",
        {"region": "eu"},
        {"cpu": 4, "mem": 8192},
    )

    nodes = list_nodes()
    assert "node1" in nodes
    assert nodes["node1"]["labels"]["region"] == "eu"
    assert nodes["node1"]["capacity"]["cpu"] == 4
    assert nodes["node1"]["healthy"] is True


def test_node_registration_without_labels(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")

    nodes = list_nodes()
    assert nodes["node1"]["labels"] == {}
    assert nodes["node1"]["capacity"] == {}


def test_node_replace_on_reregister(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000", labels={"region": "eu"})
    _ = register_node("node1", "http://localhost:9000", labels={"region": "us"})

    nodes = list_nodes()
    assert len(nodes) == 1
    assert nodes["node1"]["labels"]["region"] == "us"


def test_update_state_marks_healthy(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    update_state("node1", {"node": "node1", "cpu": 0.3, "mem": 0.5, "disk_free": 0.8})

    nodes = list_nodes()
    assert nodes["node1"]["healthy"] is True
    assert nodes["node1"]["state"]["cpu"] == 0.3


def test_update_state_marks_unhealthy_on_high_cpu(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    update_state("node1", {"node": "node1", "cpu": 0.95, "mem": 0.5, "disk_free": 0.8})

    nodes = list_nodes()
    assert nodes["node1"]["healthy"] is False


def test_update_state_marks_unhealthy_on_high_mem(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    update_state("node1", {"node": "node1", "cpu": 0.3, "mem": 0.95, "disk_free": 0.8})

    nodes = list_nodes()
    assert nodes["node1"]["healthy"] is False


def test_last_seen_is_updated(tmp_path):
    _setup(tmp_path)

    _ = register_node("node1", "http://localhost:9000")
    before = time.time()
    update_state("node1", {"node": "node1", "cpu": 0.1, "mem": 0.1, "disk_free": 0.9})
    after = time.time()

    nodes = list_nodes()
    assert before <= nodes["node1"]["last_seen"] <= after
