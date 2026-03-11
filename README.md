# Tiny Control Plane

A small distributed systems playground written in Python. Demonstrates the core concepts behind real-world orchestrators like Kubernetes and Mesos — built with Python, curiosity, and a healthy (read: minimal) amount of human input.

Yes, this entire mini-orchestrator was vibe-coded by Gemini, ChatGPT, and Claude.

> "I vaguely know what I'm doing, the architecture makes sense in my head, and the code appears to work."

---

## Architecture

```
controller → central brain (FastAPI + SQLite)
agent      → node worker (polling)
cli        → operator tool (Typer + Rich)
```

Agents poll the controller for work. The controller maintains desired state via a reconciliation loop every 5 seconds.

---

## Features

- Cluster membership and health monitoring
- Label-based node selection and least-loaded scheduling
- Docker container execution (no bare-host fallback)
- Workload lifecycle management with desired replica count
- Formal job states with lease-based lost-job detection
- Job cancellation with container process termination
- Per-node and per-operator authentication with revocation
- Streaming logs and cluster event log via SSE
- Live topology and status views

---

## Quick Start

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Set tokens**

```bash
export TCP_BOOTSTRAP_TOKEN=your-bootstrap-secret
export TCP_OPERATOR_TOKEN=your-operator-secret
```

**3. Start the controller**

```bash
./run-controller.sh
```

**4. Start one or more agents**

```bash
python agent/agent.py --node-id node1 --port 9000
python agent/agent.py --node-id node2 --port 9001 --label region=homelab
```

**5. Run something**

```bash
tcp nodes
tcp exec node1 "echo hello" --image alpine
tcp deploy workers "echo hello" 3 --image alpine
tcp workloads
tcp describe workload workers
tcp status -f
```

---

## Documentation

- [Architecture & Internals](docs/architecture.md) — scheduler, job lifecycle, leases, cancellation, auth
- [Running the System](docs/running.md) — setup, tokens, multi-host deployment
- [CLI Reference](docs/cli.md) — all commands, flags, event kinds, live views
- [Contributing & Tests](docs/contributing.md) — running tests, dev notes

---

## Running Tests

```bash
pytest
```
