
# Tiny Control Plane (vibecoded)

A tiny cluster control plane written in Python for learning how orchestration systems work.

Instead of thousands of lines of Go, CRDs, YAML, and existential dread, this project keeps
things intentionally small so the core ideas are easy to understand.

It includes:

- a **controller** (the brain)
- **agents** (workers that execute workloads)
- a simple **scheduler**
- a **reconciliation loop**
- **container / command execution**
- **workload logs**
- a small **CLI**
- **tests**

The goal is to make it possible to read the entire system in an afternoon and understand
how orchestration actually works.

---

# Architecture

The system consists of three main components.

```
CLI → Controller → Agents
```

## Controller

The controller maintains cluster state and runs the reconciliation loop.

Responsibilities:

- node registration
- workload storage
- job scheduling
- job state tracking
- log storage
- reconciliation loop

The controller exposes a small HTTP API used by both the CLI and agents.

---

## Agents

Agents run on worker nodes and execute workloads.

Responsibilities:

- register with the controller
- report node state
- request jobs
- execute workloads
- send job results
- send workload logs

Agents can run either:

- shell commands
- Docker containers

---

## CLI

The CLI is a thin wrapper around the controller API.

Example commands:

```
tcp nodes
tcp jobs
tcp workloads
tcp logs <job_id>
```

---

# Reconciliation

The controller continuously enforces **desired state**.

Example workload:

```
workers
replicas = 3
image = alpine
command = uptime
```

If the cluster currently has:

```
2 running jobs
```

The controller creates **1 new job**.

This loop runs continuously and is the core concept behind systems like Kubernetes.

---

# Scheduler

The scheduler uses a simple pipeline:

```
filter → score → pick
```

Nodes are filtered by:

- health
- staleness
- label constraints

Remaining nodes are scored using **least CPU load**.

The node with the lowest load wins.

This is intentionally simple but demonstrates the structure used by real schedulers.

---

# Logging

Agents capture workload stdout and send log lines back to the controller.

Logs are stored in the controller database and can be retrieved using:

```
tcp logs <job_id>
```

Example:

```
$ tcp logs 1a2b3c

starting worker
processing item 1
processing item 2
done
```

This works for both shell workloads and Docker containers.

---

# Example Usage

Start the controller:

```
python controller/api.py
```

Start an agent:

```
python agent/agent.py --node node1
```

Deploy a workload:

```
tcp deploy test "echo hello world" 1
```

List jobs:

```
tcp jobs
```

View logs:

```
tcp logs <job_id>
```

---

# Database

The controller stores cluster state in SQLite.

Tables:

- nodes
- workloads
- jobs
- logs

The database is intentionally simple so it can be inspected directly during development.

---

# Testing

Tests cover:

- node registration
- job lifecycle
- scheduler behaviour
- reconciliation logic
- log storage

Run tests with:

```
pytest
```

Two truths about testing:

1. **Tests prove your code works... right up until the moment production proves otherwise.**
2. **If a test passes on the first run, it probably isn't testing the scary part yet.**

Fortunately this project keeps tests deterministic by exposing `reconcile_once()` so the
reconciliation loop can be tested without time-based polling.

---

# Why This Exists

Large orchestration systems are hard to understand because the essential ideas are buried
under layers of production complexity.

This project keeps only the core concepts:

- desired vs observed state
- reconciliation
- scheduling
- agents executing workloads

Everything else can be built on top of those primitives.

---

# Future Ideas

Possible extensions:

- job leases
- node failure recovery
- event log / cluster events
- streaming logs
- rolling deployments
- resource accounting
- scheduler plugins

Each feature can be added in small steps while keeping the system understandable.

---

# License

MIT
