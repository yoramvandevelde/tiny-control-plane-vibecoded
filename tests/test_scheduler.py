import os
import time
from controller.store import init_db, register_node, update_state, create_job, start_job, JobStatus
from controller.api import pick_node


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def _nodes():
    from controller.store import list_nodes
    return list_nodes()


def test_pick_node_returns_healthy_node(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    update_state("node1", {"cpu": 0.3, "mem": 0.4})

    node = pick_node(_nodes(), {}, {})
    assert node == "node1"


def test_pick_node_excludes_unhealthy_node(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    update_state("node1", {"cpu": 0.95, "mem": 0.4})  # unhealthy

    node = pick_node(_nodes(), {}, {})
    assert node is None


def test_pick_node_excludes_stale_node(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    from controller.store import get_db, _db_lock
    import json
    with _db_lock:
        get_db().execute(
            "UPDATE nodes SET state_json=?, last_seen=?, healthy=1 WHERE id=?",
            (json.dumps({"cpu": 0.1, "mem": 0.1}), time.time() - 60, "node1")
        )
        get_db().commit()

    node = pick_node(_nodes(), {}, {})
    assert node is None


def test_pick_node_respects_label_constraints(tmp_path):
    _setup(tmp_path)

    register_node("eu-node", "http://localhost:9000", labels={"region": "eu"})
    register_node("us-node", "http://localhost:9001", labels={"region": "us"})
    update_state("eu-node", {"cpu": 0.3, "mem": 0.4})
    update_state("us-node", {"cpu": 0.3, "mem": 0.4})

    node = pick_node(_nodes(), {"region": "eu"}, {})
    assert node == "eu-node"


def test_pick_node_returns_none_when_no_label_match(tmp_path):
    _setup(tmp_path)

    register_node("us-node", "http://localhost:9001", labels={"region": "us"})
    update_state("us-node", {"cpu": 0.3, "mem": 0.4})

    node = pick_node(_nodes(), {"region": "eu"}, {})
    assert node is None


def test_pick_node_selects_least_loaded_by_cpu(tmp_path):
    """When job counts are equal, the node with lower CPU is preferred."""
    _setup(tmp_path)

    register_node("busy-node", "http://localhost:9000")
    register_node("idle-node", "http://localhost:9001")
    update_state("busy-node", {"cpu": 0.8, "mem": 0.4})
    update_state("idle-node", {"cpu": 0.1, "mem": 0.4})

    node = pick_node(_nodes(), {}, {})
    assert node == "idle-node"


def test_pick_node_prefers_node_with_fewer_jobs(tmp_path):
    """Job count takes priority over CPU when nodes differ in active jobs."""
    _setup(tmp_path)

    register_node("loaded-node", "http://localhost:9000")
    register_node("free-node", "http://localhost:9001")
    # loaded-node reports lower CPU but has more active jobs
    update_state("loaded-node", {"cpu": 0.1, "mem": 0.4})
    update_state("free-node", {"cpu": 0.5, "mem": 0.4})

    # Assign two active jobs to loaded-node
    jid1 = create_job("loaded-node", "sleep 100")
    jid2 = create_job("loaded-node", "sleep 100")
    start_job(jid1)
    start_job(jid2)

    node = pick_node(_nodes(), {}, {})
    assert node == "free-node"


def test_pick_node_returns_none_with_no_nodes(tmp_path):
    _setup(tmp_path)

    node = pick_node({}, {}, {})
    assert node is None
