import sqlite3, json, time, uuid, threading

_local = threading.local()
_db_lock = threading.Lock()

DB_PATH = "cluster.db"

LEASE_SECONDS = 60


class JobStatus:
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"
    LOST      = "lost"

    TERMINAL = {SUCCEEDED, FAILED, LOST}
    ACTIVE   = {PENDING, RUNNING}


def get_db():
    path = DB_PATH
    if not hasattr(_local, "conn") or _local.conn is None or _local.db_path != path:
        if hasattr(_local, "conn") and _local.conn is not None:
            _local.conn.close()
        _local.conn = sqlite3.connect(path, check_same_thread=True)
        _local.conn.row_factory = sqlite3.Row
        _local.db_path = path
    return _local.conn


def init_db(path=None):
    global DB_PATH
    if path:
        DB_PATH = str(path)
    _local.conn = None

    db = get_db()
    db.execute("""
    CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY,
        address TEXT,
        labels TEXT,
        capacity TEXT,
        healthy INTEGER,
        last_seen REAL,
        state_json TEXT,
        token TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        node_id TEXT,
        command TEXT,
        image TEXT,
        workload_name TEXT,
        constraints TEXT,
        resources TEXT,
        status TEXT,
        result TEXT,
        created REAL,
        updated REAL,
        lease_expires REAL
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS workloads (
        name TEXT PRIMARY KEY,
        command TEXT,
        image TEXT,
        replicas INTEGER,
        constraints TEXT,
        resources TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT,
        ts REAL,
        line TEXT
    )
    """)

    db.commit()


def register_node(node_id, address, labels=None, capacity=None):
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
            )
        )
        get_db().commit()
    return token


def verify_node_token(node_id, token):
    """Return True if the token matches the stored token for node_id."""
    row = get_db().execute(
        "SELECT token FROM nodes WHERE id=?",
        (node_id,)
    ).fetchone()
    if row is None:
        return False
    return row[0] == token


def revoke_node(node_id):
    """Set the node token to NULL, blocking further authenticated requests."""
    with _db_lock:
        get_db().execute(
            "UPDATE nodes SET token=NULL WHERE id=?",
            (node_id,)
        )
        get_db().commit()


def update_state(node_id, state):
    cpu = state.get("cpu", 0)
    mem = state.get("mem", 0)
    healthy = int(cpu < 0.9 and mem < 0.9)
    with _db_lock:
        get_db().execute(
            "UPDATE nodes SET state_json=?, last_seen=?, healthy=? WHERE id=?",
            (json.dumps(state), time.time(), healthy, node_id)
        )
        get_db().commit()


def list_nodes():
    rows = get_db().execute(
        "SELECT id,address,labels,capacity,healthy,state_json,last_seen FROM nodes"
    ).fetchall()

    result = {}
    for r in rows:
        result[r[0]] = {
            "address": r[1],
            "labels": json.loads(r[2]) if r[2] else {},
            "capacity": json.loads(r[3]) if r[3] else {},
            "healthy": bool(r[4]),
            "state": json.loads(r[5]) if r[5] else {},
            "last_seen": r[6],
        }

    return result


def create_job(node_id, command, image=None, workload_name=None):
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

    return jid


def get_pending_job(node_id):
    row = get_db().execute(
        "SELECT id, command, image FROM jobs WHERE node_id=? AND status=? LIMIT 1",
        (node_id, JobStatus.PENDING)
    ).fetchone()
    return row


def start_job(job_id):
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status=?, updated=?, lease_expires=? WHERE id=?",
            (JobStatus.RUNNING, time.time(), time.time() + LEASE_SECONDS, job_id)
        )
        get_db().commit()


def renew_lease(job_id):
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET lease_expires=? WHERE id=? AND status=?",
            (time.time() + LEASE_SECONDS, job_id, JobStatus.RUNNING)
        )
        get_db().commit()


def finish_job(job_id, status, result):
    if status == "finished":
        status = JobStatus.SUCCEEDED
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status=?, result=?, updated=?, lease_expires=NULL WHERE id=?",
            (status, result, time.time(), job_id)
        )
        get_db().commit()


