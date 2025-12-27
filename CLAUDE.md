# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

sat-deploy is a CLI tool for deploying binaries to embedded Linux targets via SSH. It provides versioned backups, dependency-aware service restarts, and one-command rollback.

## Build & Development Commands

```bash
# Install in development mode
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run all tests
python -m pytest

# Run a single test file
python -m pytest tests/test_cli_push.py

# Run a specific test
python -m pytest tests/test_cli_push.py::TestPushCommand::test_push_requires_app_name

# Run tests with output
python -m pytest -v
```

## CLI Usage

```bash
satdeploy init                     # Interactive setup
satdeploy push <app>               # Deploy binary
satdeploy status                   # Show service states
satdeploy list <app>               # List all versions (deployed + backups)
satdeploy rollback <app>           # Restore previous version
satdeploy logs <app>               # Show journalctl logs
```

## Architecture

### Module Responsibilities

- **cli.py**: Click command handlers - orchestrates the workflow using other modules
- **ssh.py**: SSH connection wrapper around paramiko - `SSHClient` context manager for connections
- **deployer.py**: Backup/deploy/rollback logic - handles file operations on remote
- **services.py**: Systemd service management - start/stop/status via SSH
- **dependencies.py**: Topological sort for service stop/start order based on `depends_on` config
- **history.py**: SQLite database for tracking deployments in `~/.satdeploy/history.db`
- **config.py**: YAML config loading from `~/.satdeploy/config.yaml`
- **output.py**: CLI output formatting (symbols, colors, step counters)

### Deployment Flow

When `push` is called:
1. Load config, resolve dependencies
2. Stop services top-down (dependents first)
3. Backup current remote binary to `{backup_dir}/{app}/{timestamp}.bak`
4. Upload new binary via SFTP
5. Start services bottom-up (dependencies first)
6. Log to history.db

### Dependency Resolution

The `DependencyResolver` builds a graph from `depends_on` config entries. For libraries with `restart` lists, it uses those directly instead of computing dependencies.

Stop order: Dependents first (top-down)
Start order: Dependencies first (bottom-up)

### Config Structure

```yaml
target:
  host: 192.168.1.50
  user: root

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  controller:
    local: ./build/controller        # Local binary path
    remote: /opt/disco/bin/controller # Remote deployment path
    service: controller.service       # Systemd service (null for libraries)
    depends_on: [csp_server]          # Services this depends on

  libparam:
    service: null
    restart: [csp_server, controller] # Services to restart when lib changes
```

## Testing

Tests use pytest with pytest-mock. Each CLI command has its own test file (`test_cli_*.py`). Module tests mock SSH connections and verify behavior without real network calls.

Test config fixtures create temporary `~/.satdeploy` directories with sample config.yaml files.
