from controller.store import init_db, create_job, list_jobs
import os


def test_job_creation(tmp_path):

    os.chdir(tmp_path)

    init_db()

    jid = create_job("node1", "uptime")

    jobs = list_jobs()

    assert jobs[0]["id"] == jid
    assert jobs[0]["command"] == "uptime"
