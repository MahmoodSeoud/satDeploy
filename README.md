# sat-deploy

Fast binary deployment tool for embedded Linux flatsat (satellite engineering model).

## Components

- **sat-agent**: Runs on flatsat, handles deployment commands
- **sat**: CLI tool for developer laptop (not yet implemented)

## Requirements

- Python 3.8+
- PyYAML (`pip install pyyaml`)

## Current Status

### Completed

#### sat-agent (Phase 1 complete)

| Command | Status | Description |
|---------|--------|-------------|
| `status` | Done | Returns JSON with running/stopped state of all services |
| `deploy <service>` | Done | Stops dependents, swaps binary, restarts services |
| `rollback <service>` | Pending | Restore previous binary version |
| `restart <service>` | Pending | Restart service and dependents |

**Features implemented:**
- Configuration loading from YAML (path configurable via `SAT_AGENT_CONFIG` env var)
- Dependency-aware service restarts (topological ordering)
- Atomic binary deployment (backup, swap, chmod +x)
- Version logging to `versions.json`
- JSON output for all commands
- Error handling with JSON error responses

### Pending

#### sat CLI (Phase 2)
- `sat status` - SSH to agent, display formatted output
- `sat deploy <service> <binary>` - rsync + SSH to agent
- `sat rollback <service>` - Trigger rollback via SSH
- `sat logs <service>` - Tail journalctl logs
- `sat restart <service>` - Restart via SSH

#### Rollback (Phase 3)
- Agent rollback command
- CLI rollback command

#### Polish (Phase 4)
- Timing output ("Deployed in 34s")
- install-agent.sh script
- Better error messages

## Development

### Testing

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install pytest pyyaml

# Run all tests (27 tests)
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
| Config loading | 2 | Pass |
| Service status check | 2 | Pass |
| Status command | 4 | Pass |
| Dependency resolution | 6 | Pass |
| Service control | 2 | Pass |
| Binary operations | 5 | Pass |
| Deploy command | 6 | Pass |
| **Total** | **27** | **All passing** |

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
|  - logs          |                     |                  |
+------------------+                     +------------------+
```

## Usage

See `plan.md` for full specification.

### sat-agent Commands (on flatsat)

```bash
# Check status of all services
./sat_agent.py status
# {"status": "ok", "services": {"controller": "running", ...}}

# Deploy a service (binary must be uploaded as <path>.new first)
./sat_agent.py deploy controller
# {"status": "ok", "service": "controller", "hash": "a3f2c9b1"}
```

## File Structure

```
sat-deploy/
├── sat_agent.py              # Agent script (runs on flatsat)
├── sat                       # CLI script (pending)
├── config.yaml               # Configuration (pending)
├── pyproject.toml            # Project configuration
├── tests/
│   └── test_sat_agent.py     # 27 unit tests
├── notes/
│   └── features/             # Feature development notes
├── plan.md                   # Full specification
└── README.md
```
