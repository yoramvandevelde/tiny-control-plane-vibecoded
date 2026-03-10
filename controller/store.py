import json
import sqlite3
import threading
import time
import uuid

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

_local   = threading.local()
_db_lock = threading.Lock()

DB_PATH = "cluster.db"

# How long a running job's lease is valid before it is considered lost.
LEASE_SECONDS = 60


def get_db() -> sqlite3.Connection:
    """Return a per-thread SQLite connection, (re)opening it when necessary."""
    path = DB_PATH
    if not hasattr(_local, "conn") or _local.conn is None or _local.db_path != path:
        if hasattr(_local, "conn") and _local.conn is not None:
            _local.conn.close()
        _local.conn = sqlite3.connect(path, check_same_thread=True)
        _local.conn.row_factory = sqlite3.Row
        _local.db_path = path
    return _local.conn


def init_db(path: str = None):
    """Initialise the database, creating tables if they do not exist."""
    global DB_PATH
    if path:
        DB_PATH = str(path)
    _local.conn = None

    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            id         TEXT PRIMARY KEY,
            address    TEXT,
            labels     TEXT,
            capacity   TEXT,
            healthy    INTEGER,
            last_seen  REAL,
            state_json TEXT,
            token      TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id            TEXT PRIMARY KEY,
            node_id       TEXT,
            command       TEXT,
            image         TEXT,
            workload_name TEXT,
            constraints   TEXT,
            resources     TEXT,
            status        TEXT,
            result        TEXT,
            created       REAL,
            updated       REAL,
            lease_expires REAL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS workloads (
            name        TEXT PRIMARY KEY,
            command     TEXT,
            image       TEXT,
            replicas    INTEGER,
            constraints TEXT,
            resources   TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            ts     REAL,
            line   TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS cancel_jobs (
            job_id  TEXT PRIMARY KEY,
            node_id TEXT,
            created REAL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL,
            kind    TEXT,
            message TEXT
        )
    """)

    db.commit()


# ---------------------------------------------------------------------------
# Job status constants
# ---------------------------------------------------------------------------

class JobStatus:
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"
    LOST      = "lost"

    # Jobs in a terminal state will not be updated by finish_job.
    TERMINAL = {SUCCEEDED, FAILED, LOST}

    # Jobs in an active state count toward workload replica targets.
    ACTIVE   = {PENDING, RUNNING}


# ---------------------------------------------------------------------------
# Node operations
# ---------------------------------------------------------------------------

def register_node(node_id: str, address: str, labels: dict = None, capacity: dict = None) -> str:
    """
    Register or re-register a node. Generates a fresh per-node token and
    returns it — the agent must include this token in all subsequent requests.
    """
    token = str(uuid.uuid4())
    with _db_lock:
        get_db().execute(
            "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?)",
            (
                node_id,
                address,
                json.dumps(labels or {}),
                json.dumps(capacity or {}),
                1,
                time.time(),
                None,
                token,
            ),
        )
        get_db().commit()
    record_event("node.registered", f"node {node_id} registered at {address}")
    return token


def verify_node_token(node_id: str, token: str) -> bool:
    """Return True if the token matches the stored token for the given node."""
    row = get_db().execute(
        "SELECT token FROM nodes WHERE id=?", (node_id,)
    ).fetchone()
    if row is None:
        return False
    return row[0] == token


def revoke_node(node_id: str):
    """
    Invalidate a node's token by setting it to NULL.
    The node record is kept so its job history remains visible.
    """
    with _db_lock:
        get_db().execute("UPDATE nodes SET token=NULL WHERE id=?", (node_id,))
        get_db().commit()
    record_event("node.revoked", f"node {node_id} revoked")


def update_state(node_id: str, state: dict):
    """
    Update a node's reported state (CPU, memory, etc.) and mark it as seen.
    A node is considered healthy when CPU and memory are both below 90%.
    """
    cpu     = state.get("cpu", 0)
    mem     = state.get("mem", 0)
    healthy = int(cpu < 0.9 and mem < 0.9)
    with _db_lock:
        get_db().execute(
            "UPDATE nodes SET state_json=?, last_seen=?, healthy=? WHERE id=?",
            (json.dumps(state), time.time(), healthy, node_id),
        )
        get_db().commit()


def list_nodes() -> dict:
    """Return all registered nodes keyed by node ID."""
    rows = get_db().execute(
        "SELECT id, address, labels, capacity, healthy, state_json, last_seen FROM nodes"
    ).fetchall()

    return {
        r[0]: {
            "address":   r[1],
            "labels":    json.loads(r[2]) if r[2] else {},
            "capacity":  json.loads(r[3]) if r[3] else {},
            "healthy":   bool(r[4]),
            "state":     json.loads(r[5]) if r[5] else {},
            "last_seen": r[6],
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# Job operations
# ---------------------------------------------------------------------------

def create_job(
    node_id:       str,
    command:       str,
    image:         str = None,
    workload_name: str = None,
) -> str:
    """Create a new job in PENDING state and return its UUID."""
    jid = str(uuid.uuid4())
    with _db_lock:
        get_db().execute(
            """
            INSERT INTO jobs (
                id, node_id, command, image, workload_name,
                constraints, resources, status, result, created, updated, lease_expires
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jid,
                node_id,
                command,
                image,
                workload_name,
                "{}",
                "{}",
                JobStatus.PENDING,
                None,
                time.time(),
                time.time(),
                None,
            ),
        )
        get_db().commit()
    workload_part = f" ({workload_name})" if workload_name else ""
    record_event("job.scheduled", f"job {jid} scheduled on {node_id}{workload_part}")
    return jid


