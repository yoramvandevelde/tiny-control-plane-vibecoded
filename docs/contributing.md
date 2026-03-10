# Contributing & Tests

## Running Tests

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_scheduler.py

# Run a single test by name
pytest tests/test_api.py::test_function_name
```

---

## Test Design

Tests use `tmp_path` fixtures for isolated SQLite databases so no shared state leaks between cases. The reconciler is tested by calling `reconcile_once()` directly rather than relying on the async background loop — this makes reconciliation behaviour deterministic and synchronous in tests.

The `TestClient` from Starlette is used for API tests, with `raise_server_exceptions=True` so unexpected 500s fail loudly.

---

## Project Structure

```
controller/
  api.py       — FastAPI app, all HTTP endpoints, reconcile loop, pick_node()
  store.py     — SQLite layer with per-thread connections and a write lock

agent/
  agent.py     — polling loop, Docker execution, heartbeat, log streaming

cli/
  tcp.py       — Typer CLI, Rich output, SSE consumers

tests/         — pytest test suite
```

---

## Key Design Notes

**Scheduling** (`pick_node()` in `controller/api.py`) — filters nodes by label constraints, health (CPU/mem < 90%), and staleness (last seen < 30s), then sorts by (active job count, CPU usage) with a pre-sort shuffle for random tie-breaking.

**Job leases** — running jobs hold a 60s lease; agents renew every 15s. Expired leases cause the reconciler to mark jobs as `lost`. A 60s startup grace period prevents false losses after a controller restart.

**Cancellation delivery** — the cancel poll endpoint (`GET /agent/cancel/{node}`) is a destructive read. Cancellations are delivered exactly once.

**Container naming** — containers are named `tcp-{job_id[:16]}` for deterministic cancellation via `docker stop`.

**Database concurrency** — `store.py` uses per-thread SQLite connections with a write lock shared between FastAPI handlers and the reconcile loop.

---

## CLAUDE.md

A `CLAUDE.md` file at the repo root provides guidance for Claude Code with command shortcuts and architecture notes kept in sync with the codebase.
