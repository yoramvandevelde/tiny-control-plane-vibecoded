
from controller.store import (
    init_db,
    create_workload,
    list_jobs,
)
from controller.api import reconcile_loop
import asyncio
import os


def test_reconcile_creates_jobs(tmp_path):

    os.chdir(tmp_path)

    init_db()

    create_workload("workers", "uptime", 2)

    async def run_once():
        task = asyncio.create_task(reconcile_loop())
        await asyncio.sleep(1)
        task.cancel()

    try:
        asyncio.run(run_once())
    except:
        pass

    jobs = list_jobs()

    assert len(jobs) >= 2
