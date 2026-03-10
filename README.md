# Tiny Control Plane

Welcome to **Tiny Control Plane**, a small distributed systems playground written in Python.

This project demonstrates the core concepts behind real world orchestrators like Kubernetes and Mesos. Instead of thousands of lines of Go, YAML, and existential dread we (Gemini, ChatGPT and Claude) built it with **Python, curiosity, and a healthy (read: minimal) amount of input from a human.**

Yes, this entire mini-orchestrator was vibe-coded.

Which means:

> "I vaguely know what I'm doing, the architecture makes sense in my head,
> and the code appears to work."

Somehow, this still resulted in:

* agents
* cluster membership
* health checks
* job scheduling
* label-based node selection
* least-loaded binpacking
* reconciliation loop
* Docker container execution
* workload lifecycle management
* formal job states with failure handling
* job leases and lost job detection
* job cancellation with process termination
* a CLI that tells you what is actually happening
* scaling workloads up and down
* per-node authentication and revocation
* per-operator authentication
* streaming logs via SSE
* cluster event log with lifecycle instrumentation
* topology view
* multi-host LAN deployment

---

# Architecture

The system has three components:

```
controller → central brain (FastAPI + SQLite)
agent      → node worker (polling)
cli        → operator tool (Typer + Rich)
```

Agents poll the controller for work. The controller maintains desired state via a reconciliation loop.

---

# Features

### Authentication

The controller uses a three-token system.

**Bootstrap token** — a shared secret set via `TCP_BOOTSTRAP_TOKEN` on both the controller and each agent. Used only for the initial registration request. If it is wrong or missing, the agent exits immediately.

**Node token** — a UUID issued by the controller on successful registration. The agent sends it as `X-Node-Token` on every subsequent request. Each node has its own token, so individual nodes can be revoked without affecting others. Re-registering issues a new node token and immediately invalidates the old one.

**Operator token** — a shared secret set via `TCP_OPERATOR_TOKEN` on the controller. The CLI sends it as `X-Operator-Token` on every request. All operator-facing endpoints (`/nodes`, `/jobs`, `/workloads`, `/events`, and their sub-routes) require this token. Requests without it or with the wrong value are rejected with HTTP 401.

The CLI resolves the operator token in this order:

1. `~/.tcp/operator.token` — write the token here manually for persistent access
2. `TCP_OPERATOR_TOKEN` environment variable — useful for scripting or CI

If neither is present the CLI exits immediately with a clear error message. To rotate the token, update the value on the controller and update the file or env var on the operator side — no command is needed.

---

### Node Registration

Agents register themselves with a node ID, labels, and resource capacity. Labels are used for scheduling constraints. The controller issues a node token on successful registration.

---

### Health Monitoring

Agents report CPU usage, memory usage, and disk free on every poll cycle. The controller marks a node **unhealthy** if CPU or memory exceed 90%. Unhealthy nodes are excluded from scheduling. Nodes that have not reported within 30 seconds are considered stale and also excluded.

---

### Job Queue

Jobs are stored in SQLite. Agents poll for pending jobs assigned to their node ID and execute them locally — either as a shell command or inside a Docker container, depending on whether an image is specified. Jobs run in background threads, so a node can pick up new work while existing jobs are still running.

---

### Job Lifecycle

Every job moves through a defined set of states:

```
pending → running → succeeded
                  → failed
                  → cancelled
                  → lost
```

`pending` — scheduled, not yet picked up by an agent.
`running` — agent has claimed the job and is executing it.
`succeeded` — completed with exit code 0.
`failed` — completed with a non-zero exit code.
`cancelled` — explicitly stopped by an operator action (scale-down or undeploy).
`lost` — was running but the agent disappeared or its lease expired without renewal.

Only `pending` and `running` jobs count as active. The reconciler uses this to determine how many new jobs to create for a workload.

---

### Job Leases

When an agent picks up a job it receives a lease expiring after 60 seconds. The agent renews it every 15 seconds via heartbeat. If an agent dies mid-job, heartbeats stop, and the next reconcile pass marks the job `lost` and schedules a replacement.

---

### Job Cancellation

When a job is cancelled (via scale-down or undeploy), the controller records a cancellation entry for that job's node. The agent polls `GET /agent/cancel/{node}` on every loop iteration. When a cancel is received, the agent terminates the process:

- Shell jobs receive SIGTERM, then SIGKILL after a 5-second grace period.
- Docker jobs are stopped via `docker stop`, which also sends SIGTERM then SIGKILL.

Cancellations are delivered exactly once — the poll endpoint is a destructive read.

Late results arriving after a job has been cancelled are silently ignored; terminal state is never overwritten.

---

### Scheduler

Jobs are placed using a pipeline of:

1. **Label constraint matching** — only nodes satisfying all workload constraints are considered.
2. **Health and staleness filter** — unhealthy or stale nodes are excluded.
3. **Least active jobs** — the node with the fewest pending+running jobs is preferred.
4. **Lowest CPU** — used as a tiebreaker when job counts are equal.
5. **Random shuffle** — candidates are shuffled before sorting so exact ties are broken randomly.

---

### Docker Execution

Jobs can run inside Docker containers using `docker run --rm --network none`. Containers are named `tcp-{job_id[:16]}` so the agent can reliably stop them by name during cancellation. Containers clean up after themselves and have no outbound network access by default.

---

### Workloads

A workload declares a desired number of replicas for a command:

