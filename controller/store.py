
import sqlite3, json, time, uuid, threading

_local = threading.local()
_db_lock = threading.Lock()

DB_PATH = "cluster.db"


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
    # Force a fresh connection for the new path
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
        state_json TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        node_id TEXT,
        command TEXT,
        workload_name TEXT,
        constraints TEXT,
        resources TEXT,
        status TEXT,
        result TEXT,
        created REAL,
        updated REAL
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS workloads (
        name TEXT PRIMARY KEY,
        command TEXT,
        replicas INTEGER,
        constraints TEXT,
        resources TEXT
    )
    """)

    db.commit()


def register_node(node_id, address, labels=None, capacity=None):
    with _db_lock:
        get_db().execute(
            "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?)",
            (
                node_id,
                address,
                json.dumps(labels or {}),
                json.dumps(capacity or {}),
                1,
                time.time(),
                None,
            )
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


def create_job(node_id, command, workload_name=None):
    jid = str(uuid.uuid4())
    with _db_lock:
        get_db().execute(
            """
            INSERT INTO jobs (
                id, node_id, command, workload_name,
                constraints, resources, status, result, created, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jid,
                node_id,
                command,
                workload_name,
                "{}",
                "{}",
                "pending",
                None,
                time.time(),
                time.time(),
            ),
        )
        get_db().commit()

    return jid


def get_pending_job(node_id):
    row = get_db().execute(
        "SELECT id,command FROM jobs WHERE node_id=? AND status='pending' LIMIT 1",
        (node_id,)
    ).fetchone()
    return row


def start_job(job_id):
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status='running', updated=? WHERE id=?",
            (time.time(), job_id)
        )
        get_db().commit()


def finish_job(job_id, status, result):
    with _db_lock:
        get_db().execute(
            "UPDATE jobs SET status=?, result=?, updated=? WHERE id=?",
            (status, result, time.time(), job_id)
        )
        get_db().commit()


def list_jobs():
    rows = get_db().execute(
        "SELECT id,node_id,command,workload_name,status,result FROM jobs"
    ).fetchall()

    return [
        {
            "id": r[0],
            "node": r[1],
            "command": r[2],
            "workload_name": r[3],
            "status": r[4],
            "result": r[5],
        }
        for r in rows
    ]


def count_active_workload_jobs(workload_name):
    row = get_db().execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE workload_name=? AND status NOT IN ('finished', 'failed')
        """,
        (workload_name,)
    ).fetchone()
    return row[0]


def create_workload(name, command, replicas, constraints=None, resources=None):
    with _db_lock:
        get_db().execute(
            "INSERT OR REPLACE INTO workloads VALUES (?,?,?,?,?)",
            (name, command, replicas, json.dumps(constraints or {}), json.dumps(resources or {}))
        )
        get_db().commit()


def list_workloads():
    rows = get_db().execute(
        "SELECT name,command,replicas,constraints,resources FROM workloads"
    ).fetchall()

    return [
        {
            "name": r[0],
            "command": r[1],
            "replicas": r[2],
            "constraints": json.loads(r[3]) if r[3] else {},
            "resources": json.loads(r[4]) if r[4] else {},
        }
        for r in rows
    ]
