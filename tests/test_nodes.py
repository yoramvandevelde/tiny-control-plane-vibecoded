from controller.store import init_db, register_node, list_nodes
import os


def test_node_registration(tmp_path):

    os.chdir(tmp_path)

    init_db()

    register_node(
        "node1",
        "http://localhost:9000",
        {"region": "eu"},
        {"cpu": 4, "mem": 8192},
    )

    nodes = list_nodes()

    assert "node1" in nodes
    assert nodes["node1"]["labels"]["region"] == "eu"
