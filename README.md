# sat-deploy

Fast binary deployment tool for embedded Linux flatsat (satellite engineering model).

## Components

- **sat-agent**: Runs on flatsat, handles deployment commands
- **sat**: CLI tool for developer laptop

## Requirements

- Python 3.8+
- PyYAML (`pip install pyyaml`)

## Quick Start

```bash
# Install sat-agent on flatsat
./install-agent.sh flatsat-disco.local

# Check status
./sat.py status

# Deploy a binary
./sat.py deploy controller ./build/controller
```

## Current Status

All phases complete.

### sat-agent

| Command | Status | Description |
|---------|--------|-------------|
| `status` | Done | Returns JSON with running/stopped state of all services |
| `deploy <service>` | Done | Stops dependents, swaps binary, restarts services |
| `rollback <service>` | Done | Restore previous binary version |
| `restart <service>` | Done | Restart service and dependents |

**Features:**
- Configuration loading from YAML (path configurable via `SAT_AGENT_CONFIG` env var)
- Dependency-aware service restarts (topological ordering)
- Atomic binary deployment (backup, swap, chmod +x)
- Version logging to `versions.json`
- JSON output for all commands
- Error handling with JSON error responses

### sat CLI

| Command | Status | Description |
|---------|--------|-------------|
| `status` | Done | SSH to agent, display formatted output |
| `deploy <service> <binary>` | Done | rsync + SSH to agent, shows timing |
| `rollback <service>` | Done | Trigger rollback via SSH |
| `restart <service>` | Done | Restart via SSH |
| `logs <service>` | Done | Tail journalctl logs |

**Features:**
- Configuration loading from YAML (path configurable via `SAT_CONFIG` env var)
- SSH command execution to remote flatsat
- rsync upload of binaries to remote host
- Timing output for deploy command
- Nice terminal output with checkmarks/X marks
- Error handling with helpful hints

## Development

### Testing

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install pytest pyyaml

# Run all tests
python -m pytest tests/ -v
```

### Configuration

The sat-agent config path can be overridden for testing:

```bash
export SAT_AGENT_CONFIG=/path/to/test/config.yaml
```

Default production path: `/opt/sat-agent/config.yaml`

### Test Coverage

| Module | Tests | Status |
|--------|-------|--------|
| **sat-agent** | | |
| Config loading | 2 | Pass |
| Service status check | 2 | Pass |
| Status command | 4 | Pass |
| Dependency resolution | 6 | Pass |
| Service control | 2 | Pass |
| Binary operations | 5 | Pass |
| Deploy command | 6 | Pass |
| Rollback command | 8 | Pass |
| Restart command | 4 | Pass |
| Main CLI | 2 | Pass |
| **sat CLI** | | |
| Config loading | 3 | Pass |
| SSH execution | 2 | Pass |
| rsync upload | 4 | Pass |
| Status command | 5 | Pass |
| Deploy command | 6 | Pass |
| Rollback command | 5 | Pass |
| Timing output | 3 | Pass |
| Logs command | 3 | Pass |
| Restart command | 5 | Pass |
| Main CLI | 3 | Pass |
| **Total** | **80** | **All passing** |

## Architecture

```
Developer Laptop                         Flatsat (Yocto Linux)
+------------------+                     +------------------+
|                  |    SSH + rsync      |                  |
|  sat (CLI)       | ------------------> |  sat-agent       |
|                  |                     |                  |
|  - deploy        |    JSON responses   |  - deploy        |
|  - status        | <------------------ |  - status        |
|  - rollback      |                     |  - rollback      |
|  - restart       |                     |  - restart       |
|  - logs          |                     |                  |
+------------------+                     +------------------+
```

## Usage

See `plan.md` for full specification.

### Installing the Agent

```bash
# Install to default host (flatsat-disco.local)
./install-agent.sh

# Install to specific host
./install-agent.sh my-flatsat.local
```

### sat-agent Commands (on flatsat)

```bash
# Check status of all services
./sat-agent status
# {"status": "ok", "services": {"controller": "running", ...}}

# Deploy a service (binary must be uploaded as <path>.new first)
./sat-agent deploy controller
# {"status": "ok", "service": "controller", "hash": "a3f2c9b1"}

# Restart a service and its dependents
./sat-agent restart controller
# {"status": "ok", "service": "controller"}
```

### sat CLI Commands (on developer laptop)

```bash
# Check status of all services
./sat.py status
# [+] controller: running
# [+] csp_server: running
# [+] param_handler: running

# Deploy a service (shows timing)
./sat.py deploy controller ./build/controller
# [~] Uploading controller...
# [~] Deploying controller...
# [+] Deployed controller (a3f2c9b1 in 28.5s)

# Rollback a service
./sat.py rollback controller
# [~] Rolling back controller...
# [+] Rolled back controller (prev_hash)

# Restart a service
./sat.py restart controller
# [~] Restarting controller...
# [+] Restarted controller

# Tail logs (Ctrl+C to exit)
./sat.py logs controller
```

## File Structure

```
sat-deploy/
├── sat_agent.py              # Agent script (runs on flatsat)
├── sat.py                    # CLI script (runs on developer laptop)
├── install-agent.sh          # Installation script
├── config.yaml               # Configuration for CLI
├── pyproject.toml            # Project configuration
├── tests/
│   ├── test_sat_agent.py     # Agent unit tests
│   └── test_sat.py           # CLI unit tests
├── notes/
│   └── features/             # Feature development notes
├── plan.md                   # Full specification
└── README.md
```
