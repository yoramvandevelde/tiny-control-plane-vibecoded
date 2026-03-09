
import sqlite3, json, time, uuid

DB = sqlite3.connect("cluster.db", check_same_thread=False)

def init_db():
    DB.execute("""
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

    DB.execute("""
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

    DB.execute("""
    CREATE TABLE IF NOT EXISTS workloads (
        name TEXT PRIMARY KEY,
        command TEXT,
        replicas INTEGER
    )
    """)

    DB.commit()


def register_node(node_id, address, labels=None, capacity=None):
    DB.execute(
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
    DB.commit()


def update_state(node_id, state):
    DB.execute(
        "UPDATE nodes SET state_json=?, last_seen=? WHERE id=?",
        (json.dumps(state), time.time(), node_id)
    )
    DB.commit()


def list_nodes():
    rows = DB.execute(
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
    DB.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
        (jid, node_id, command, "{}", "{}", "pending", None, time.time(), time.time())
    )
    DB.commit()
    return jid


def get_pending_job(node_id):
    row = DB.execute(
        "SELECT id,command FROM jobs WHERE node_id=? AND status='pending' LIMIT 1",
        (node_id,)
    ).fetchone()
    return row


def start_job(job_id):
    DB.execute(
        "UPDATE jobs SET status='running', updated=? WHERE id=?",
        (time.time(), job_id)
    )
    DB.commit()


def finish_job(job_id, status, result):
    DB.execute(
        "UPDATE jobs SET status=?, result=?, updated=? WHERE id=?",
        (status, result, time.time(), job_id)
    )
    DB.commit()


def list_jobs():
    rows = DB.execute(
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
    DB.execute(
        "INSERT OR REPLACE INTO workloads VALUES (?,?,?)",
        (name, command, replicas)
    )
    DB.commit()


def list_workloads():
    rows = DB.execute(
        "SELECT name,command,replicas FROM workloads"
    ).fetchall()

    return [{"name": r[0], "command": r[1], "replicas": r[2]} for r in rows]