def mark_lost(job_id):
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status=?, updated=? WHERE id=?",
            (JobStatus.LOST, time.time(), job_id)
        )
        get_db().commit()


def expire_lost_jobs():
    """Mark running jobs whose lease has expired as lost."""
    with _db_lock:
        get_db().execute(
            """
            UPDATE jobs SET status=?, updated=?
            WHERE status=? AND lease_expires IS NOT NULL AND lease_expires < ?
            """,
            (JobStatus.LOST, time.time(), JobStatus.RUNNING, time.time())
        )
        get_db().commit()


def list_jobs():
    rows = get_db().execute(
        "SELECT id, node_id, command, image, workload_name, status, result, created, updated FROM jobs"
    ).fetchall()

    return [
        {
            "id": r[0],
            "node": r[1],
            "command": r[2],
            "image": r[3],
            "workload_name": r[4],
            "status": r[5],
            "result": r[6],
            "created": r[7],
            "updated": r[8],
        }
        for r in rows
    ]


def count_active_node_jobs(node_id):
    """Count pending+running jobs assigned to a node."""
    placeholders = ",".join("?" * len(JobStatus.ACTIVE))
    row = get_db().execute(
        f"SELECT COUNT(*) FROM jobs WHERE node_id=? AND status IN ({placeholders})",
        (node_id, *JobStatus.ACTIVE)
    ).fetchone()
    return row[0]


def count_active_workload_jobs(workload_name):
    placeholders = ",".join("?" * len(JobStatus.ACTIVE))
    row = get_db().execute(
        f"SELECT COUNT(*) FROM jobs WHERE workload_name=? AND status IN ({placeholders})",
        (workload_name, *JobStatus.ACTIVE)
    ).fetchone()
    return row[0]


def get_excess_workload_jobs(workload_name, keep):
    """
    Return job IDs for active jobs beyond the desired replica count.
    Prefers to mark pending jobs lost before running ones.
    """
    placeholders = ",".join("?" * len(JobStatus.ACTIVE))
    rows = get_db().execute(
        f"""
        SELECT id, status FROM jobs
        WHERE workload_name=? AND status IN ({placeholders})
        ORDER BY
            CASE status WHEN 'pending' THEN 0 ELSE 1 END,
            created DESC
        """,
        (workload_name, *JobStatus.ACTIVE)
    ).fetchall()

    excess = len(rows) - keep
    if excess <= 0:
        return []
    return [r[0] for r in rows[:excess]]


def update_workload_replicas(name, replicas):
    """Update replica count for a workload. Returns True if found, False if not."""
    with _db_lock:
        cur = get_db().execute(
            "UPDATE workloads SET replicas=? WHERE name=?",
            (replicas, name)
        )
        get_db().commit()
    return cur.rowcount > 0


def create_workload(name, command, replicas, image=None, constraints=None, resources=None):
    with _db_lock:
        get_db().execute(
            "INSERT OR REPLACE INTO workloads VALUES (?,?,?,?,?,?)",
            (name, command, image, replicas, json.dumps(constraints or {}), json.dumps(resources or {}))
        )
        get_db().commit()


def delete_workload(name):
    with _db_lock:
        get_db().execute("DELETE FROM workloads WHERE name=?", (name,))
        get_db().commit()


def list_workloads():
    rows = get_db().execute(
        "SELECT name, command, image, replicas, constraints, resources FROM workloads"
    ).fetchall()

    return [
        {
            "name": r[0],
            "command": r[1],
            "image": r[2],
            "replicas": r[3],
            "constraints": json.loads(r[4]) if r[4] else {},
            "resources": json.loads(r[5]) if r[5] else {},
        }
        for r in rows
    ]


def store_log(job_id, line):
    with _db_lock:
        get_db().execute(
            "INSERT INTO logs(job_id, ts, line) VALUES (?,?,?)",
            (job_id, time.time(), line),
        )
        get_db().commit()


def get_logs(job_id):
    rows = get_db().execute(
        "SELECT ts, line FROM logs WHERE job_id=? ORDER BY id",
        (job_id,),
    ).fetchall()

    return [{"ts": r[0], "line": r[1]} for r in rows]
