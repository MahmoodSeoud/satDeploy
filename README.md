# sat-deploy

A CLI tool for deploying binaries to embedded Linux targets with versioned backups, dependency-aware service restarts, and one-command rollback.

## The Problem

Deploying binaries to an embedded Linux target during development is manual and error-prone. You're either using a janky uploader, a USB stick, or SSH + prayer. No versioning, no rollback, no dependency awareness.

## The Solution

A CLI tool that deploys binaries to their real paths, keeps versioned backups, restarts services in dependency order, and lets you rollback in one command.

## Installation

```bash
# Clone the repository
git clone https://github.com/MahmoodSeoud/satBuild.git
cd satBuild

# Create virtual environment and install
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Quick Start

```bash
# Initialize configuration (interactive)
satdeploy init

# Deploy an app
satdeploy push controller

# Check status of all apps
satdeploy status

# Rollback to previous version
satdeploy rollback controller

# View available backups
satdeploy list controller

# View service logs
satdeploy logs controller
```

## Commands

| Command | Description |
|---------|-------------|
| `satdeploy init` | Interactive setup, creates config.yaml |
| `satdeploy push <app>` | Deploy binary, backup old, restart services |
| `satdeploy push <app> --local ./path` | Deploy with local path override |
| `satdeploy status` | Show status of all apps and services |
| `satdeploy list <app>` | List all versions (deployed + backups) |
| `satdeploy rollback <app>` | Restore previous version |
| `satdeploy rollback <app> <hash>` | Restore specific version by hash |
| `satdeploy logs <app>` | Show journalctl logs for service |
| `satdeploy logs <app> -n 50` | Show last 50 lines of logs |

## Configuration

Configuration is stored in `~/.satdeploy/config.yaml`:

```yaml
target:
  host: 192.168.1.50
  user: root

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
    depends_on: [csp_server]

  csp_server:
    local: ./build/csp_server
    remote: /usr/bin/csp_server
    service: csp_server.service

  libparam:
    local: ./build/libparam.so
    remote: /usr/lib/libparam.so
    service: null
    restart: [csp_server, controller]
```

### Configuration Options

| Field | Description |
|-------|-------------|
| `target.host` | Target device IP or hostname |
| `target.user` | SSH user (default: root) |
| `backup_dir` | Remote directory for backups |
| `max_backups` | Max backups per app (oldest deleted when exceeded) |
| `apps.<name>.local` | Local path to binary |
| `apps.<name>.remote` | Remote deployment path |
| `apps.<name>.service` | Systemd service name (null for libraries) |
| `apps.<name>.depends_on` | Services this app depends on |
| `apps.<name>.restart` | Services to restart when this library changes |

## Dependency Resolution

When deploying an app with dependencies, sat-deploy automatically:

1. **Stops services top-down** (dependents first, then the service itself)
2. **Deploys the binary**
3. **Starts services bottom-up** (the service first, then dependents)

Example: If `controller` depends on `csp_server` which depends on `param_handler`:

```
Stop order:  controller → csp_server → param_handler
Start order: param_handler → csp_server → controller
```

## Example Session

```
$ satdeploy status
Target: 192.168.1.50 (root)

    APP              STATUS        	HASH       TIMESTAMP
    ------------------------------------------------------------
  ▸ controller      running       	a3f2c9b8  2024-01-15 14:30:22
  ▸ csp_server      running       	b7e1d2a4  2024-01-15 09:15:44
  • libparam        deployed      	c4d5e6f1  2024-01-12 16:23:01

$ satdeploy push controller
Connecting to 192.168.1.50...
Deploying controller...
[1/4] Stopping controller (controller.service)
[2/4] Backing up root@192.168.1.50:/opt/disco/bin/controller
[3/4] Uploading ./build/controller
                → root@192.168.1.50:/opt/disco/bin/controller
[4/4] Starting controller (controller.service)
▸ Deployed controller (e5f6a7b9)

$ satdeploy list controller
Versions for controller:

    HASH       TIMESTAMP            STATUS
    ---------------------------------------------
  → e5f6a7b9  2024-01-15 14:35:10  deployed
  • a3f2c9b8  2024-01-15 14:30:22  backup
  • d2c3b4a5  2024-01-14 09:15:00  backup

$ satdeploy logs controller -n 5
Logs for controller (controller.service):

Jan 15 14:35:12 flatsat systemd[1]: Started controller.service.
Jan 15 14:35:12 flatsat controller[1234]: Initializing...
Jan 15 14:35:13 flatsat controller[1234]: Connected to csp_server
Jan 15 14:35:13 flatsat controller[1234]: Ready

$ satdeploy rollback controller
Connecting to 192.168.1.50...
Rolling back controller...
[1/3] Stopping controller (controller.service)
[2/3] Restoring a3f2c9b8
[3/3] Starting controller (controller.service)
▸ Rolled back controller to a3f2c9b8
```

## Requirements

- Python 3.8+
- SSH access to target device
- systemd on target device

### Dependencies

- click - CLI framework
- paramiko - SSH connection
- PyYAML - Config parsing

## License

MIT
