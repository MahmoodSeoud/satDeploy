```
███████╗ █████╗ ████████╗██████╗ ███████╗██████╗ ██╗      ██████╗ ██╗   ██╗
██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔════╝██╔══██╗██║     ██╔═══██╗╚██╗ ██╔╝
███████╗███████║   ██║   ██║  ██║█████╗  ██████╔╝██║     ██║   ██║ ╚████╔╝
╚════██║██╔══██║   ██║   ██║  ██║██╔══╝  ██╔═══╝ ██║     ██║   ██║  ╚██╔╝
██████╔╝██║  ██║   ██║   ██████╔╝███████╗██║     ███████╗╚██████╔╝   ██║
╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═════╝ ╚══════╝╚═╝     ╚══════╝ ╚═════╝    ╚═╝
```

OTA deployment for embedded Linux satellites. Push files over SSH or CSP, track versions, rollback with one command.

Deploying software to satellite hardware during development means USB drives, ad-hoc scripts, or hoping SSH works. No versioning, no rollback, no way to know what's running. satdeploy fixes this — works over SSH for networked targets and CSP (CubeSat Space Protocol) over CAN bus for air-gapped ones.

## Try it now

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free for personal and education use).

```bash
pipx install satdeploy         # or: pip install satdeploy
satdeploy demo start            # starts a simulated satellite via Docker
```

This starts a simulated satellite, configures a test app, and prints a quick-start guide. Then:

```bash
satdeploy status                # See what's deployed
satdeploy push test_app         # Deploy a new version
satdeploy list test_app         # See version history
satdeploy rollback test_app     # Roll back to previous
satdeploy logs test_app         # View service logs
satdeploy demo shell            # Shell into the satellite
satdeploy demo eject            # Generate config for your real target
satdeploy demo stop             # Clean up
```

Docker is only used for the demo simulator. Real deployments use SSH or CSP directly.

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

## Deploy to Real Hardware

After trying the demo, transition to your actual target:

### SSH (networked targets)

If your target has network access:

```bash
satdeploy demo eject              # generates ~/.satdeploy/config.yaml (select "ssh")
satdeploy status                  # verify connection
satdeploy push my-app             # deploy
```

Edit `~/.satdeploy/config.yaml` to match your target:

```yaml
host: 192.168.1.50               # your target's IP
user: root                       # SSH user
apps:
  my-app:
    local: ./build/my-app        # path to local binary
    remote: /opt/bin/my-app      # where it goes on target
    service: my-app.service      # systemd service to restart
```

### CSP (air-gapped targets, CAN bus)

For targets connected via CAN bus or serial (no network):

