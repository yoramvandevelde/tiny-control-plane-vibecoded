
import sqlite3, json, time, uuid

DB = None

def get_db():
    global DB

    if DB is None:
        DB = sqlite3.connect("cluster.db", check_same_thread=False)

    return DB

def init_db():
    get_db().execute("""
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

    get_db().execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        node_id TEXT,
        command TEXT,
        constraints TEXT,
        resources TEXT,
        status TEXT,
        result TEXT,
        created REAL,
        updated REAL
    )
    """)

    get_db().execute("""
    CREATE TABLE IF NOT EXISTS workloads (
        name TEXT PRIMARY KEY,
        command TEXT,
        replicas INTEGER
    )
    """)

    get_db().commit()


def register_node(node_id, address, labels=None, capacity=None):
    get_db().execute(
        "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?)",
        (
            node_id,
            address,
            json.dumps(labels or {}),
            json.dumps(capacity or {}),
            1,
            time.time(),
            None
        )
    )
    get_db().commit()


def update_state(node_id, state):
    get_db().execute(
        "UPDATE nodes SET state_json=?, last_seen=? WHERE id=?",
        (json.dumps(state), time.time(), node_id)
    )
    get_db().commit()


def list_nodes():
    rows = get_db().execute(
        "SELECT id,address,labels,capacity,healthy,state_json FROM nodes"
    ).fetchall()

    result = {}

    for r in rows:
        result[r[0]] = {
            "address": r[1],
            "labels": json.loads(r[2]) if r[2] else {},
            "capacity": json.loads(r[3]) if r[3] else {},
            "healthy": bool(r[4]),
            "state": json.loads(r[5]) if r[5] else {}
        }

    return result


def create_job(node_id, command):

    jid = str(uuid.uuid4())

    get_db().execute(
        """
        INSERT INTO jobs (
            id,
            node_id,
            command,
            constraints,
            resources,
            status,
            result,
            created,
            updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            jid,
            node_id,
            command,
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
    get_db().execute(
        "UPDATE jobs SET status='running', updated=? WHERE id=?",
        (time.time(), job_id)
    )
    get_db().commit()


def finish_job(job_id, status, result):
    get_db().execute(
        "UPDATE jobs SET status=?, result=?, updated=? WHERE id=?",
        (status, result, time.time(), job_id)
    )
    get_db().commit()


def list_jobs():
    rows = get_db().execute(
        "SELECT id,node_id,command,status,result FROM jobs"
    ).fetchall()

    result = []

    for r in rows:
        result.append({
            "id": r[0],
            "node": r[1],
            "command": r[2],
            "status": r[3],
            "result": r[4]
        })

    return result


def create_workload(name, command, replicas):
    get_db().execute(
        "INSERT OR REPLACE INTO workloads VALUES (?,?,?)",
        (name, command, replicas)
    )
    get_db().commit()


def list_workloads():
    rows = get_db().execute(
        "SELECT name,command,replicas FROM workloads"
    ).fetchall()

    return [{"name": r[0], "command": r[1], "replicas": r[2]} for r in rows]
