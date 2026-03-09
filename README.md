
# Tiny Control Plane (Vibe-Coded Edition)

Welcome to **Tiny Control Plane**, a small distributed systems playground written in Python.

This project demonstrates the core concepts behind real orchestrators such as:

- Kubernetes
- Nomad
- Mesos
- CI worker fleets

But instead of thousands of lines of Go, YAML, and existential dread…

we built it with **Python, curiosity, and a healthy amount of vibecoding.**

Yes, this entire mini‑orchestrator was vibe‑coded.

Which means:

> "I vaguely know what I'm doing, the architecture makes sense in my head,
> and the code appears to work."

Somehow, this still resulted in:

- agents
- cluster membership
- health checks
- job scheduling
- resource‑aware binpacking
- reconciliation loop

Which is suspiciously close to a real orchestrator.

Apparently vibecoding scales.

---

# Architecture

The system has three components:

controller → central brain  
agent → node worker  
cli → operator tool  

Agents poll the controller for jobs.

The controller:

1. tracks nodes
2. evaluates health
3. schedules jobs
4. maintains desired workloads

---

# Features

### Node Registration

Agents register themselves with:

- labels
- resource capacity

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

- CPU usage
- memory usage
- disk free

Controller marks nodes unhealthy if thresholds are exceeded.

---

### Job Queue

Jobs are stored in SQLite.

Agents poll:

```
GET /agent/jobs/<node>
```

and execute commands locally.

---

### Scheduler

Jobs are placed using:

1. label constraints
2. resource checks
3. simple binpacking

The node with **least remaining capacity** is chosen.

---

### Workloads (Desired State)

Instead of running a job once you can declare:

```
replicas = 3
command = uptime
```

Controller ensures 3 copies are always running.

If jobs finish or nodes disappear → controller starts new ones.

This pattern is called **reconciliation**.

It is the core of Kubernetes.

---

# Running the System

Install dependencies:

```
pip install -r requirements.txt
```

Start controller:

```
./run-controller.sh
```

Start agents:

```
python agent/agent.py --node-id node1 --port 9000
python agent/agent.py --node-id node2 --port 9001
```

View nodes:

```
python cli/tcp.py nodes
```

Watch cluster:

```
python cli/tcp.py watch
```

Run job:

```
python cli/tcp.py exec node1 uptime
```

Declare workload:

```
python cli/tcp.py deploy workers uptime 3
```

---

# Testing

"This project contains three tests, which is roughly three more tests than most vibecoded infrastructure projects."

---

# Disclaimer

This project was built primarily for learning and experimentation.

It is **not production ready**.

But it *is* a great example of how surprisingly small the core of an orchestrator can be.

Also proof that vibecoding sometimes works.

Do not tell the enterprise architects.
