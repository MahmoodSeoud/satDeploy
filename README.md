# satdeploy

Deploy binaries to embedded Linux targets with versioned backups, dependency-aware service restarts, and one-command rollback.

## Why

Deploying binaries to embedded targets during development is tedious. You're either using a janky uploader, a USB stick, or SSH + prayer. No versioning, no rollback, no dependency awareness.

satdeploy fixes this with:
- **Versioned backups** - Every deploy saves the previous binary
- **Dependency ordering** - Services stop/start in the right order
- **One-command rollback** - Instantly restore any previous version
- **Multi-transport** - Works over SSH or CSP (satellite links)
- **Fleet management** - Deploy across multiple targets

## Components

| Component | What it does |
|-----------|--------------|
| `satdeploy` (Python) | Ground station CLI |
| `satdeploy-agent` (C) | Runs on target, handles CSP deploys |
| `satdeploy-apm` (C) | csh slash commands for ground station |

## Installation

```bash
git clone https://github.com/MahmoodSeoud/satBuild.git
cd satBuild
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Quick Start

```bash
# Setup (interactive)
satdeploy init

# Deploy
satdeploy push controller

# Check status
satdeploy status

# Rollback
satdeploy rollback controller

# View backups
satdeploy list controller
```

## Commands

| Command | Description |
|---------|-------------|
| `satdeploy init` | Interactive setup |
| `satdeploy push <app>` | Deploy binary |
| `satdeploy push --all` | Deploy all apps |
| `satdeploy status` | Show app statuses |
| `satdeploy list <app>` | List all versions |
| `satdeploy rollback <app>` | Restore previous version |
| `satdeploy rollback <app> <hash>` | Restore specific version |
| `satdeploy logs <app>` | Show service logs |
| `satdeploy config` | Show configuration |

### Fleet Commands

| Command | Description |
|---------|-------------|
| `satdeploy fleet status` | Status across all modules |
| `satdeploy diff <m1> <m2>` | Compare versions between modules |
| `satdeploy sync <src> <dst>` | Sync target to match source |

## Configuration

Config lives at `~/.satdeploy/config.yaml`:

```yaml
modules:
  default:
    transport: ssh
    host: 192.168.1.50
    user: root

  satellite1:
    transport: csp
    zmq_endpoint: tcp://localhost:4040
    agent_node: 5424

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

### App Options

| Field | Description |
|-------|-------------|
| `local` | Path to local binary |
| `remote` | Deployment path on target |
| `service` | systemd service (null for libraries) |
| `depends_on` | Services this app depends on |
| `restart` | Services to restart when this library changes |
| `param` | libparam name for CSP start/stop |

## Transports

### SSH

Direct SSH/SFTP connection. Works with any Linux target.

```yaml
modules:
  flatsat:
    transport: ssh
    host: 192.168.1.50
    user: root
```

### CSP (Cubesat Space Protocol)

For satellite communication links. Requires `satdeploy-agent` running on target.

```yaml
modules:
  satellite:
    transport: csp
    zmq_endpoint: tcp://localhost:4040
    agent_node: 5424
    ground_node: 4040
```

## Dependency Resolution

When deploying an app with dependencies:

1. **Stop** services top-down (dependents first)
2. **Deploy** the binary
3. **Start** services bottom-up (dependencies first)

Example: `controller` depends on `csp_server`:
```
Stop:  controller → csp_server
Start: csp_server → controller
```

For libraries with a `restart` list, those services are restarted directly.

## Example Session

```
$ satdeploy status
Module: default (192.168.1.50)

    APP              STATUS        HASH       TIMESTAMP
    ------------------------------------------------------------
  > controller      running       a3f2c9b8  2024-01-15 14:30:22
  > csp_server      running       b7e1d2a4  2024-01-15 09:15:44
  - libparam        deployed      c4d5e6f1  2024-01-12 16:23:01

$ satdeploy push controller
[1/4] Stopping controller.service
[2/4] Backing up /opt/disco/bin/controller
[3/4] Uploading ./build/controller
[4/4] Starting controller.service
> Deployed controller (e5f6a7b9)

$ satdeploy list controller
Versions for controller:

    HASH       TIMESTAMP            STATUS
    ---------------------------------------------
  > e5f6a7b9  2024-01-15 14:35:10  deployed
  - a3f2c9b8  2024-01-15 14:30:22  backup
  - d2c3b4a5  2024-01-14 09:15:00  backup

$ satdeploy rollback controller
[1/3] Stopping controller.service
[2/3] Restoring a3f2c9b8
[3/3] Starting controller.service
> Rolled back controller to a3f2c9b8
```

## Building the Agent

The `satdeploy-agent` runs on ARM targets. Cross-compile with:

```bash
source /opt/poky/environment-setup-armv8a-poky-linux
cd satdeploy-agent
meson setup build-arm --cross-file yocto_cross.ini
ninja -C build-arm
```

Deploy `build-arm/satdeploy-agent` to the target.

## Building satdeploy-apm

Ground station csh module:

```bash
cd satdeploy-apm
meson setup build
ninja -C build
cp build/libcsh_satdeploy_apm.so ~/.local/lib/csh/
```

Then in csh: `satdeploy help`

## Requirements

- Python 3.8+
- SSH access to target (for SSH transport)
- `satdeploy-agent` on target (for CSP transport)
- systemd on target

## License

MIT
