# Tiny Control Plane (Vibe-Coded Edition)

Welcome to **Tiny Control Plane**, a small distributed systems playground written in Python.

This project demonstrates the core concepts behind real orchestrators such as:

* Kubernetes
* Nomad
* Mesos
* CI worker fleets

But instead of thousands of lines of Go, YAML, and existential dread…

we built it with **Python, curiosity, and a healthy amount of vibecoding.**

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

Which is suspiciously close to a real orchestrator.

Apparently vibecoding scales.

Then an AI reviewed the vibe-coded code, found actual bugs, fixed them, and
introduced new bugs in the tests.

Apparently vibecoding also has a patch cycle now.

> "We fixed the reconciler. The tests were wrong.
> Then the patch was wrong. Then the patch path was wrong.
> Three iterations to fix four bugs. This is just software."

Then Docker support was added. The agent gained the ability to pull and run
containers. The first test command was `echo hello` in an Alpine image.

It worked. We felt powerful. Then we noticed the jobs were stuck on `pending`
because exceptions were silently swallowed and Docker wasn't running.

> "Adding container orchestration to your orchestrator is straightforward.
> Remembering to start Docker is optional, apparently."

---

# Architecture

The system has three components:

```
controller → central brain
agent      → node worker
cli        → operator tool
```

Agents poll the controller for jobs.

The controller:

1. tracks nodes
2. evaluates health
3. schedules jobs using label constraints and least-CPU binpacking
4. maintains desired workloads via reconciliation

---

# Features

### Node Registration

Agents register themselves with:

* labels
* resource capacity

Example:

```json
{
  "node": "node1",
  "labels": {"region": "eu"},
  "capacity": {"cpu": 4, "mem": 8192}
}
```

---

### Health Monitoring

Agents periodically report:

* CPU usage
* memory usage
* disk free

The controller marks nodes **unhealthy** if CPU or memory exceed 90%.
Unhealthy nodes are excluded from scheduling.

Nodes that have not reported state within 30 seconds are also excluded —
they are considered stale and may be dead.

---

### Job Queue

Jobs are stored in SQLite.

Agents poll:

```
GET /agent/jobs/<node>
```

and execute the job locally — either as a shell command or inside a Docker
container, depending on whether an image is specified.

---

### Scheduler

Jobs are placed using:

1. **Label constraint matching** — only nodes whose labels satisfy all
   constraints are considered
2. **Health and staleness filter** — unhealthy or stale nodes are excluded
3. **Least-loaded selection** — among eligible nodes, the one with the
   lowest reported CPU usage is chosen

This is a real scheduling pipeline: filter → score → pick.
Which is exactly what Kubernetes does, minus 400 lines of Go per step.

---

### Docker Execution

Jobs can run inside Docker containers. The agent uses `docker run --rm`
so containers clean up after themselves.

Containers run with `--network none` by default — no outbound access,
no surprises.

If no image is specified, the job runs as a plain shell command instead.
Both modes coexist; the agent decides at execution time based on whether
`image` is set.

---

### Workloads (Desired State)

Instead of running a job once you can declare:

```
replicas = 3
command  = uptime
```

The controller ensures 3 copies are always running.

If jobs finish or nodes disappear → the controller starts new ones.

Workloads support label constraints, so you can pin replicas to a region:

```bash
python cli/tcp.py deploy eu-workers uptime 3 --constraint region=eu
```

Workloads also support Docker images:

```bash
python cli/tcp.py deploy alpine-workers "echo hello" 2 --image alpine
```

This pattern is called **reconciliation**. It is the core of Kubernetes.

Each job is linked to its workload by name, not by command string. Two
workloads with the same command do not interfere with each other's replica
counts.

---

### Concurrent Safety

The SQLite layer uses per-thread connections and a write lock, so the
reconcile loop and the FastAPI request handlers do not race each other.

This is the kind of thing that works fine until it doesn't, and then you
spend a Friday reading WAL documentation. We handled it before that Friday.

---

# Running the System

Install dependencies:

```bash
pip install -r requirements.txt
```

Start controller:

```bash
./run-controller.sh
```

Start agents:

```bash
python agent/agent.py --node-id node1 --port 9000
python agent/agent.py --node-id node2 --port 9001
```

View nodes:

```bash
python cli/tcp.py nodes
```

Watch cluster:

```bash
python cli/tcp.py watch
```

Run a shell job on a specific node:

```bash
python cli/tcp.py exec node1 uptime
```

Run a Docker job on a specific node:

```bash
python cli/tcp.py exec node1 "echo hello" --image alpine
```

Declare a shell workload:

```bash
python cli/tcp.py deploy workers uptime 3
```

Declare a Docker workload:

```bash
python cli/tcp.py deploy alpine-workers "echo hello" 2 --image alpine
```

Declare a workload with a label constraint:

```bash
python cli/tcp.py deploy eu-workers uptime 3 --constraint region=eu
```

List all jobs:

```bash
python cli/tcp.py jobs
```

---

# Debugging Docker Jobs

If Docker jobs are stuck on `pending`, check these in order:

```bash
# is Docker running?
docker ps

# did containers start and exit?
docker ps -a

# test the image manually
docker run --rm --network none alpine echo hello
```

If nothing appears in `docker ps -a` while jobs sit pending, the agent is
hitting a silent exception. Check the agent terminal output.

---

# Running Tests

```bash
pytest
```

Tests are synchronous and call `reconcile_once()` directly. No async timing,
no `sleep(1)` and hope. Each test gets an isolated SQLite database via
`tmp_path`.

If tests fail with schema errors, delete `cluster.db` and restart the
controller. The schema has evolved across versions and old database files
will not have the right columns.

---

# Disclaimer

This project was built primarily for learning and experimentation.

It is **not production ready**.

But it *is* a great example of how surprisingly small the core of an
orchestrator can be.

Also proof that vibecoding sometimes works.

Do not tell the enterprise architects.

Do not tell them about the patch cycle either.

Do not tell them we added Docker support and immediately forgot to start Docker.