def get_pending_job(node_id: str):
    """Return the oldest PENDING job for a node, or None if there is none."""
    return get_db().execute(
        "SELECT id, command, image FROM jobs WHERE node_id=? AND status=? LIMIT 1",
        (node_id, JobStatus.PENDING),
    ).fetchone()


def start_job(job_id: str):
    """Transition a job from PENDING to RUNNING and set its initial lease."""
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status=?, updated=?, lease_expires=? WHERE id=?",
            (JobStatus.RUNNING, time.time(), time.time() + LEASE_SECONDS, job_id),
        )
        get_db().commit()
    record_event("job.started", f"job {job_id} started")


def renew_lease(job_id: str):
    """Extend the lease of a RUNNING job. Called by the agent heartbeat."""
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET lease_expires=? WHERE id=? AND status=?",
            (time.time() + LEASE_SECONDS, job_id, JobStatus.RUNNING),
        )
        get_db().commit()


def finish_job(job_id: str, status: str, result: str):
    """
    Record the final status and output of a job.
    If the job is already in a terminal state (e.g. marked lost by the
    reconciler before the agent posted its result), this is a no-op.
    """
    if status == "finished":
        status = JobStatus.SUCCEEDED
    with _db_lock:
        placeholders = ",".join("?" * len(JobStatus.TERMINAL))
        cur = get_db().execute(
            f"UPDATE jobs SET status=?, result=?, updated=?, lease_expires=NULL "
            f"WHERE id=? AND status NOT IN ({placeholders})",
            (status, result, time.time(), job_id, *JobStatus.TERMINAL),
        )
        get_db().commit()
    if cur.rowcount > 0:
        record_event(f"job.{status}", f"job {job_id} {status}")


def mark_lost(job_id: str):
    """Unconditionally mark a job as LOST, regardless of its current state."""
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status=?, updated=? WHERE id=?",
            (JobStatus.LOST, time.time(), job_id),
        )
        get_db().commit()
    record_event("job.lost", f"job {job_id} marked lost")


