import os
import time

from controller.store import (
    init_db,
    record_event,
    list_events,
    get_events_since,
    register_node,
    revoke_node,
    create_job,
    start_job,
    finish_job,
    mark_cancelled,
    expire_lost_jobs,
    create_workload,
    delete_workload,
    get_db,
    _db_lock,
    JobStatus,
)


def _setup(tmp_path):
    os.chdir(tmp_path)
    init_db(str(tmp_path / "cluster.db"))


# ---------------------------------------------------------------------------
# record_event / list_events
# ---------------------------------------------------------------------------

def test_record_event_stores_entry(tmp_path):
    _setup(tmp_path)

    record_event("test.kind", "something happened")

    events = list_events()
    assert len(events) == 1
    assert events[0]["kind"] == "test.kind"
    assert events[0]["message"] == "something happened"


def test_record_event_stores_timestamp(tmp_path):
    _setup(tmp_path)

    before = time.time()
    record_event("test.kind", "msg")
    after = time.time()

    events = list_events()
    assert before <= events[0]["ts"] <= after


def test_list_events_returns_chronological_order(tmp_path):
    _setup(tmp_path)

    record_event("first",  "one")
    record_event("second", "two")
    record_event("third",  "three")

    events = list_events()
    kinds = [e["kind"] for e in events]
    assert kinds == ["first", "second", "third"]


def test_list_events_respects_limit(tmp_path):
    _setup(tmp_path)

    for i in range(10):
        record_event("kind", f"msg {i}")

    events = list_events(limit=3)
    assert len(events) == 3
    # Should return the most recent 3 in chronological order
    assert events[-1]["message"] == "msg 9"


def test_list_events_default_limit(tmp_path):
    _setup(tmp_path)

    for i in range(205):
        record_event("kind", f"msg {i}")

    events = list_events()
    assert len(events) == 200


def test_list_events_returns_id(tmp_path):
    _setup(tmp_path)

    record_event("kind", "msg")
    events = list_events()
    assert "id" in events[0]
    assert events[0]["id"] > 0


# ---------------------------------------------------------------------------
# get_events_since
# ---------------------------------------------------------------------------

def test_get_events_since_returns_newer_events(tmp_path):
    _setup(tmp_path)

    record_event("first",  "one")
    record_event("second", "two")

    events = list_events()
    first_id = events[-1]["id"]

    record_event("third", "three")

    newer = get_events_since(first_id)
    assert len(newer) == 1
    assert newer[0]["kind"] == "third"


def test_get_events_since_returns_all_after_cursor(tmp_path):
    _setup(tmp_path)

    for i in range(5):
        record_event("kind", f"msg {i}")

    events = list_events()
    cursor = events[2]["id"]  # after the third event

    newer = get_events_since(cursor)
    assert len(newer) == 2
    assert newer[0]["message"] == "msg 3"
    assert newer[1]["message"] == "msg 4"


def test_get_events_since_returns_empty_when_nothing_new(tmp_path):
    _setup(tmp_path)

    record_event("kind", "msg")
    events = list_events()
    last_id = events[-1]["id"]

    assert get_events_since(last_id) == []


def test_get_events_since_zero_returns_all(tmp_path):
    _setup(tmp_path)

    record_event("first",  "one")
    record_event("second", "two")

    events = get_events_since(0)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Events emitted by lifecycle operations
# ---------------------------------------------------------------------------

def test_register_node_emits_event(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")

    events = list_events()
    kinds = [e["kind"] for e in events]
    assert "node.registered" in kinds


def test_revoke_node_emits_event(tmp_path):
    _setup(tmp_path)

    register_node("node1", "http://localhost:9000")
    revoke_node("node1")

    kinds = [e["kind"] for e in list_events()]
    assert "node.revoked" in kinds


def test_create_job_emits_scheduled_event(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")

    kinds = [e["kind"] for e in list_events()]
    assert "job.scheduled" in kinds


def test_start_job_emits_started_event(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    start_job(jid)

    kinds = [e["kind"] for e in list_events()]
    assert "job.started" in kinds


def test_finish_job_succeeded_emits_event(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    start_job(jid)
    finish_job(jid, JobStatus.SUCCEEDED, "ok")

    kinds = [e["kind"] for e in list_events()]
    assert "job.succeeded" in kinds


def test_finish_job_failed_emits_event(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "uptime")
    start_job(jid)
    finish_job(jid, JobStatus.FAILED, "error")

    kinds = [e["kind"] for e in list_events()]
    assert "job.failed" in kinds


def test_mark_cancelled_emits_cancelled_event(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "sleep 100")
    start_job(jid)
    mark_cancelled(jid, "node1")

    kinds = [e["kind"] for e in list_events()]
    assert "job.cancelled" in kinds


def test_mark_cancelled_event_includes_previous_state(tmp_path):
    _setup(tmp_path)

    jid = create_job("node1", "sleep 100")
    start_job(jid)
    mark_cancelled(jid, "node1")

    events = list_events()
    cancelled = next(e for e in events if e["kind"] == "job.cancelled")
    assert "running" in cancelled["message"]
    assert "node1" in cancelled["message"]


def test_expire_lost_jobs_emits_lost_event(tmp_path):
    _setup(tmp_path)

    from controller.store import _db_lock
    jid = create_job("node1", "sleep 100")
    start_job(jid)

    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET lease_expires=? WHERE id=?",
            (time.time() - 1, jid),
        )
        get_db().commit()

    expire_lost_jobs()

    kinds = [e["kind"] for e in list_events()]
    assert "job.lost" in kinds


def test_create_workload_emits_event(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 3)

    kinds = [e["kind"] for e in list_events()]
    assert "workload.created" in kinds


def test_delete_workload_emits_removed_event(tmp_path):
    _setup(tmp_path)

    create_workload("workers", "uptime", 3)
    delete_workload("workers")

    kinds = [e["kind"] for e in list_events()]
    assert "workload.removed" in kinds


def test_finish_job_does_not_emit_event_when_already_terminal(tmp_path):
    """Late agent results on a cancelled job should not produce a duplicate event."""
    _setup(tmp_path)

    jid = create_job("node1", "sleep 100")
    start_job(jid)
    mark_cancelled(jid, "node1")

    events_before = len(list_events())
    finish_job(jid, JobStatus.SUCCEEDED, "output")
    events_after = len(list_events())

    assert events_before == events_after
