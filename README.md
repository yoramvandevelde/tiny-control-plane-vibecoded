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
* workload lifecycle management
* formal job states with failure handling
* job leases and lost job detection
* a CLI that tells you what is actually happening
* scaling workloads up and down
* per-node authentication and revocation

Which is suspiciously close to a real orchestrator.

Apparently vibecoding scales.

Then an AI reviewed the vibe-coded code, found actual bugs, fixed them, and
introduced new bugs in the tests.

Apparently vibecoding also has a patch cycle now.

> "We fixed the reconciler. The tests were wrong.
> Then the patch was wrong. Then the patch path was wrong.
> Three iterations to fix four bugs. This is just software."

Then someone asked: "can we stop workers?"

It turned out we had built an orchestrator with no off switch.
The reconciler would just keep scheduling jobs until the heat death of the universe,
or until someone pulled the plug on the controller.

> "We built desired state without building undesired state.
> In our defense, Kubernetes took three years to add this too."

Then we added formal job states. It turned out we had been using the string
`"finished"` to mean success, which is fine until you need to distinguish
success from failure from disappearance.

> "Any sufficiently advanced status string is indistinguishable from a bug.
> We had four states that needed five names and one that needed to be retired."

Then the scheduler was putting all jobs on the same node. It turned out that
with `sleep 1000` running, CPU stays near zero everywhere, and floating point
noise consistently picked the same winner. We switched to job count as the
primary score and added a random shuffle to break ties.

> "Our scheduler was doing least-loaded selection.
> It was selecting correctly. The load was just wrong."

Then someone asked: "can we see what is actually running?" It turned out we
had an orchestrator with no dashboard, no status view, and a `jobs` command
that printed raw JSON. The `status` command was added.

Do not tell the enterprise architects any of this.

Then someone asked: "can we scale back workers?" It turned out there was no
way to reduce replicas without undeploying the whole workload. We added a
`scale` command. Excess jobs are marked `lost` immediately — they keep running
on the agent, but the controller stops pretending they count.

> "Desired state is easy to declare.
> Undesiring state is where it gets interesting."

Then someone asked: "what stops a rogue agent from impersonating a node?"
It turned out the answer was: nothing. Every agent could talk to the controller
freely. We added a bootstrap token for registration and per-node tokens for
everything else.

> "We built a distributed system and forgot to lock the front door.
> In our defense, so did Kubernetes. They just had more time before anyone noticed."

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
3. schedules jobs using label constraints and job-count + CPU binpacking
4. maintains desired workloads via reconciliation
5. expires lost jobs via lease timeouts
6. removes workloads when asked nicely

---

# Features

### Authentication

The controller uses a two-token system.

**Bootstrap token** — a shared secret configured via the `TCP_BOOTSTRAP_TOKEN`
environment variable on both the controller and each agent. Used only for the
initial registration request. If it is wrong or missing, the agent exits
immediately rather than running unauthenticated.

**Node token** — a UUID issued by the controller on successful registration.
The agent stores it in memory and sends it as `X-Node-Token` on every
subsequent request. Each node gets its own token, so a single node can be
revoked without affecting the others.

To revoke a node:

```bash
python cli/tcp.py revoke node1
```

The node's next request will receive a 401. It logs the rejection and backs
off for 10 seconds rather than crashing — crashing would lose any in-memory
job state. Re-registering issues a fresh token; the old one is immediately
invalid.

> "We added authentication after building the entire orchestrator.
> To be fair, so did everyone else."

---

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

and execute commands locally — either as a shell command or inside a Docker
container, depending on whether an image is specified.

Jobs run in background threads on the agent, so a node can pick up new work
while existing jobs are still running.

---

### Job Lifecycle States

Every job moves through a defined set of states:

```
pending → running → succeeded
                  → failed
                  → lost
```

`pending` — job has been scheduled, not yet picked up by an agent.

`running` — agent has claimed the job and is executing it.

`succeeded` — job completed with exit code 0.

`failed` — job completed with a non-zero exit code.

`lost` — job was running but the agent disappeared before reporting a result.

Only `pending` and `running` jobs count as active. The reconciler uses this
to determine how many new jobs to create for a workload. A `lost` job is
treated as finished — the reconciler will schedule a replacement on the next pass.

---

### Job Leases

When an agent picks up a job it receives a lease that expires after 60 seconds.
The agent renews the lease every 15 seconds by sending a heartbeat:

```
POST /agent/heartbeat/<job_id>
```

If the agent dies mid-job, heartbeats stop. The next reconcile pass will find
the expired lease and mark the job `lost`, after which a replacement is scheduled.

---

### Scheduler

Jobs are placed using:

1. **Label constraint matching** — only nodes whose labels satisfy all
   constraints are considered
2. **Health and staleness filter** — unhealthy or stale nodes are excluded
3. **Least active jobs** — the node with the fewest pending+running jobs is preferred
4. **Lowest CPU** — used as a tiebreaker when job counts are equal
5. **Random shuffle** — candidates are shuffled before sorting so exact ties
   are broken randomly rather than by dict ordering or floating point noise

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

If jobs finish, fail, or get lost → the controller starts new ones.

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

### Scaling Workloads

To change the number of replicas for a running workload:

```bash
python cli/tcp.py scale workers 1
```

Scaling up schedules new jobs on the next reconcile pass. Scaling down marks
excess active jobs as `lost` immediately — pending jobs are cancelled before
running ones. The reconciler will not schedule replacements for lost jobs once
the replica count is satisfied.

Response:

```json
{"ok": true, "replicas": 1, "cancelled": 2}
```

Note: marking a job `lost` does not kill the subprocess on the agent. The
agent will finish executing and post a result, which the controller will
accept. Job cancellation at the process level is a future concern.

---

### Stopping Workloads

To stop a workload, undeploy it:

```bash
python cli/tcp.py undeploy workers
```

This removes the workload from the controller. The reconciler will no longer
schedule new jobs for it on the next pass.

Jobs that are already running complete normally — they are not killed.
Only future scheduling is stopped.

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

Set the bootstrap token (required on both controller and agent):

```bash
export TCP_BOOTSTRAP_TOKEN=your-secret-here
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

Agents can also register with labels:

```bash
python agent/agent.py --node-id node1 --port 9000 --label region=eu
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

Stop a workload:

```bash
python cli/tcp.py undeploy workers
```

Scale a workload:

```bash
python cli/tcp.py scale workers 1
```

Revoke a node:

```bash
python cli/tcp.py revoke node1
```

Show job status with elapsed times:

```bash
python cli/tcp.py status
```

List all jobs (raw JSON):

```bash
python cli/tcp.py jobs
```

---

## Status View

The `status` command shows a formatted table of all jobs:

```
 job id    node    workload   command   status      elapsed
 ──────────────────────────────────────────────────────────
 3a1f9c2b  node1   workers    uptime    running     1m 4s
 7bd02e11  node2   workers    uptime    running     1m 2s
 c4e88a30  node1   workers    uptime    succeeded   0s
```

For active jobs, elapsed counts from creation. For terminal jobs, it shows
how long the job actually ran.

---

## Logs

Workload output is captured by agents and stored by the controller.

You can retrieve logs for any job:

```bash
python cli/tcp.py logs <job_id>
```

Example:

```bash
$ python cli/tcp.py logs 6c1f5a4e
starting worker
processing item 1
processing item 2
done
```

Logs are collected from the workload's stdout and stored in the controller database.

This works for both shell commands and Docker-based workloads.

---

# Running Tests

```bash
pytest
```

Tests are synchronous and call `reconcile_once()` directly. No async timing,
no `sleep(1)` and hope. Each test gets an isolated SQLite database via
`tmp_path`.

---

# Disclaimer

This project was built primarily for learning and experimentation.

It is **not production ready**.

But it *is* a great example of how surprisingly small the core of an
orchestrator can be.

Also proof that vibecoding sometimes works.

Do not tell the enterprise architects.

Do not tell them about the patch cycle either.

Do not tell them it took a user asking "can we stop workers?" for us to notice
there was no off switch.

Do not tell them we had a state called `"finished"` that meant success, and
only noticed when we needed to add failure.

Do not tell them the scheduler was putting all jobs on the same node because
`sleep 1000` uses no CPU and floating point picked a favourite.

Do not tell them we added `status` because someone asked "but what is it
actually doing?" and the honest answer was "we have no idea, here is some JSON."

Do not tell them scaling down marks jobs as `lost` while they keep running.
The state is correct. The process is uninformed.

Do not tell them we built the entire system before adding authentication.
The bootstrap token was always part of the plan.
We just forgot to write it down until someone asked.
