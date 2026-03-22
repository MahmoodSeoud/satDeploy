# Docker Development Guide (macOS)

CSH, satdeploy-apm, and satdeploy-agent are Linux-only. Docker lets you build and run everything on macOS.

## Prerequisites

- [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/)

## Quick Start

```bash
# Start zmqproxy + agent (builds C code on first run)
docker compose up -d

# Configure the Python CLI (one-time)
satdeploy init
# Accept defaults: transport=csp, endpoint=tcp://localhost:9600,
# agent_node=5425, ground_node=40

# Test it
satdeploy status
```

That's it. `docker compose up -d` starts zmqproxy (ports 9600/9601) and the satdeploy-agent. The Python CLI runs on your Mac and talks to them through the forwarded ports.

## What's running

| Service | Purpose | Port |
|---------|---------|------|
| zmqproxy | Routes CSP packets between ground and agent | 9600, 9601 |
| agent | Handles deploy/status/rollback commands | (via CSP) |
| build | Compiles C code (runs once, then exits) | — |

## Common commands

```bash
# Start everything
docker compose up -d

# Rebuild after C code changes
docker compose up build
docker compose restart agent

# View logs
docker compose logs -f agent
docker compose logs -f zmqproxy

# Stop everything
docker compose down

# Full rebuild (after Dockerfile changes)
docker compose build --no-cache
docker compose up -d
```

## Ports

zmqproxy uses ports 9600/9601 (not the libcsp defaults of 6000/7000) to avoid
conflicts with macOS AirPlay Receiver which holds port 7000. All components
(agent, Python CLI, CSH) are configured to match.

## Using CSH (ground station shell)

CSH needs an interactive terminal, so it uses a separate profile:

```bash
docker compose run --rm csh
```

Inside CSH:
```
apm load
satdeploy status -n 5425
satdeploy deploy -n 5425 -f /path/to/binary -r /opt/app/bin/app app_name
```

## Python CLI

The Python CLI runs natively on your Mac:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

satdeploy status                    # Query agent
satdeploy push app_name             # Deploy binary
satdeploy list app_name             # List versions
satdeploy rollback app_name         # Restore previous
```

## Architecture

```
┌─── Your Mac ───────────────────────────┐
│                                        │
│  satdeploy CLI (Python)                │
│    PUB → localhost:9600                │
│    SUB ← localhost:9601                │
│                                        │
└────────┬──────────────┬────────────────┘
         │              │
    port 9600      port 9601
         │              │
┌────────┴──────────────┴────────────────┐
│  Docker                                │
│                                        │
│  zmqproxy (XSUB:9600 ↔ XPUB:9601)    │
│       ↕                                │
│  satdeploy-agent (CSP node 5425)       │
│    - handles deploy commands           │
│    - manages backups                   │
│    - starts/stops apps via libparam    │
│                                        │
└────────────────────────────────────────┘
```

## Troubleshooting

### "Request timed out"
zmqproxy or agent isn't running. Check `docker compose ps` and `docker compose logs`.

### Port already in use
```bash
docker compose down
# If still stuck, check for leftover containers:
docker ps -a | grep satdev
```

### Rebuild C code
```bash
docker compose up build
docker compose restart agent
```

### CAN bus
CAN (SocketCAN) doesn't work in Docker on macOS. Use ZMQ transport only.