def mark_cancelled(job_id: str, node_id: str):
    """Mark a job as LOST due to an explicit operator cancellation."""
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status=?, updated=? WHERE id=?",
            (JobStatus.LOST, time.time(), job_id),
        )
        get_db().commit()
    record_event("job.cancelled", f"job {job_id} cancelled on {node_id}")


def expire_lost_jobs():
    """Mark all RUNNING jobs whose lease has expired as LOST."""
    now = time.time()
    with _db_lock:
        # Fetch expired job IDs before updating so we can record individual events.
        expired = get_db().execute(
            "SELECT id FROM jobs WHERE status=? AND lease_expires IS NOT NULL AND lease_expires < ?",
            (JobStatus.RUNNING, now),
        ).fetchall()
        get_db().execute(
            """
            UPDATE jobs SET status=?, updated=?
            WHERE status=? AND lease_expires IS NOT NULL AND lease_expires < ?
            """,
            (JobStatus.LOST, now, JobStatus.RUNNING, now),
        )
        get_db().commit()
    for row in expired:
        record_event("job.lost", f"job {row[0]} lease expired")


def list_jobs() -> list:
    """Return all jobs."""
    rows = get_db().execute(
        "SELECT id, node_id, command, image, workload_name, status, result, created, updated FROM jobs"
    ).fetchall()

    return [
        {
            "id":            r[0],
            "node":          r[1],
            "command":       r[2],
            "image":         r[3],
            "workload_name": r[4],
            "status":        r[5],
            "result":        r[6],
            "created":       r[7],
            "updated":       r[8],
        }
        for r in rows
    ]


def count_active_node_jobs(node_id: str) -> int:
    """Count PENDING + RUNNING jobs assigned to a node. Used by the scheduler."""
    placeholders = ",".join("?" * len(JobStatus.ACTIVE))
    row = get_db().execute(
        f"SELECT COUNT(*) FROM jobs WHERE node_id=? AND status IN ({placeholders})",
        (node_id, *JobStatus.ACTIVE),
    ).fetchone()
    return row[0]


def count_active_workload_jobs(workload_name: str) -> int:
    """Count PENDING + RUNNING jobs belonging to a workload."""
    placeholders = ",".join("?" * len(JobStatus.ACTIVE))
    row = get_db().execute(
        f"SELECT COUNT(*) FROM jobs WHERE workload_name=? AND status IN ({placeholders})",
        (workload_name, *JobStatus.ACTIVE),
    ).fetchone()
    return row[0]


def get_excess_workload_jobs(workload_name: str, keep: int) -> list:
    """
    Return (job_id, node_id) tuples for active jobs that exceed the desired
    replica count. PENDING jobs are selected before RUNNING ones to minimise
    disruption to already-executing work.
    """
    placeholders = ",".join("?" * len(JobStatus.ACTIVE))
    rows = get_db().execute(
        f"""
        SELECT id, node_id FROM jobs
        WHERE workload_name=? AND status IN ({placeholders})
        ORDER BY
            CASE status WHEN 'pending' THEN 0 ELSE 1 END,
            created DESC
        """,
        (workload_name, *JobStatus.ACTIVE),
    ).fetchall()

    excess = len(rows) - keep
    if excess <= 0:
        return []
    return [(r[0], r[1]) for r in rows[:excess]]


# ---------------------------------------------------------------------------
# Workload operations
# ---------------------------------------------------------------------------

def create_workload(
    name:        str,
    command:     str,
    replicas:    int,
    image:       str  = None,
    constraints: dict = None,
    resources:   dict = None,
):
    """Create or replace a workload definition."""
    with _db_lock:
        get_db().execute(
            "INSERT OR REPLACE INTO workloads VALUES (?,?,?,?,?,?)",
            (name, command, image, replicas, json.dumps(constraints or {}), json.dumps(resources or {})),
        )
        get_db().commit()
    record_event("workload.created", f"workload {name} created replicas={replicas}")


