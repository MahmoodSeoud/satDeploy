**Stop SCP-ing binaries onto your flatsat.** satdeploy is one CLI for the entire CubeSat lifecycle — push over Ethernet/SSH on the bench, then over CSP/CAN/serial after launch, with versioned backups, hash verification, and one-command rollback. The same tool that ships your Tuesday afternoon code change pushes the in-orbit patch two years later.

<details>
<summary><code>satdeploy</code></summary>

```
███████╗ █████╗ ████████╗██████╗ ███████╗██████╗ ██╗      ██████╗ ██╗   ██╗
██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔════╝██╔══██╗██║     ██╔═══██╗╚██╗ ██╔╝
███████╗███████║   ██║   ██║  ██║█████╗  ██████╔╝██║     ██║   ██║ ╚████╔╝
╚════██║██╔══██║   ██║   ██║  ██║██╔══╝  ██╔═══╝ ██║     ██║   ██║  ╚██╔╝
██████╔╝██║  ██║   ██║   ██████╔╝███████╗██║     ███████╗╚██████╔╝   ██║
╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═════╝ ╚══════╝╚═╝     ╚══════╝ ╚═════╝    ╚═╝
```

</details>

We shipped a CubeSat without being 100% sure what software was on it. After launch, we spent weeks trying to recreate the state on our flatsat. USB drives, ad-hoc SCP scripts, Slack messages saying "I updated the binary" ... after dozens of incremental updates, nobody could say exactly what was running on the hardware.

So we built satdeploy. Every deploy is versioned, hash-verified, and recorded. Every file can be rolled back with one command. It works over SSH for networked targets and over CSP for air-gapped satellite links (CAN bus, serial).

> **Early stage.** satdeploy works on our hardware. We're looking for other satellite teams to try it. Does this fit your deployment workflow? What's missing? What's confusing? [Open an issue](https://github.com/MahmoodSeoud/satBuild/issues) or reach out.

## What it does

- **Deploy to your flatsat over Ethernet/SSH** — every push is versioned, hash-verified, and tagged with the git commit that built the binary. `satdeploy push` replaces whatever SCP script or Ansible playbook your team is hating this week.
- **Same tool in orbit over CSP/CAN/serial** — when your hardware launches, switch the transport from `ssh` to `csp` in the config. Same CLI, same history database, same rollback semantics. No second tool to learn, no second workflow to build.
- **See what's running** with `satdeploy status`. Hash-verified against the target, so you know if something changed outside satdeploy — including the git commit the deployed binary came from.
- **Roll back in one command** with `satdeploy rollback`. Every deploy is backed up with its content hash; the git provenance follows the rollback so you always know exactly what commit is live.
- **Complements Yocto.** Yocto builds your base image. satdeploy tracks the incremental updates that happen during development and in orbit.

## Try it now

Zero dependencies beyond Python and git. No Docker, no satellite, no hardware.

```bash
pipx install satdeploy   # or: pip install satdeploy
satdeploy demo           # 10 seconds end-to-end — no prerequisites
```

`satdeploy demo` sets up a throwaway git repo + local target directory and pre-installs test_app v1.0.0. Then try the real product loop:

```bash
satdeploy status              # See what's deployed (v1, git-tagged)
satdeploy push test_app       # Deploy v2 (new hash, new commit)
satdeploy rollback test_app   # Undo — one command, git tag carries through
satdeploy demo stop           # Tear it down when you're done
```

The demo uses the same `LocalTransport`, `History` database, backup semantics, and rollback logic as a real deployment — every hash and git commit shown is real. When you're ready for real hardware, run `satdeploy init` and point at your flatsat over SSH or your satellite over CSP.

## Example Session

Real output from `satdeploy demo` — exactly what you'll see on your own machine after `pipx install satdeploy`:

