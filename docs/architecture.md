# Architecture & Internals

## Components

### Controller (`controller/`)

Central FastAPI server backed by SQLite (`cluster.db`). Runs a reconciliation loop every 5 seconds that expires stale job leases and schedules new replicas for workloads. All state lives in the database.

### Agent (`agent/agent.py`)

Runs on each compute node. Polls the controller every ~1 second: registers itself, reports CPU/memory metrics, picks up one pending job at a time, executes it in a Docker container, streams logs back, and posts the result.

### CLI (`cli/tcp.py`)

Typer-based operator tool. Reads the operator token from `~/.tcp/operator.token` or `TCP_OPERATOR_TOKEN`. Supports live-updating views via SSE streams with `--follow` / `-f`.

---

## Authentication

The controller uses a three-token system.

**Bootstrap token** — a shared secret set via `TCP_BOOTSTRAP_TOKEN` on both the controller and each agent. Used only for the initial registration request. If it is wrong or missing, the agent exits immediately.

**Node token** — a UUID issued by the controller on successful registration. The agent sends it as `X-Node-Token` on every subsequent request. Each node has its own token, so individual nodes can be revoked without affecting others. Re-registering issues a new node token and immediately invalidates the old one.

**Operator token** — a shared secret set via `TCP_OPERATOR_TOKEN` on the controller. The CLI sends it as `X-Operator-Token` on every request. All operator-facing endpoints (`/nodes`, `/jobs`, `/workloads`, `/events`, and their sub-routes) require this token. Requests without it or with the wrong value are rejected with HTTP 401.

The CLI resolves the operator token in this order:

1. `~/.tcp/operator.token` — write the token here manually for persistent access
2. `TCP_OPERATOR_TOKEN` environment variable — useful for scripting or CI

If neither is present the CLI exits immediately with a clear error message. To rotate the token, update the value on the controller and update the file or env var on the operator side — no command needed.

---

## Node Registration & Health

Agents register themselves with a node ID, labels, and resource capacity. Labels are used for scheduling constraints. The controller issues a node token on successful registration.

Agents report CPU usage, memory usage, and disk free on every poll cycle. The controller marks a node **unhealthy** if CPU or memory exceed 90%. Unhealthy nodes are excluded from scheduling. Nodes that have not reported within 30 seconds are considered stale and also excluded.

---

## Scheduler

Jobs are placed using a pipeline of:

1. **Label constraint matching** — only nodes satisfying all workload constraints are considered.
2. **Health and staleness filter** — unhealthy or stale nodes are excluded.
3. **Least active jobs** — the node with the fewest pending+running jobs is preferred.
4. **Lowest CPU** — used as a tiebreaker when job counts are equal.
5. **Random shuffle** — candidates are shuffled before sorting so exact ties are broken randomly.

See `pick_node()` in `controller/api.py`.

---

## Job Lifecycle

Every job moves through a defined set of states:

```
pending → running → succeeded
                  → failed
                  → cancelled
                  → lost
```

| State | Meaning |
|---|---|
| `pending` | Scheduled, not yet picked up by an agent |
| `running` | Agent has claimed the job and is executing it |
| `succeeded` | Completed with exit code 0 |
| `failed` | Completed with a non-zero exit code |
| `cancelled` | Explicitly stopped by an operator action (scale-down or undeploy) |
| `lost` | Was running but the agent disappeared or its lease expired without renewal |

Only `pending` and `running` jobs count as active. The reconciler uses this to determine how many new jobs to create for a workload.

---

## Job Leases

When an agent picks up a job it receives a lease expiring after 60 seconds. The agent renews it every 15 seconds via heartbeat. If an agent dies mid-job, heartbeats stop, and the next reconcile pass marks the job `lost` and schedules a replacement.

A 60-second startup grace period prevents false losses after a controller restart.

---

## Job Cancellation

When a job is cancelled (via scale-down or undeploy), the controller records a cancellation entry for that job's node. The agent polls `GET /agent/cancel/{node}` on every loop iteration. When a cancel is received, the agent stops the container via `docker stop`, which sends SIGTERM then SIGKILL after a grace period.

Cancellations are delivered exactly once — the poll endpoint is a destructive read.

Late results arriving after a job has been cancelled are silently ignored; terminal state is never overwritten.

---

## Docker Execution

Every job runs inside a Docker container using `docker run --rm --network none`. Bare host execution is not supported. A Docker image is required on every job submission and workload declaration.

Containers are named `tcp-{job_id[:16]}` so the agent can reliably stop them by name during cancellation. Containers clean up after themselves and have no outbound network access by default.

---

## Workloads & Reconciliation

A workload declares a desired number of replicas for a command. The reconciler runs every 5 seconds and ensures the declared number of replicas are always running. If jobs finish, fail, or get lost, it schedules replacements on the next pass.

Scaling up schedules new jobs on the next reconcile pass. Scaling down immediately cancels excess jobs — pending jobs are cancelled before running ones.

---

## Database Concurrency

`controller/store.py` uses per-thread SQLite connections with a write lock shared between FastAPI request handlers and the reconcile loop. Tests use `tmp_path` fixtures for isolated databases and call `reconcile_once()` directly rather than relying on the async loop.

## Database Maintenance

Terminal jobs (succeeded, failed, cancelled, lost) and cluster events accumulate
in SQLite indefinitely. Two mechanisms keep the database lean.

**Automatic** — `cancel_jobs` rows are pruned by the reconciler on every pass:
- Acked rows (agent confirmed receipt) are deleted immediately.
- Unacked rows older than CANCEL_REDELIVER_SECONDS are deleted — this covers
agents that died before polling.

No operator action is needed. The reconciler handles this silently alongside
lease expiry and workload scheduling.

**Manual** — terminal jobs, their log lines, and old events are removed via
`tcp gc`. See the [CLI Reference](docs/cli.md) for usage.
