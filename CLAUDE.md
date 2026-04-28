# CLAUDE.md

Guidance for Claude Code when working with this repository.

## Project Overview

**satdeploy** is a deployment system for embedded Linux targets (satellites)
with versioned backups, dependency-aware service management, one-command
rollback, and **cross-pass resumable transfers** over CSP/DTP — a partial
upload survives operator Ctrl-C, agent reboot, and pass-window boundaries.

### Components

| Component | Language | Purpose |
|-----------|----------|---------|
| **satdeploy-agent** | C | Runs on ARM target — handles CSP deploy commands |
| **satdeploy-apm** | C | csh slash commands for the ground station |

CSP-only since `phase0-week1`. The Python CLI was deleted to focus the project
on the application-level OTA story for CSP missions; it can be reintroduced
later if SSH transport is needed.

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

The `build/` directory is for x86 native testing only — never deploy it.

### satdeploy-apm (Ground station)

**CRITICAL:** The CSP submodule (`lib/csp`) must match CSH's CSP version. The APM
is dlopen'd into CSH's process and shares `csp_packet_t` structs — a version
mismatch causes wrong field offsets and silent data corruption. Sync with:
```bash
cd satdeploy-apm/lib/csp && git checkout $(cd /path/to/csh/lib/csp && git rev-parse HEAD)
```

```bash
cd satdeploy-apm
meson setup build --wipe
ninja -C build
# Install: cp build/libcsh_satdeploy_apm.so /root/.local/lib/csh/
```

## Architecture

### satdeploy-agent (C)

Runs on target, listens on CSP port 20 for protobuf commands:

| Command | Action |
|---------|--------|
| `STATUS` | Return app statuses with hashes |
| `DEPLOY` | Stop app, backup, download via DTP (cross-pass resumable), install, start |
| `ROLLBACK` | Restore from backup directory |
| `LIST_VERSIONS` | List available backups |
| `VERIFY` | Return SHA256 of installed file |

**Interfaces:** ZMQ (default), CAN, KISS serial

**Dependencies:** libcsp, libparam, DTP, protobuf-c, OpenSSL

### satdeploy-apm Slash Commands

Ground station csh module providing:
- `satdeploy status` — Show status of deployed apps and services
- `satdeploy push <app>` — Deploy one or more apps to a target
- `satdeploy rollback <app>` — Rollback to a previous version
- `satdeploy list <app>` — List all versions of an app (deployed + backups)
- `satdeploy logs <app>` — Show logs for an app's service

### Cross-pass DTP resume

Within-pass reliability is the libdtp selective-repeat retry loop in
`satdeploy-agent/src/dtp_client.c` (commit 5fbe1b1) — bitmap of received
seqs, scan for gaps, re-issue the request with `request_meta.intervals[]`,
up to 8 retry rounds.

Cross-pass persistence wraps that loop. The receive bitmap is written to
`/var/lib/satdeploy/state/<app>.dtpstate` atomically when a pass exhausts
its retry budget without full coverage; the next deploy for the same
`(app, expected_hash)` pre-patches `request_meta.intervals[]` so the very
first `dtp_start_transfer` only asks for the still-missing seqs.

Strict equality on the sidecar header gates resume — a re-staged binary
(different SHA256) blows away stale state instead of inheriting a poisoned
bitmap. See `satdeploy-agent/include/session_state.h` for the on-disk
format and design rationale.

## CLI Commands

All run from inside CSH after `apm load`:

```
satdeploy push <app>                # Deploy file via DTP
satdeploy push <app> -f ./path      # Path override
satdeploy push -a                   # Deploy all apps from config
satdeploy status                    # All app statuses with git provenance
satdeploy list <app>                # Versions (deployed + backups)
satdeploy rollback <app>            # Restore previous version
satdeploy rollback <app> <hash>     # Restore specific version
satdeploy logs <app>                # Show service logs
satdeploy config                    # Show current config

# Override target node ad-hoc (defaults to agent_node from config)
satdeploy status -n 5425
```

## Config Structure

Each target gets its own config at `~/.satdeploy/<target>/config.yaml`:

```yaml
name: som1
zmq_endpoint: tcp://localhost:9600
agent_node: 5425
ground_node: 4040

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
    depends_on: [csp_server]
    param: mng_controller     # libparam name

  libparam:
    local: ./build/libparam.so
    remote: /usr/lib/libparam.so
    service: null
    restart: [csp_server, controller]
```

The `name` field identifies this target in history records (defaults to `"default"`).

## Deployment Flow

1. APM registers the local file as a DTP payload (deterministic FNV-8 of app_name → payload_id; del-then-add to refresh slot contents)
2. APM sends DEPLOY command to agent (CSP port 20)
3. Agent stops app via libparam (TODO)
4. Agent backs up current file
5. Agent downloads new file via DTP from ground (cross-pass resumable — see above)
6. Agent verifies full SHA256 against `expected_checksum`
7. Agent moves temp into place, applies file mode
8. Agent starts app via libparam (TODO)
9. APM records to `~/.satdeploy/history.db` (transport="csp")

## Dependency Resolution

- **Stop order:** Dependents first (top-down)
- **Start order:** Dependencies first (bottom-up)

For libraries with `restart` lists, those services are used directly instead of computing the dependency graph.

## Protocol Details

### CSP Ports (Agent)
- **Port 20:** Deploy command handler (protobuf)
- **Port 7:** DTP metadata requests
- **Port 8:** DTP data packets

### Hash Format
- **On the wire:** full 64-hex SHA256 (gates cross-pass resume — an 8-char prefix isn't collision-resistant for that purpose)
- **Display:** `%.8s` truncation in status/list tables for readability

### Backup Naming
Files are named: `{YYYYMMDD}-{HHMMSS}-{hash}.bak` where `hash` is the full 64-hex SHA256. Legacy 8-char backups still parse (the rollback hash extractor accepts both lengths).

### Session State Sidecar
- Path: `/var/lib/satdeploy/state/<app_name>.dtpstate`
- Mode: 0600
- Format: see `satdeploy-agent/include/session_state.h` (uint32 version + uint32 size + char[65] hash + uint32 nof_packets + uint16 effective_mtu + uint16 reserved + uint8[bitmap_bytes])

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
