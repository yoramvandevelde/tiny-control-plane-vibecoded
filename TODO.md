# Tiny Control Plane — TODO

## Bug Fixes

- [ ] **`CANCELLED` is not a real status** — `mark_cancelled` sets status to `LOST` and `JobStatus` has no `CANCELLED` constant, despite the README and events treating it as distinct. The database, status table, and CLI colouring all need updating. Also do a full audit of every place `LOST` is used to check whether it should actually be `CANCELLED` — the reconciler treating a cancelled job the same as a lost one and scheduling a replacement is a subtle but real bug.

- [ ] **`cancelled` status missing from `STATUS_COLOUR` in the CLI** — falls through to white instead of yellow. Minor but inconsistent with `EVENT_COLOUR`.

- [ ] **Log streaming does not close on `cancelled` status** — `job_logs_stream` only terminates on `succeeded`, `failed`, or `lost`. A cancelled job's stream hangs open indefinitely.

## Reliability

- [ ] **Operator endpoints have no authentication** — anyone on the network can deploy, undeploy, scale, or revoke nodes. Needs a design decision (static token, bearer token, mTLS) and implementation — the bootstrap token pattern already in place is a natural model to follow.

- [ ] **Cancel signals are lost on controller restart** — the cancel queue lives in SQLite and is drained destructively. If the controller dies after enqueuing and before the agent polls, the signal is gone and the process keeps running until the agent dies or re-registers.

## Performance

- [ ] **Reconciler does no work batching** — creates jobs one at a time with individual DB writes and lock acquisitions. Fine at current scale, worth addressing if workload counts grow significantly.

## Features

- [ ] **Job retry policy** — failed jobs stay failed and workloads that always fail spin forever scheduling replacements. Add `--retries` and `--max-attempts` to `tcp deploy`, track attempt count in the DB, and reschedule with backoff only if retries remain.

- [ ] **DB pruning** — terminal jobs and old events accumulate forever. Add `tcp gc` with a configurable age policy, e.g. delete jobs and events older than N days.

- [ ] **Missing CLI commands** — add `tcp workloads` (list all workloads), `tcp describe workload <name>`, `tcp describe job <id>`, and `tcp describe node <id>` for better day-to-day usability.

- [ ] **Watch streams** — the event architecture (`list_events` / `get_events_since` / `events/stream`) already mirrors the Kubernetes list/watch pattern. `tcp watch jobs`, `tcp watch nodes`, and `tcp watch workloads` would be natural extensions with minimal new controller work.

- [ ] **Node drain** — add `tcp drain <node>` that marks the node as unschedulable, lets running jobs finish (or optionally cancels and reschedules them elsewhere), then allows safe removal or maintenance.

- [ ] **Resource-aware scheduling** — the highest-value feature addition. Scheduling currently ignores capacity entirely despite the data model already being in place. Add `--cpu` and `--mem` to both `tcp deploy` and agent registration, track allocated resources per node in the scheduler, and only place a job if the node has sufficient headroom.
