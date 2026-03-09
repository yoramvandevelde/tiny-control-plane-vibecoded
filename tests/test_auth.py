import os
from controller.store import (
    init_db,
    register_node,
    verify_node_token,
    revoke_node,
    list_nodes,
)


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


def test_register_returns_token(tmp_path):
    _setup(tmp_path)

    token = register_node("node1", "http://localhost:9000")
    assert token is not None
    assert len(token) > 0


def test_verify_node_token_accepts_valid_token(tmp_path):
    _setup(tmp_path)

    token = register_node("node1", "http://localhost:9000")
    assert verify_node_token("node1", token) is True


def test_verify_node_token_rejects_wrong_token(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    assert verify_node_token("node1", "not-the-right-token") is False


def test_verify_node_token_rejects_unknown_node(tmp_path):
    _setup(tmp_path)

    assert verify_node_token("ghost-node", "any-token") is False


def test_reregister_issues_new_token(tmp_path):
    _setup(tmp_path)

    token1 = register_node("node1", "http://localhost:9000")
    token2 = register_node("node1", "http://localhost:9000")

    assert token1 != token2
    assert verify_node_token("node1", token2) is True
    assert verify_node_token("node1", token1) is False


def test_revoke_invalidates_token(tmp_path):
    _setup(tmp_path)

    token = register_node("node1", "http://localhost:9000")
    assert verify_node_token("node1", token) is True

    revoke_node("node1")
    assert verify_node_token("node1", token) is False


def test_revoke_does_not_remove_node(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    revoke_node("node1")

    assert "node1" in list_nodes()
