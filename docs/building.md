# Building from Source

## Components

| Component | Language | Runs on | Purpose |
|-----------|----------|---------|---------|
| `satdeploy` | Python | Ground station | CLI, architecture-independent |
| `satdeploy-agent` | C | Target | Handles CSP deploy commands via [libcsp](https://github.com/spaceinventor/libcsp). Must be cross-compiled for the target architecture. |
| `satdeploy-apm` | C | Ground station | Slash commands for [CSH](https://github.com/spaceinventor/csh). Compiled natively. |

## Python CLI (development)

```bash
git clone --recursive https://github.com/MahmoodSeoud/satBuild.git
cd satBuild
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

If you already cloned without `--recursive`:

```bash
git submodule update --init --recursive
```

## satdeploy-agent (target, cross-compiled)

The agent runs on the target and is required for CSP transport.

### Option A: Yocto recipe (recommended)

Add `meta-satdeploy` to your Yocto build:

```
bitbake-layers add-layer /path/to/meta-satdeploy
# In local.conf:
IMAGE_INSTALL:append = " satdeploy-agent"
```

See [`meta-satdeploy/`](../meta-satdeploy/) for details.

### Option B: Manual cross-compile

System dependencies (Ubuntu/Debian; your Yocto SDK sysroot may already have these):

```bash
sudo apt install build-essential pkg-config meson ninja-build \
  libzmq3-dev libsocketcan-dev libyaml-dev libbsd-dev \
  libprotobuf-c-dev libssl-dev
```

Build (assumes you cloned with `--recursive`):

```bash
source /opt/poky/environment-setup-armv8a-poky-linux
cd satdeploy-agent
meson setup build-arm --cross-file yocto_cross.ini
ninja -C build-arm
# Output: build-arm/satdeploy-agent
```

For other toolchains, point meson at your own cross-compilation file and build normally.

## satdeploy-apm (ground station, native)

[CSH](https://github.com/spaceinventor/csh) ground station module:

```bash
# System dependencies (Ubuntu/Debian):
sudo apt install build-essential pkg-config meson ninja-build \
  libzmq3-dev libsocketcan-dev libbsd-dev

cd satdeploy-apm
meson setup build
ninja -C build
cp build/libcsh_satdeploy_apm.so ~/.local/lib/csh/
```

Then in CSH: `apm load` to activate the satdeploy commands.

The APM adds `-n/--node NUM` to each command for targeting a specific CSP node (defaults to `agent_node` from config).

> **Note:** libyaml, protobuf-c, and sqlite3 are bundled automatically via meson wraps, so no system packages are needed. OpenSSL is not required (SHA256 is built-in).

> **CSP version pinning.** The APM is dlopen'd into CSH's process and shares `csp_packet_t` structs with it. The `lib/csp` submodule **must** match CSH's CSP version, or field offsets diverge and packets silently corrupt. Sync with:
> ```bash
> cd satdeploy-apm/lib/csp && git checkout $(cd /path/to/csh/lib/csp && git rev-parse HEAD)
> ```
