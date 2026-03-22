# CLAUDE.md

Guidance for Claude Code when working with this repository.

## Project Overview

**satdeploy** is a deployment system for embedded Linux targets (satellites) with versioned backups, dependency-aware service management, and one-command rollback. It supports both SSH and CSP (Cubesat Space Protocol) transports.

### Components

| Component | Language | Purpose |
|-----------|----------|---------|
| **satdeploy** | Python | Ground station CLI - orchestrates deployments |
| **satdeploy-agent** | C | Runs on ARM target - handles CSP deploy commands |
| **satdeploy-apm** | C | csh slash commands for ground station |

## Build Commands

### satdeploy-agent (ARM cross-compile)

**CRITICAL:** This runs on ARM targets. Always cross-compile:

```bash
source /opt/poky/environment-setup-armv8a-poky-linux
cd satdeploy-agent
meson setup build-arm --cross-file yocto_cross.ini --wipe
ninja -C build-arm
# Output: build-arm/satdeploy-agent
```

The `build/` directory is for x86 native testing only - never deploy it.

### satdeploy-apm (Ground station)

```bash
cd satdeploy-apm
meson setup build --wipe
ninja -C build
# Install: cp build/libcsh_satdeploy_apm.so /root/.local/lib/csh/
```

### Python CLI

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

## Architecture

### Transport Abstraction

The CLI uses a transport layer (`satdeploy/transport/`) supporting:

- **SSH** (`ssh.py`) - Traditional SSH/SFTP for direct network access
- **CSP** (`csp.py`) - Cubesat Space Protocol over ZMQ for satellite links

Both implement the same interface: `deploy()`, `rollback()`, `get_status()`, `list_backups()`, `verify()`.

### Python Modules

| Module | Purpose |
|--------|---------|
| `cli.py` | Click command handlers, main orchestration |
| `config.py` | YAML config loading, per-target flat format |
| `transport/base.py` | Abstract transport interface |
| `transport/ssh.py` | SSH/SFTP implementation |
| `transport/csp.py` | CSP/DTP implementation |
| `deployer.py` | Backup creation, binary upload, hash verification |
| `services.py` | systemd service management |
| `dependencies.py` | Topological sort for service ordering |
| `history.py` | SQLite deployment tracking |
| `output.py` | CLI formatting (colors, symbols, steps) |
| `csp/dtp_server.py` | DTP server for serving binaries to satellite |

### satdeploy-agent (C)

Runs on target, listens on CSP port 20 for protobuf commands:

| Command | Action |
|---------|--------|
| `STATUS` | Return app statuses with hashes |
| `DEPLOY` | Stop app, backup, download via DTP, install, start |
| `ROLLBACK` | Restore from backup directory |
| `LIST_VERSIONS` | List available backups |
| `VERIFY` | Return SHA256 of installed binary |

**Interfaces:** ZMQ (default), CAN, KISS serial

**Dependencies:** libcsp, libparam, DTP, protobuf-c

### satdeploy-apm Slash Commands

Ground station csh module providing:
- `satdeploy status` - Query agent status
- `satdeploy deploy <app>` - Deploy binary
- `satdeploy rollback <app>` - Rollback
- `satdeploy list <app>` - List backups
- `satdeploy verify <app>` - Verify checksum

## CLI Commands

```bash
satdeploy init                      # Interactive setup
satdeploy deploy <app>              # Deploy binary (alias: push)
satdeploy deploy <app> --local ./path # Deploy with path override
satdeploy deploy --all              # Deploy all apps
satdeploy deploy --require-clean    # Refuse to deploy from dirty git tree
satdeploy status                    # Show all app statuses with git provenance
satdeploy list <app>                # List versions (deployed + backups)
satdeploy rollback <app>            # Restore previous version
satdeploy rollback <app> <hash>     # Restore specific version
satdeploy logs <app>                # Show service logs
satdeploy config                    # Show current config
satdeploy demo start                # Start simulated satellite (Docker)
satdeploy demo stop                 # Stop simulator
satdeploy demo watch                # Stream agent logs
satdeploy demo eject                # Generate config for real hardware

# Switch targets with --config
satdeploy status --config ~/.satdeploy/som2/config.yaml
```

## Config Structure

Each target gets its own config directory (e.g. `~/.satdeploy/som1/config.yaml`):

```yaml
name: som1
transport: csp
zmq_endpoint: tcp://localhost:9600
agent_node: 5425
ground_node: 4040
appsys_node: 10

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
    depends_on: [csp_server]
    param: mng_controller     # libparam name (CSP only)

  libparam:
    local: ./build/libparam.so
    remote: /usr/lib/libparam.so
    service: null
    restart: [csp_server, controller]
```

The `name` field identifies this target in history records (defaults to `"default"`).

## Deployment Flow

### SSH Transport
1. Stop services (dependents first)
2. Backup current binary to `{backup_dir}/{app}/{timestamp}-{hash}.bak`
3. Upload via SFTP
4. Start services (dependencies first)
5. Health check
6. Record to history.db

### CSP Transport
1. Send DEPLOY command to agent (port 20)
2. Agent stops app via libparam
3. Agent backs up current binary
4. Agent downloads new binary via DTP from ground
5. Agent verifies checksum
6. Agent starts app via libparam

## Dependency Resolution

- **Stop order:** Dependents first (top-down)
- **Start order:** Dependencies first (bottom-up)

For libraries with `restart` lists, those services are used directly instead of computing the dependency graph.

## Testing

Tests use pytest with pytest-mock. Run with:

```bash
pytest                    # All tests
pytest tests/test_cli_push.py  # Single file
pytest -k "test_push"     # Pattern match
```

Test files mock SSH/CSP connections - no real network calls.

## Protocol Details

### CSP Ports (Agent)
- **Port 20:** Deploy command handler (protobuf)
- **Port 7:** DTP metadata requests
- **Port 8:** DTP data packets

### Backup Naming
Files are named: `{YYYYMMDD}-{HHMMSS}-{hash8}.bak`

Hash is first 8 chars of SHA256 (all components: ground, agent, APM).
