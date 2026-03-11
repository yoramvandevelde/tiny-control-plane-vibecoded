# CLI Reference

The CLI binary is `tcp`. It reads the operator token from `~/.tcp/operator.token` or the `TCP_OPERATOR_TOKEN` environment variable. If neither is present it exits immediately.

---

## Command Summary

```bash
tcp nodes                                                # list nodes
tcp topology [-f]                                        # cluster tree view; -f live
tcp exec <node> <command> --image <image>                # run one-off job
tcp deploy <n> <command> <replicas> \
    --image <image> [--constraint key=value] \
    [--max-attempts N]                                   # declare workload (default unlimited)
tcp scale <n> <replicas>                                 # change replica count
tcp undeploy <n>                                         # remove workload, cancels jobs
tcp workloads                                            # list workloads with replica health
tcp describe job <id>                                    # detailed job info + recent logs
tcp describe node <id>                                   # detailed node info + active jobs
tcp describe workload <n>                                # detailed workload info + all jobs
tcp revoke <node>                                        # revoke node token
tcp status [-f]                                          # job status table; -f live
tcp jobs                                                 # raw job list (JSON)
tcp logs <job_id> [-f]                                   # job output; -f stream live
tcp events [-f]                                          # recent cluster events; -f stream live
tcp gc [--days N] [--dry-run]                            # delete old jobs and events
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

### `tcp deploy <n> <command> <replicas> --image <image>`

Declares a workload with a desired replica count. The controller continuously ensures this many replicas are running.

```bash
tcp deploy alpine-workers "echo hello" 2 --image alpine
tcp deploy eu-workers uptime 3 --image alpine --constraint region=homelab
tcp deploy flaky-job "python run.py" 2 --image python:3.12 --max-attempts 5
```

Use `--constraint key=value` to restrict scheduling to nodes with matching labels. Multiple `--constraint` flags are ANDed together.

`--max-attempts` caps how many times the reconciler will reschedule a failing replica before considering the workload exhausted. By default there is no limit — the reconciler will keep scheduling replacements indefinitely. Set `--max-attempts N` to stop after N failures. When the limit is reached the reconciler emits a `workload.exhausted` event and stops scheduling — the workload definition stays in place so you can fix the command and redeploy. Cancelled jobs do not count against the attempt limit.

---

### `tcp scale <n> <replicas>`

Changes the replica count of a running workload.

```bash
tcp scale workers 5
```

Scaling up schedules new jobs immediately. Scaling down cancels excess jobs — pending jobs are cancelled before running ones.

---

### `tcp undeploy <n>`

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

### `tcp workloads`

Lists all declared workloads with their image, command, desired replica count, how many replicas are currently active, and the `max-attempts` limit. The running count is shown in green when it meets the desired count, yellow when it falls short. A `max-attempts` of `0` is shown as `unlimited`.

```
name      image    command     desired  running  max-attempts  constraints
workers   alpine   sleep 100   3        3        unlimited     region=eu
```

---

### `tcp describe job <id>`

Shows full details for a job: status, node, workload, image, command, elapsed time, resource requests, and exit result. Appends the last 10 log lines with a hint to use `tcp logs` for the full output.

`<id>` can be a prefix — the shortest unambiguous prefix is enough.

```bash
tcp describe job 7b2e31
```

---

### `tcp describe node <id>`

Shows full details for a node: health, version, address, last-seen age, capacity, current CPU/memory usage, labels, and active job count. Appends a live job table for any jobs currently running on the node.

```bash
tcp describe node node1
```

---

### `tcp describe workload <n>`

Shows full details for a workload: image, command, active vs desired replica count, max-attempts, resource requests, and constraints. Appends a job table covering all jobs ever scheduled for the workload (active and terminal).

```bash
tcp describe workload workers
```

---

### `tcp logs <job_id> [-f]`

Prints captured container stdout for a job.

`-f` streams output live via SSE and exits when the job reaches a terminal state.

---

### `tcp events [-f]`

Prints the most recent 200 cluster events.

`-f` streams new events live via SSE.

---

### `tcp gc [--days N] [--dry-run]`

Removes old terminal jobs (succeeded/failed/cancelled/lost) and old events from the database. Log lines and cancel signal entries for pruned jobs are removed in the same operation.

```bash
tcp gc                  # delete rows older than 7 days (default)
tcp gc --days 30        # use a custom age threshold
tcp gc --dry-run        # preview what would be deleted without removing anything
```

`--dry-run` calls a read-only preview endpoint and prints row counts without deleting anything. Use it before running gc on a production cluster.

`cancel_jobs` rows are pruned automatically by the reconciler and do not require manual gc.

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
| `workload.exhausted` | Workload hit max-attempts limit, no further scheduling |
| `job.scheduled` | Job assigned to a node |
| `job.started` | Agent picked up the job |
| `job.succeeded` | Job exited 0 |
| `job.failed` | Job exited non-zero |
| `job.cancelled` | Job explicitly cancelled by operator action |
| `job.lost` | Job lost due to agent disappearance or lease expiry |
