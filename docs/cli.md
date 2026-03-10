# CLI Reference

The CLI binary is `tcp`. It reads the operator token from `~/.tcp/operator.token` or the `TCP_OPERATOR_TOKEN` environment variable. If neither is present it exits immediately.

---

## Command Summary

```bash
tcp nodes                                                # list nodes
tcp topology [-f]                                        # cluster tree view; -f live
tcp exec <node> <command> --image <image>                # run one-off job
tcp deploy <n> <command> <replicas> \
    --image <image> [--constraint key=value]             # declare workload
tcp scale <n> <replicas>                                 # change replica count
tcp undeploy <n>                                         # remove workload, cancels jobs
tcp revoke <node>                                        # revoke node token
tcp status [-f]                                          # job status table; -f live
tcp jobs                                                 # raw job list (JSON)
tcp logs <job_id> [-f]                                   # job output; -f stream live
tcp events [-f]                                          # recent cluster events; -f stream live
```

`--image` is required on both `tcp exec` and `tcp deploy`.

---

## Commands

### `tcp nodes`

Lists all registered nodes with their labels, health status, and resource usage.

---

### `tcp exec <node> <command> --image <image>`

Runs a one-off job on the specified node. The job runs inside a Docker container and the job ID is printed on submission.

```bash
tcp exec node1 "echo hello" --image alpine
```

---

### `tcp deploy <name> <command> <replicas> --image <image>`

Declares a workload with a desired replica count. The controller continuously ensures this many replicas are running.

```bash
tcp deploy alpine-workers "echo hello" 2 --image alpine
tcp deploy eu-workers uptime 3 --image alpine --constraint region=homelab
```

Use `--constraint key=value` to restrict scheduling to nodes with matching labels. Multiple `--constraint` flags are ANDed together.

---

### `tcp scale <name> <replicas>`

Changes the replica count of a running workload.

```bash
tcp scale workers 5
```

Scaling up schedules new jobs immediately. Scaling down cancels excess jobs — pending jobs are cancelled before running ones.

---

### `tcp undeploy <name>`

Cancels all running and pending jobs for the workload and removes it from the controller. Running containers are stopped immediately, not left to complete.

```bash
tcp undeploy workers
```

---

### `tcp revoke <node>`

Revokes the token for a node. The node's next request receives a 401 and the agent exits immediately.

```bash
tcp revoke node1
```

---

### `tcp status [-f]`

Displays a formatted table of all jobs with node, workload, command, colour-coded status, and elapsed time. Active jobs show time since creation; terminal jobs show how long they actually ran.

`-f` redraws the table live every second.

---

### `tcp topology [-f]`

Displays a tree rooted at the cluster, with nodes as branches and their active jobs as leaves. Each node shows current CPU, memory, and job count. The cluster root shows aggregate node count, job count, and average CPU.

`-f` redraws live every second.

---

### `tcp jobs`

Prints the raw job list as JSON. Useful for scripting.

---

### `tcp logs <job_id> [-f]`

Prints captured container stdout for a job.

`-f` streams output live via SSE and exits when the job reaches a terminal state.

---

### `tcp events [-f]`

Prints the most recent 200 cluster events.

`-f` streams new events live via SSE.

---

## Event Kinds

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
