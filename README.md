# Tiny Control Plane

Welcome to **Tiny Control Plane**, a small distributed systems playground written in Python.

This project demonstrates the core concepts behind real world orchestrators Kubernetes, Mesos and the like. Instead of thousands of lines of Go, YAML, and existential dread we (Gemini, ChatGPT and Claude) built it with **Python, curiosity, and a healthy (read: minimal) amount of input from a human.**

Yes, this entire mini‑orchestrator was vibe‑coded.

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

The controller uses a two-token system.

**Bootstrap token** — a shared secret set via `TCP_BOOTSTRAP_TOKEN` on both the controller and each agent. Used only for the initial registration request. If it is wrong or missing, the agent exits immediately.

**Node token** — a UUID issued by the controller on successful registration. The agent sends it as `X-Node-Token` on every subsequent request. Each node has its own token, so individual nodes can be revoked without affecting others.

Re-registering issues a new token and immediately invalidates the old one.

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
                  → lost
```

`pending` — scheduled, not yet picked up by an agent.
`running` — agent has claimed the job and is executing it.
`succeeded` — completed with exit code 0.
`failed` — completed with a non-zero exit code.
`lost` — was running but the agent disappeared or the job was cancelled.

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

Jobs can run inside Docker containers using `docker run --rm --network none`. Containers clean up after themselves and have no outbound network access by default. The agent captures the container ID via `--cidfile` to enable cancellation via `docker stop`.

---

### Workloads

A workload declares a desired number of replicas for a command:

```bash
python cli/tcp.py deploy workers uptime 3
python cli/tcp.py deploy alpine-workers "echo hello" 2 --image alpine
python cli/tcp.py deploy eu-workers uptime 3 --constraint region=eu
```

The controller ensures the declared number of replicas are always running. If jobs finish, fail, or get lost, the reconciler schedules replacements on the next pass. Each job is linked to its workload by name, so two workloads with the same command do not interfere with each other's replica counts.

---

### Scaling

To change the replica count of a running workload:

```bash
python cli/tcp.py scale workers 1
```

Scaling up schedules new jobs on the next reconcile pass. Scaling down immediately cancels excess jobs — pending jobs are cancelled before running ones — and sends kill signals to the agents hosting them.

---

### Stopping Workloads

```bash
python cli/tcp.py undeploy workers
```

Removes the workload from the controller. The reconciler stops scheduling new jobs for it. Running jobs complete normally unless explicitly cancelled beforehand.

---

### Node Revocation

```bash
python cli/tcp.py revoke node1
```

Sets the node token to NULL. The node's next request receives a 401. The agent logs the rejection and backs off rather than crashing, preserving any in-memory state.

---

### Logs

Agent stdout is captured line by line and stored in the controller database.

```bash
python cli/tcp.py logs <job_id>
```

Works for both shell and Docker workloads.

---

### Status View

```bash
python cli/tcp.py status
```

Displays a formatted table of all jobs with node, workload, command, colour-coded status, and elapsed time. Active jobs show time since creation. Terminal jobs show how long they actually ran.

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

Start the controller:

```bash
./run-controller.sh
```

Start agents:

```bash
python agent/agent.py --node-id node1 --port 9000
python agent/agent.py --node-id node1 --port 9000 --label region=eu
```

---

# CLI Reference

```bash
python cli/tcp.py nodes                                          # list nodes
python cli/tcp.py watch                                          # live node view
python cli/tcp.py exec <node> <command> [--image <image>]        # run one-off job
python cli/tcp.py deploy <name> <command> <replicas> \
    [--image <image>] [--constraint key=value]                   # declare workload
python cli/tcp.py scale <name> <replicas>                        # change replica count
python cli/tcp.py undeploy <name>                                # remove workload
python cli/tcp.py revoke <node>                                  # revoke node token
python cli/tcp.py status                                         # job status table
python cli/tcp.py jobs                                           # raw job list (JSON)
python cli/tcp.py logs <job_id>                                  # job output
```

---

# Running Tests

```bash
pytest
```

Tests are synchronous and call `reconcile_once()` directly. Each test gets an isolated SQLite database via `tmp_path`.

