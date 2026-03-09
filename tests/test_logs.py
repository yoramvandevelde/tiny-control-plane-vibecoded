import os
from controller.store import (
    init_db,
    store_log,
    get_logs,
)

def test_store_and_fetch_logs(tmp_path):
    os.chdir(tmp_path)
    init_db()

    job_id = "job123"

    store_log(job_id, "hello")
    store_log(job_id, "world")

    logs = get_logs(job_id)

    assert len(logs) == 2
    assert logs[0]["line"] == "hello"
    assert logs[1]["line"] == "world"