1. Build `satdeploy-agent` for your target — see [Building satdeploy-agent](#building-satdeploy-agent) for full instructions (Yocto recipe or manual cross-compile with all dependencies listed).

2. Get the agent binary onto your target (skip this if you used the Yocto recipe — it's already in the image).

3. Start the agent on the target with the right interface flag:

    ```bash
    satdeploy-agent -i CAN -p can0           # CAN bus
    satdeploy-agent -i KISS -p /dev/ttyS1    # Serial link
    satdeploy-agent -i ZMQ -p localhost       # ZMQ (demo/local only)
    ```

4. Configure the ground station:

    ```bash
    satdeploy demo eject              # generates config (select "csp")
    satdeploy status                  # verify connection
    ```

    Edit `~/.satdeploy/config.yaml` to match your setup:

    ```yaml
    zmq_endpoint: tcp://localhost:9600  # CSH's ZMQ address
    agent_node: 5425                    # your satellite's CSP node ID
    ground_node: 40                     # your ground station's CSP node ID
    ```

    The `zmq_endpoint` in your config points at [CSH](https://github.com/spaceinventor/csh), which bridges between ZMQ and CAN/serial:

    ```
    Demo (ZMQ only):
      Python CLI  -->  zmqproxy  -->  Agent (-i ZMQ)

    Real satellite (CAN bus):
      Python CLI  -->  CSH  -->  CAN bus  -->  Agent (-i CAN)

    Serial link (KISS):
      Python CLI  -->  CSH  -->  serial   -->  Agent (-i KISS)
    ```

    `zmqproxy` is a simple ZMQ forwarder (demo only). For real hardware with CAN or serial, you need CSH — a full [CSP](https://github.com/spaceinventor/libcsp) router that bridges between its ZMQ interface (where the Python CLI connects) and CAN or KISS interfaces (where the satellite lives).

## Ground Station (CSH)

If you use [CSH](https://github.com/spaceinventor/csh) as your ground station, satdeploy provides native slash commands via the APM module:

```
satdeploy status -n 5425       # Query agent status
satdeploy push test_app        # Deploy an app
satdeploy rollback test_app    # Rollback
satdeploy list test_app        # List versions
satdeploy logs test_app        # View logs
```

Build and install:

```bash
cd satdeploy-apm
meson setup build
ninja -C build
cp build/libcsh_satdeploy_apm.so ~/.local/lib/csh/
```

Then in CSH: `apm load` to activate the satdeploy commands.

CSH also acts as the CSP router for CAN and serial links — the Python CLI connects to CSH via ZMQ, and CSH routes to the satellite over CAN or KISS.

## Features

- **Versioned backups** - Every deploy saves the previous file with its content hash
- **Git provenance** - Every deploy records the git commit that built the file
- **Dependency ordering** - Services stop/start in the right order
- **One-command rollback** - Instantly restore any previous version
- **Multi-transport** - Works over SSH or CSP (satellite links)
- **Per-target configs** - Separate config dirs per target, switch with `--config`

## Commands

| Command | Description |
|---------|-------------|
| `satdeploy push <app>` | Deploy file |
| `satdeploy push <app> --local ./path` | Deploy with path override |
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
| `satdeploy demo shell` | Shell into the satellite (streams agent logs) |
| `satdeploy demo eject` | Generate config for real hardware |

All commands accept `--config` to select which target config to use (e.g. `--config ~/.satdeploy/som2/config.yaml`).

## Configuration

Each target gets its own config directory (e.g. `~/.satdeploy/som1/config.yaml`):

```yaml
name: som1
transport: csp
zmq_endpoint: tcp://localhost:9600
agent_node: 5425
ground_node: 40
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
| `local` | Path to local file |
| `remote` | Deployment path on target |
| `service` | systemd service (null for libraries) |
| `depends_on` | Services this app depends on |
| `restart` | Services to restart when this library changes |
| `param` | libparam name for CSP start/stop |

### Transports

**SSH** — Direct SSH/SFTP connection. Works with any Linux target.

```yaml
name: flatsat
transport: ssh
host: 192.168.1.50
user: root
```

**CSP** — [CubeSat Space Protocol](https://github.com/spaceinventor/libcsp) over ZMQ, CAN, or KISS serial. Requires `satdeploy-agent` on target.

```yaml
name: satellite
transport: csp
zmq_endpoint: tcp://localhost:9600
agent_node: 5425
ground_node: 40
```

### Dependency Resolution

When deploying an app with dependencies:

1. **Stop** services top-down (dependents first)
2. **Deploy** the file
3. **Start** services bottom-up (dependencies first)

For libraries with a `restart` list, those services are restarted directly.

## Install from Source

For contributors or development:

```bash
git clone --recursive https://github.com/MahmoodSeoud/satBuild.git
cd satBuild
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

If you already cloned without `--recursive`, pull the submodules with:

```bash
git submodule update --init --recursive
```

## Components

| Component | Language | Runs on | Purpose |
|-----------|----------|---------|---------|
| `satdeploy` | Python | Ground station | CLI — architecture-independent |
| `satdeploy-agent` | C | Target | Handles CSP deploy commands via [libcsp](https://github.com/spaceinventor/libcsp) — must be cross-compiled for target architecture |
| `satdeploy-apm` | C | Ground station | Slash commands for [CSH](https://github.com/spaceinventor/csh) — compiled natively |

### Building satdeploy-agent

The agent runs on the target and is required for CSP transport. Two options:

**Option A: Yocto recipe (recommended)** — add `meta-satdeploy` to your Yocto build:

```
bitbake-layers add-layer /path/to/meta-satdeploy
# In local.conf:
IMAGE_INSTALL:append = " satdeploy-agent"
```

See [`meta-satdeploy/`](meta-satdeploy/) for details.

**Option B: Manual cross-compile**

System dependencies (Ubuntu/Debian — your Yocto SDK sysroot may already have these):

```bash
sudo apt install build-essential pkg-config meson ninja-build \
  libzmq3-dev libsocketcan-dev libyaml-dev libbsd-dev \
  libprotobuf-c-dev libssl-dev
```

Build (assumes you cloned with `--recursive` — see [Install from Source](#install-from-source)):

```bash
source /opt/poky/environment-setup-armv8a-poky-linux
cd satdeploy-agent
meson setup build-arm --cross-file yocto_cross.ini
ninja -C build-arm
# Output: build-arm/satdeploy-agent
```

For other toolchains, point meson at your own cross-compilation file and build normally.

### Building satdeploy-apm

[CSH](https://github.com/spaceinventor/csh) ground station module. Compiled natively on the ground station (not cross-compiled):

```bash
# System dependencies (Ubuntu/Debian):
sudo apt install libzmq3-dev libsocketcan-dev libyaml-dev libbsd-dev \
  libprotobuf-c-dev libssl-dev libsqlite3-dev

cd satdeploy-apm
meson setup build
ninja -C build
cp build/libcsh_satdeploy_apm.so ~/.local/lib/csh/
```

## Requirements

- Python 3.8+
- Docker (for demo mode only)
- SSH access to target (for SSH transport)
- `satdeploy-agent` on target (for CSP transport)
- [CSH](https://github.com/spaceinventor/csh) on ground station (for CAN/KISS transport — bridges ZMQ to physical bus)
- systemd on target

## License

MIT