```
$ satdeploy status
Target: ~/.satdeploy/demo/target

    APP              STATUS        HASH                      PATH
    ------------------------------------------------------------
  ▸ test_app        running       32c0702b (main@0c7e8fb2)  /opt/demo/bin/test_app

$ satdeploy push test_app
▸ Deployed test_app (5f3413a2) (main@1f1750a6)

$ satdeploy status
Target: ~/.satdeploy/demo/target

    APP              STATUS        HASH                      PATH
    ------------------------------------------------------------
  ▸ test_app        running       5f3413a2 (main@1f1750a6)  /opt/demo/bin/test_app

$ satdeploy list test_app
Versions for test_app:

    HASH                      TIMESTAMP            STATUS
    ---------------------------------------------
  • 32c0702b (main@0c7e8fb2)  2026-04-14T12:49:16  backup
  → 5f3413a2 (main@1f1750a6)  2026-04-14T12:49:16  deployed

$ satdeploy rollback test_app
Rolling back test_app...
▸ Rolled back test_app to 32c0702b

$ satdeploy status
Target: ~/.satdeploy/demo/target

    APP              STATUS        HASH                      PATH
    ------------------------------------------------------------
  ▸ test_app        running       32c0702b (main@0c7e8fb2)  /opt/demo/bin/test_app
```

Notice the last line: after rollback, `status` shows both the file hash *and* the git commit it came from. satdeploy tracks which commit is live on your hardware across every push and rollback — you always know exactly what's running.

## Deploy to Real Hardware

### SSH (networked targets)

Your target has network access. You don't need any C components — just the Python CLI.

**1. Create a config:**

```bash
satdeploy init                   # select "ssh", enter your target's IP
```

**2. Edit `~/.satdeploy/config.yaml` for your target:**

```yaml
name: flatsat
transport: ssh
host: 192.168.1.50
user: root
apps:
  controller:
    local: ./build/controller          # path to your local binary
    remote: /opt/bin/controller        # where it goes on target
    service: controller.service        # systemd service to restart (or null)
```

**3. Deploy:**

```bash
satdeploy push controller
satdeploy status
```

**4. See what happened:**

```bash
satdeploy list controller            # version history
satdeploy rollback controller        # undo the deploy
satdeploy logs controller            # service logs
```

### CSP (air-gapped targets, CAN bus)

Your target is connected via CAN bus or serial — no network. You need three pieces:

| Piece | Where it runs | How to get it |
|-------|---------------|---------------|
| Python CLI or CSH APM | Ground station | `pip install satdeploy` or [build the APM](#building-satdeploy-apm) |
| satdeploy-agent | Target satellite | [Yocto recipe or cross-compile](#building-satdeploy-agent) |
| [CSH](https://github.com/spaceinventor/csh) | Ground station | Bridges ZMQ ↔ CAN/serial |

**1. Start the agent on the target:**

```bash
satdeploy-agent -i CAN -p can0           # CAN bus
satdeploy-agent -i KISS -p /dev/ttyS1    # Serial link
satdeploy-agent -i ZMQ -p localhost       # ZMQ (local testing only)
```

**2. Create a config on the ground station:**

```bash
satdeploy init                   # select "csp", enter your node IDs
```

**3. Edit `~/.satdeploy/config.yaml`:**

```yaml
name: my-satellite
transport: csp
zmq_endpoint: tcp://localhost:9600       # CSH's ZMQ address
agent_node: 55                           # your satellite's CSP node ID
ground_node: 40                          # your ground station's CSP node ID
apps:
  controller:
    local: ./build/controller
    remote: /opt/bin/controller
```

**4. Deploy:**

```bash
satdeploy push controller
satdeploy status
```

**How the pieces connect:**

```
Local testing (ZMQ):
  Python CLI  -->  zmqproxy  -->  Agent (-i ZMQ)

Real satellite (CAN bus):
  Python CLI  -->  CSH  -->  CAN bus  -->  Agent (-i CAN)

Serial link (KISS):
  Python CLI  -->  CSH  -->  serial   -->  Agent (-i KISS)
```

`zmqproxy` is a simple ZMQ forwarder (demo/local only). For real hardware, you need [CSH](https://github.com/spaceinventor/csh) — it bridges between its ZMQ interface (where the CLI connects) and CAN or KISS interfaces (where the satellite lives).

## Ground Station (CSH)

If you use [CSH](https://github.com/spaceinventor/csh) as your ground station, satdeploy provides native slash commands via the APM module. The commands are **identical** to the Python CLI.

Build and install:

```bash
cd satdeploy-apm
meson setup build
ninja -C build
cp build/libcsh_satdeploy_apm.so ~/.local/lib/csh/
```

Then in CSH: `apm load` to activate the satdeploy commands.

The APM also adds `-n/--node NUM` to each command for targeting a specific CSP node (defaults to `agent_node` from config).

CSH also acts as the CSP router for CAN and serial links — the Python CLI connects to CSH via ZMQ, and CSH routes to the satellite over CAN or KISS.

## Features

- **Versioned backups** - Every deploy saves the previous file with its content hash
- **Git provenance** - Every deploy records the git commit that built the file
- **Dependency ordering** - Services stop/start in the right order
- **One-command rollback** - Instantly restore any previous version
- **Multi-transport** - Works over SSH or CSP (satellite links)
- **Per-target configs** - Separate config dirs per target, switch with `--config`

## Commands

The Python CLI and CSH APM share the same command interface. Every flag works in both.

### push — Deploy files to target

```
satdeploy push <app>                         # Deploy app from config
satdeploy push <app1> <app2>                 # Deploy multiple apps
satdeploy push -a / --all                    # Deploy all apps from config
satdeploy push -f PATH -r PATH              # Ad-hoc deploy (no config entry needed)
```

| Flag | Description |
|------|-------------|
| `-f, --local PATH` | Local file path (overrides config) |
| `-r, --remote PATH` | Remote path on target |
| `-F, --force` | Force deploy even if same version |
| `-a, --all` | Deploy all apps from config |
| `--require-clean` | Refuse to deploy from a dirty git tree |

### status — Show deployed apps

```
satdeploy status
```

### list — Show version history

```
satdeploy list <app>
```

### rollback — Restore a previous version

```
satdeploy rollback <app>                     # Roll back to previous version
satdeploy rollback <app> -H HASH             # Roll back to specific version
```

| Flag | Description |
|------|-------------|
| `-H, --hash HASH` | Specific backup hash to restore |

### logs — View service logs

```
satdeploy logs <app>
satdeploy logs <app> -l 50                   # Show last 50 lines
```

| Flag | Description |
|------|-------------|
| `-l, --lines NUM` | Number of lines to show (default: 100) |

### config — Show current configuration

```
satdeploy config
```

### demo — Zero-prerequisite workflow demo (Python CLI only)

```
satdeploy demo           # Set up throwaway git repo + local target dir
satdeploy demo stop      # Tear down
satdeploy demo status    # Check if the demo is set up
```

### Shell completion

```bash
# Bash — add to ~/.bashrc
eval "$(_SATDEPLOY_COMPLETE=bash_source satdeploy)"

# Zsh — add to ~/.zshrc
eval "$(_SATDEPLOY_COMPLETE=zsh_source satdeploy)"
```

All commands also accept:

| Flag | Description |
|------|-------------|
| `-n, --node NUM` | Target CSP node (overrides `agent_node` from config) |
| `--config PATH` | Config file (default: `~/.satdeploy/config.yaml`) |

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
sudo apt install build-essential pkg-config meson ninja-build \
  libzmq3-dev libsocketcan-dev libbsd-dev

cd satdeploy-apm
meson setup build
ninja -C build
cp build/libcsh_satdeploy_apm.so ~/.local/lib/csh/
```

> **Note:** libyaml, protobuf-c, and sqlite3 are bundled automatically via meson wraps — no system packages needed. OpenSSL is not required (SHA256 is built-in).

## Requirements

- Python 3.8+
- git (for the zero-prerequisite demo, and for provenance tracking on real deploys)
- SSH access to target (for SSH transport)
- `satdeploy-agent` on target (for CSP transport)
- [CSH](https://github.com/spaceinventor/csh) on ground station (for CAN/KISS transport — bridges ZMQ to physical bus)
- systemd on target

## License

MIT
