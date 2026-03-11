# Tiny Control Plane — TODO

## Bug Fixes

- [X] **`CANCELLED` is not a real status** — `mark_cancelled` sets status to `LOST` and `JobStatus` has no `CANCELLED` constant, despite the README and events treating it as distinct. The database, status table, and CLI colouring all need updating. Also do a full audit of every place `LOST` is used to check whether it should actually be `CANCELLED` — the reconciler treating a cancelled job the same as a lost one and scheduling a replacement is a subtle but real bug.

- [X] **`cancelled` status missing from `STATUS_COLOUR` in the CLI** — falls through to white instead of yellow. Minor but inconsistent with `EVENT_COLOUR`.

- [X] **Log streaming does not close on `cancelled` status** — `job_logs_stream` only terminates on `succeeded`, `failed`, or `lost`. A cancelled job's stream hangs open indefinitely.

## Reliability

- [X] **Operator endpoints have no authentication** — anyone on the network can deploy, undeploy, scale, or revoke nodes. Needs a design decision (static token, bearer token, mTLS) and implementation — the bootstrap token pattern already in place is a natural model to follow.

- [X] **Cancel signals are lost on controller restart** — the cancel queue lives in SQLite and is drained destructively. If the controller dies after enqueuing and before the agent polls, the signal is gone and the process keeps running until the agent dies or re-registers.

## Performance

- [ ] **Reconciler does no work batching** — creates jobs one at a time with individual DB writes and lock acquisitions. Fine at current scale, worth addressing if workload counts grow significantly.

## Features

- [X] **Job retry policy** — `--max-attempts` added to `tcp deploy` (default unlimited (0), set to a positive integer to cap retries). `attempt` counter and `max_attempts` column already present in DB. Reconciler checks `count_failed_workload_jobs` against `max_attempts * replicas` before scheduling; emits `workload.exhausted` event when limit is reached. Cancelled jobs excluded from the failure count.

- [X] **DB pruning** — terminal jobs and old events accumulate forever. `cancel_jobs` rows are pruned automatically by the reconciler on every pass (acked rows immediately; unacked rows after `CANCEL_REDELIVER_SECONDS`). Operator-triggered pruning via `tcp gc [--days N] [--dry-run]` removes old terminal jobs (cascading to logs and cancel_jobs) and old events.

- [X] **Missing CLI commands** — added `tcp workloads` (list all workloads with replica health), `tcp describe workload <n>`, `tcp describe job <id>`, and `tcp describe node <id>`. The `describe` commands are a sub-app (`tcp describe --help` works independently) using a key/value table layout. `tcp describe job` supports ID prefix matching and shows the last 10 log lines inline.

- [ ] **Watch streams** — the event architecture (`list_events` / `get_events_since` / `events/stream`) already mirrors the Kubernetes list/watch pattern. `tcp watch jobs`, `tcp watch nodes`, and `tcp watch workloads` would be natural extensions with minimal new controller work.

- [ ] **Node drain** — add `tcp drain <node>` that marks the node as unschedulable, lets running jobs finish (or optionally cancels and reschedules them elsewhere), then allows safe removal or maintenance.

- [X] **Resource-aware scheduling** — the highest-value feature addition. Scheduling currently ignores capacity entirely despite the data model already being in place. Add `--cpu` and `--mem` to both `tcp deploy` and agent registration, track allocated resources per node in the scheduler, and only place a job if the node has sufficient headroom.
