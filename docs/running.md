# Running the System

## Prerequisites

- Python 3.x
- Docker (required — all jobs run in containers)

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Tokens

The system uses three tokens. Set them before starting any component.

**Bootstrap token** — shared between the controller and all agents. Used for agent registration.

```bash
export TCP_BOOTSTRAP_TOKEN=your-bootstrap-secret
```

Set this on both the controller host and every agent host.

**Operator token** — used by the CLI to authenticate with the controller.

```bash
export TCP_OPERATOR_TOKEN=your-operator-secret
```

Set this on the controller host. For the CLI, either export the same env var or write it to a file:

```bash
mkdir -p ~/.tcp && echo "your-operator-secret" > ~/.tcp/operator.token && chmod 600 ~/.tcp/operator.token
```

The file takes precedence over the env var. To rotate, update the value on the controller and on the CLI side — no command needed.

---

## Starting the Controller

```bash
./run-controller.sh
# or directly:
uvicorn controller.api:app --host 0.0.0.0 --port 8000 --reload --no-server-header
```

The controller listens on port 8000 by default and creates `cluster.db` in the working directory.

---

## Starting Agents

```bash
python agent/agent.py --node-id node1 --port 9000
python agent/agent.py --node-id node2 --port 9001 --label region=homelab
```

Available flags:

| Flag | Description |
|---|---|
| `--node-id` | Unique identifier for this node (required) |
| `--port` | Port the agent listens on |
| `--label key=value` | Attach one or more labels for scheduling constraints |
| `--address http://ip:port` | Override the advertised address (see below) |

---

## Multi-Host Deployment

### Controller address

By default agents and the CLI connect to `http://127.0.0.1:8000`. To connect from other machines, set the controller address:

```bash
export TCP_CONTROLLER=http://10.0.0.5:8000
```

Example — agent on a remote host:

```bash
TCP_CONTROLLER=http://10.0.0.5:8000 python agent/agent.py --node-id node2 --port 9000
```

Example — CLI on a remote host:

```bash
TCP_CONTROLLER=http://10.0.0.5:8000 tcp nodes
```

### Agent advertised address

When an agent registers it advertises its own address to the controller. By default the agent auto-detects its LAN IP by opening a temporary UDP socket toward the controller — no data is sent, the OS simply selects the correct outbound interface. If detection fails the agent falls back to `127.0.0.1` and logs a warning.

To override explicitly:

```bash
python agent/agent.py --node-id node1 --port 9000 --address http://10.0.0.10:9000
```

Use `--address` when the auto-detected IP is wrong — for example on hosts with multiple network interfaces or when running inside a container.

---

## Environment Variable Summary

| Variable | Used by | Purpose |
|---|---|---|
| `TCP_BOOTSTRAP_TOKEN` | Controller, Agent | Shared secret for agent registration |
| `TCP_OPERATOR_TOKEN` | Controller, CLI | Shared secret for operator access |
| `TCP_CONTROLLER` | Agent, CLI | Controller URL (default: `http://localhost:8000`) |
