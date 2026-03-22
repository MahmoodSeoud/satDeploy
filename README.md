# satdeploy

Deploy binaries to embedded Linux targets with versioned backups, dependency-aware service restarts, and one-command rollback.

## Try it in 60 seconds

No hardware needed. satdeploy ships with a simulated satellite target via Docker:

```bash
git clone https://github.com/MahmoodSeoud/satBuild.git
cd satBuild
python -m venv .venv && source .venv/bin/activate
pip install -e .

satdeploy demo start
```

This pulls a container with a simulated satellite, configures everything, and prints a guided tutorial. Then:

```bash
satdeploy status --config ~/.satdeploy/.demo-config.yaml        # See what's deployed
satdeploy push test_app --config ~/.satdeploy/.demo-config.yaml  # Deploy a binary
satdeploy list test_app --config ~/.satdeploy/.demo-config.yaml  # See version history
satdeploy rollback test_app --config ~/.satdeploy/.demo-config.yaml  # Roll back
satdeploy demo watch                                    # Stream agent logs
satdeploy demo stop                                     # Clean up
```

Docker is only used for the demo simulator. Real deployments use SSH or CSP directly.

## Why

Deploying binaries to embedded targets during development is tedious. You're either using a janky uploader, a USB stick, or SSH + prayer. No versioning, no rollback, no dependency awareness.

satdeploy fixes this with:
- **Versioned backups** - Every deploy saves the previous binary with its content hash
- **Git provenance** - Every deploy records the git commit that built the binary
- **Dependency ordering** - Services stop/start in the right order
- **One-command rollback** - Instantly restore any previous version
- **Multi-transport** - Works over SSH or CSP (satellite links)
- **Per-target configs** - Separate config dirs per target, switch with `--config`

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

## Commands

| Command | Description |
|---------|-------------|
| `satdeploy init` | Interactive setup |
| `satdeploy push <app>` | Deploy binary |
| `satdeploy push --all` | Deploy all apps |
| `satdeploy push --require-clean` | Refuse to deploy from dirty git tree |
| `satdeploy status` | Show app statuses with git provenance |
| `satdeploy list <app>` | List all versions |
| `satdeploy rollback <app>` | Restore previous version |
| `satdeploy rollback <app> <hash>` | Restore specific version |
| `satdeploy logs <app>` | Show service logs |
| `satdeploy config` | Show configuration |
| `satdeploy demo start` | Start simulated satellite (Docker) |
| `satdeploy demo stop` | Stop simulator |
| `satdeploy demo watch` | Stream agent logs |
| `satdeploy demo eject` | Generate config for real hardware |

All commands accept `--config` to select which target config to use (e.g. `--config ~/.satdeploy/som2/config.yaml`).

## Example Session

```
$ satdeploy push controller
[1/4] Stopping controller.service
[2/4] Backing up /opt/disco/bin/controller
[3/4] Uploading ./build/controller
[4/4] Starting controller.service
> Deployed controller (e5f6a7b9) main@3c940acf

$ satdeploy status
Target: som1 (192.168.1.50)

    APP              STATUS        HASH       SOURCE           TIMESTAMP
    --------------------------------------------------------------------------
  > controller      running       e5f6a7b9  main@3c940acf    2024-01-15 14:35
  > csp_server      running       b7e1d2a4  main@ddfa081f    2024-01-15 09:15
  - libparam        deployed      c4d5e6f1  main@9c622a2b    2024-01-12 16:23

$ satdeploy list controller
Versions for controller:

    HASH       SOURCE           TIMESTAMP            STATUS
    ---------------------------------------------------------------
  > e5f6a7b9  main@3c940acf    2024-01-15 14:35:10  deployed
  - a3f2c9b8  main@ddfa081f    2024-01-15 14:30:22  backup
  - d2c3b4a5  feat@17ad579b    2024-01-14 09:15:00  backup

$ satdeploy rollback controller
[1/3] Stopping controller.service
[2/3] Restoring a3f2c9b8
[3/3] Starting controller.service
> Rolled back controller to a3f2c9b8
```

## Configuration

Each target gets its own config directory (e.g. `~/.satdeploy/som1/config.yaml`):

```yaml
name: som1
transport: csp
zmq_endpoint: tcp://localhost:4040
agent_node: 5424
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
name: flatsat
transport: ssh
host: 192.168.1.50
user: root
```

### CSP (Cubesat Space Protocol)

For satellite communication links. Requires `satdeploy-agent` running on target.

```yaml
name: satellite
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
Stop:  controller -> csp_server
Start: csp_server -> controller
```

For libraries with a `restart` list, those services are restarted directly.

## From Demo to Real Hardware

After trying the demo, transition to your actual target:

```bash
# Generate a config template for your hardware
satdeploy demo eject

# Or set up manually
satdeploy init --config ~/.satdeploy/my-target
```

For SSH targets, you just need network access and an SSH key. For CSP targets, you need:
1. `satdeploy-agent` running on the target (see below)
2. A CSP link (zmqproxy, CAN, or KISS serial)

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
- Docker (for demo mode only)
- SSH access to target (for SSH transport)
- `satdeploy-agent` on target (for CSP transport)
- systemd on target

## License

MIT