```bash
tcp deploy workers uptime 3
tcp deploy alpine-workers "echo hello" 2 --image alpine
tcp deploy eu-workers uptime 3 --constraint region=homelab
```

The controller ensures the declared number of replicas are always running. If jobs finish, fail, or get lost, the reconciler schedules replacements on the next pass.

---

### Scaling

To change the replica count of a running workload:

```bash
tcp scale workers 1
```

Scaling up schedules new jobs on the next reconcile pass. Scaling down immediately cancels excess jobs — pending jobs are cancelled before running ones — and sends kill signals to the agents hosting them.

---

### Stopping Workloads

```bash
tcp undeploy workers
```

Cancels all running and pending jobs for the workload, then removes it from the controller. Running jobs are terminated, not left to complete.

---

### Node Revocation

```bash
tcp revoke node1
```

Sets the node token to NULL. The node's next request receives a 401. The agent logs the rejection and exits immediately.

---

### Logs

Agent stdout is captured line by line and stored in the controller database.

```bash
tcp logs <job_id> [-f]   # print captured output; -f streams live via SSE until job is terminal
```

Works for both shell and Docker workloads.

---

### Event Log

The controller records a structured event for every significant lifecycle transition.

```bash
tcp events [-f]          # recent events (last 200); -f streams live via SSE
```

Event kinds:

| Kind | Meaning |
|---|---|
| `node.registered` | Agent registered or re-registered |
| `node.revoked` | Node token revoked |
| `workload.created` | New workload declared |
| `workload.scaled` | Replica count changed |
| `workload.deleting` | Undeploy initiated, cancelling jobs |
| `workload.removed` | Workload fully removed |
| `job.scheduled` | Job assigned to a node |
| `job.started` | Agent picked up the job |
| `job.succeeded` | Job exited 0 |
| `job.failed` | Job exited non-zero |
| `job.cancelled` | Job explicitly cancelled by operator action |
| `job.lost` | Job lost due to agent disappearance or lease expiry |

---

### Status View

```bash
tcp status [-f]   # job status table; -f redraws live every second
```

Displays a formatted table of all jobs with node, workload, command, colour-coded status, and elapsed time. Active jobs show time since creation. Terminal jobs show how long they actually ran.

---

### Topology View

```bash
tcp topology [-f]   # cluster tree: nodes with their active jobs as leaves; -f redraws live every second
```

Displays a tree rooted at the cluster, with nodes as branches and their active jobs as leaves. Each node shows current CPU, memory, and job count. The cluster root shows aggregate node count, job count, and average CPU.

---

### Concurrent Safety

The SQLite layer uses per-thread connections and a write lock, so the reconcile loop and FastAPI request handlers do not race each other.

---

# Running the System

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the bootstrap token on both the controller host and each agent host:

```bash
export TCP_BOOTSTRAP_TOKEN=your-secret-here
```

Set the operator token on the controller host:

```bash
export TCP_OPERATOR_TOKEN=your-operator-secret-here
```

Provide the operator token to the CLI — either via the environment variable:

```bash
export TCP_OPERATOR_TOKEN=your-operator-secret-here
```

Or by writing it to the token file (takes precedence over the env var):

```bash
mkdir -p ~/.tcp && echo "your-operator-secret-here" > ~/.tcp/operator.token && chmod 600 ~/.tcp/operator.token
```

Start the controller:

```bash
./run-controller.sh
```

Start agents:

```bash
python agent/agent.py --node-id node1 --port 9000
python agent/agent.py --node-id node2 --port 9001 --label region=homelab
```

---

### Controller address

Agents and the CLI can connect to the controller using any reachable IP address.
By default both connect to:

    http://127.0.0.1:8000

To run agents or the CLI on other machines or internal networks, set the
controller address via the environment variable:

    TCP_CONTROLLER=http://10.0.0.5:8000

Example — agent on a remote host:

    TCP_CONTROLLER=http://10.0.0.5:8000 python -m agent.agent --node-id node2 --port 9000

Example — CLI on a remote host:

    TCP_CONTROLLER=http://10.0.0.5:8000 tcp nodes

---

### Agent advertised address

When an agent registers it advertises its own address to the controller. By
default the agent auto-detects its LAN IP by opening a temporary UDP socket
toward the controller — no data is sent, the OS simply selects the correct
outbound interface. If detection fails the agent falls back to `127.0.0.1`
and logs a warning.

To override this explicitly, use `--address`:

```bash
python agent/agent.py --node-id node1 --port 9000 --address http://10.0.0.10:9000
```

Use `--address` when the auto-detected IP is wrong — for example on hosts with
multiple network interfaces or when running inside a container.

---

# CLI Reference

```bash
tcp nodes                                                # list nodes
tcp topology [-f]                                        # cluster tree view; -f live
tcp exec <node> <command> [--image <image>]              # run one-off job
tcp deploy <n> <command> <replicas> \
    [--image <image>] [--constraint key=value]           # declare workload
tcp scale <n> <replicas>                                 # change replica count
tcp undeploy <n>                                         # remove workload, cancels jobs
tcp revoke <node>                                        # revoke node token
tcp status [-f]                                          # job status table; -f live
tcp jobs                                                 # raw job list (JSON)
tcp logs <job_id> [-f]                                   # job output; -f stream live
tcp events [-f]                                          # recent cluster events; -f stream live
```

---

# Running Tests

```bash
pytest
```

Tests are synchronous and call `reconcile_once()` directly. Each test gets an isolated SQLite database via `tmp_path`.