def update_workload_replicas(name: str, replicas: int, silent: bool = False) -> bool:
    """Update the replica count for a workload. Returns False if not found.
    Pass silent=True to suppress the event (used internally during undeploy)."""
    with _db_lock:
        cur = get_db().execute(
            "UPDATE workloads SET replicas=? WHERE name=?", (replicas, name)
        )
        get_db().commit()
    if cur.rowcount > 0 and not silent:
        record_event("workload.scaled", f"workload {name} scaled to replicas={replicas}")
    return cur.rowcount > 0


def delete_workload(name: str):
    """Remove a workload definition. Does not affect existing jobs."""
    with _db_lock:
        get_db().execute("DELETE FROM workloads WHERE name=?", (name,))
        get_db().commit()
    record_event("workload.removed", f"workload {name} removed")


def list_workloads() -> list:
    """Return all workload definitions."""
    rows = get_db().execute(
        "SELECT name, command, image, replicas, constraints, resources FROM workloads"
    ).fetchall()

    return [
        {
            "name":        r[0],
            "command":     r[1],
            "image":       r[2],
            "replicas":    r[3],
            "constraints": json.loads(r[4]) if r[4] else {},
            "resources":   json.loads(r[5]) if r[5] else {},
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Cancellation queue
# ---------------------------------------------------------------------------

def enqueue_cancel(job_id: str, node_id: str):
    """
    Record that a job should be cancelled on its node.
    The agent drains this queue on every poll cycle and kills the process.
    """
    with _db_lock:
        get_db().execute(
            "INSERT OR REPLACE INTO cancel_jobs(job_id, node_id, created) VALUES (?,?,?)",
            (job_id, node_id, time.time()),
        )
        get_db().commit()


def pop_cancel_jobs(node_id: str) -> list:
    """
    Return and atomically remove all pending cancellations for a node.
    Exactly-once delivery: if the agent misses a cycle it will not receive
    the same cancellations again.
    """
    with _db_lock:
        rows = get_db().execute(
            "SELECT job_id FROM cancel_jobs WHERE node_id=?", (node_id,)
        ).fetchall()
        if rows:
            placeholders = ",".join("?" * len(rows))
            job_ids = [r[0] for r in rows]
            get_db().execute(
                f"DELETE FROM cancel_jobs WHERE job_id IN ({placeholders})", job_ids
            )
            get_db().commit()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Log operations
# ---------------------------------------------------------------------------

def store_log(job_id: str, line: str):
    """Append a single log line for a job."""
    with _db_lock:
        get_db().execute(
            "INSERT INTO logs(job_id, ts, line) VALUES (?,?,?)",
            (job_id, time.time(), line),
        )
        get_db().commit()


def get_logs(job_id: str) -> list:
    """Return all log lines for a job in insertion order."""
    rows = get_db().execute(
        "SELECT ts, line FROM logs WHERE job_id=? ORDER BY id", (job_id,)
    ).fetchall()
    return [{"ts": r[0], "line": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

def record_event(kind: str, message: str):
    """Append a structured event to the event log."""
    with _db_lock:
        get_db().execute(
            "INSERT INTO events(ts, kind, message) VALUES (?,?,?)",
            (time.time(), kind, message),
        )
        get_db().commit()


def list_events(limit: int = 200) -> list:
    """Return the most recent events in chronological order."""
    rows = get_db().execute(
        "SELECT id, ts, kind, message FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"id": r[0], "ts": r[1], "kind": r[2], "message": r[3]} for r in reversed(rows)]


def get_events_since(event_id: int) -> list:
    """Return all events with an id greater than event_id."""
    rows = get_db().execute(
        "SELECT id, ts, kind, message FROM events WHERE id > ? ORDER BY id",
        (event_id,),
    ).fetchall()
    return [{"id": r[0], "ts": r[1], "kind": r[2], "message": r[3]} for r in rows]
