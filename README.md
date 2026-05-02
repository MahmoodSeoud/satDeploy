<p align="center">
  <img src="docs/assets/hero.webp" alt="A DISCO-2 cubesat in orbit above Earth" width="860">
</p>

<h1 align="center">satDeploy</h1>

Recently, we flew [DISCO-2](https://discosat.dk/v2_disco-2/), a 3U student CubeSat, and then spent weeks trying to recreate what was on it.

The payload ran a Yocto Linux image with several apps on it, each on its own release cadence, each updated the same way: rebuild locally, copy the binary over, and post "I updated the binary" in Slack. By launch, nobody could list every commit running on the hardware with confidence. After launch, rebuilding the same set on our flatsat took weeks of chasing memory and old tmux sessions, and we still ran into lib version mismatches we hadn't known were there.

satDeploy is what we built so it doesn't happen again. Every deploy is versioned, hash-verified, and tagged with the git commit it came from. Every file can be rolled back with one command. It runs over [CSP](https://github.com/spaceinventor/libcsp) (CAN bus, KISS serial, ZMQ) for air-gapped satellite links, and is **resumable across pass windows**. A transfer that doesn't finish in one pass picks up where it left off on the next, with no re-sending bytes the ground already shipped.

> DISCO-2 is a 3U student CubeSat from Aarhus University, SDU, and ITU Copenhagen, launched on [SpaceX Transporter-16](https://x.com/i/broadcasts/1kJzDMgwZAvKv) (March 30, 2026) to image Arctic glaciers from a 510 km sun-synchronous orbit. Coverage: [Danish Space News](https://danishspacenews.substack.com/p/disco-2-one-of-the-most-ambitious), [The Danish Dream](https://thedanishdream.com/danish-society/science/danish-students-launch-satellite-to-track-melting-arctic/), [project site](https://projects.au.dk/ausat/disco-2).

> **Early stage, but heading to orbit.** We built satDeploy *after* DISCO-2 launched, so the current orbiting payload is flying without it. Flatsat testing is ongoing, and the first install on the satellite still has to ride the legacy upload path. satDeploy can't yet bootstrap itself onto a satellite that doesn't already run `satdeploy-agent`. Once that first install lands, every subsequent deploy will be versioned, hash-verified, and rollback-able from the ground. Right now it runs on our flatsat. We're actively putting it in front of other cubesat teams; the more hardware it sees on the bench, the more rough edges we find together before anything flies.

## What's unusual about this

The unusual bit: a single deploy can span multiple pass windows, with the OBC powered down between them.

- **Power-cycle the OBC mid-deploy.** Power down between passes (power budget, thermal, scheduling); the next push resumes from a bitmap on disk that the agent reads on boot.
- **Atomic, hash-verified swap.** Partial binaries never run. The new file moves into place only after the full SHA256 matches what the ground announced. A re-staged binary (different SHA) blows away stale resume state instead of inheriting a poisoned bitmap, so content addressing on the sidecar is strict equality.
- **Cross-pass orchestration over libcsp/libdtp.** `csh upload` and spaceboot work at the transport layer; satDeploy layers application-level pass-awareness on top of libdtp's selective-repeat retries.

> 90-second F6 reboot demo (PDU power-cycles the OBC mid-transfer, second push resumes): _lands Tuesday 2026-05-05_.

## Components

| Piece | Where it runs | Language |
|-------|---------------|----------|
| **satdeploy-agent** | Target satellite | C |
| **satdeploy-apm** | Ground station, inside [CSH](https://github.com/spaceinventor/csh) | C |

The APM is dlopen'd into CSH and adds `satdeploy push/status/rollback/list/logs` slash commands. The agent listens on CSP port 20 for protobuf deploy commands and pulls files via DTP from the ground.

Both write to the same `~/.satdeploy/history.db` (SQLite, WAL mode) so `satdeploy status` shows the full deploy history regardless of where the command was issued from.

## Quick start (local loopback in Docker)

The fastest way to see satDeploy work is the dev container. It bundles CSH, builds the APM, generates four test binaries (50 B → 50 MB), and wires ground + target through a local ZMQ proxy. No satellite, no CAN hardware, no Yocto SDK needed.

Requirements: Docker.

```bash
git clone --recurse-submodules https://github.com/MahmoodSeoud/satDeploy.git
cd satDeploy
./scripts/docker-dev.sh
```

The entrypoint pre-builds the agent + APM, generates four test binaries (50 B → 50 MB), and drops you into a two-pane tmux: **left** = `csh` with the APM auto-loaded, **right** = `satdeploy-agent` running on ZMQ. In the left (csh) pane:

```
satdeploy push hello
satdeploy status
satdeploy rollback hello
```

The file lands at `/tmp/satdeploy-target/hello`, gets hash-verified, and rolls back from a backup. To exercise cross-pass resume, push the 50 MB `payload` app and Ctrl-C the agent in the right pane mid-transfer. The next push picks up from the bitmap sidecar.

## Build from source (host)

System dependencies (Ubuntu/Debian):

```bash
sudo apt install build-essential pkg-config meson ninja-build \
  libzmq3-dev libsocketcan-dev libyaml-dev libbsd-dev \
  libprotobuf-c-dev libssl-dev
git clone --recurse-submodules https://github.com/MahmoodSeoud/satDeploy.git
cd satDeploy
```

Build and install the APM (assumes [CSH](https://github.com/spaceinventor/csh) is already installed; the APM is dlopen'd by `csh`'s `apm load`):

```bash
cd satdeploy-apm
meson setup build
ninja -C build
mkdir -p ~/.local/lib/csh && cp build/libcsh_satdeploy_apm.so ~/.local/lib/csh/
```

Cross-compile the agent for the target. Yocto recipe lives in [`meta-satdeploy/`](meta-satdeploy/); manual cross-compile with the Poky SDK:

```bash
source /opt/poky/environment-setup-armv8a-poky-linux
cd satdeploy-agent
meson setup build-arm --cross-file yocto_cross.ini
ninja -C build-arm
# scp build-arm/satdeploy-agent root@target:/usr/bin/
```

Full build notes (Yocto layer, CSP version pinning, sysroot caveats) live in [docs/building.md](docs/building.md).

## Cross-pass resumable transfers

CubeSat operators upload software to satellites over UHF radio links that are flaky, slow (hundreds of bps to ~10 kbps), and only available during 5-10 minute pass windows. Existing tools (`csh upload`, spaceboot) operate at the transport layer. When a pass ends mid-transfer, or when the operator power-cycles the OBC, the next attempt starts over.

satDeploy persists the receive bitmap to a sidecar at `/var/lib/satdeploy/state/<app>.dtpstate` whenever a pass ends partial. The next deploy for the same `(app, hash)` pre-patches the DTP request to ask only for the still-missing intervals. A re-staged binary (different SHA256) invalidates the sidecar via strict-equality content addressing, so a partial transfer can never silently inherit a stale bitmap.

The state lives on persistent flash, so it survives a full agent reboot or OBC power cycle. Operators can power down between passes (for power budget, thermal, or mission scheduling) without losing the megabytes already shipped. See [`satdeploy-agent/include/session_state.h`](satdeploy-agent/include/session_state.h) for the on-disk format and design rationale.

## Measured

| Test | n | Result | Source |
|---|---|---|---|
| 0% loss tail-race (smart build, 256 KB / 1 MB / 4 MB) | 30 | 30/30 transfers complete; 28/30 use exactly 1 retry round to clean up libdtp's tail-end termination race at `dtp_client.c:284` | `experiments/results/tail_race.csv` |
| 0% loss naive baseline (`-Dnaive_baseline=true`, retry rounds capped at 0) | 30 | 1/30 complete; 29/30 fail with 1-2 packet gap at the tail | same |
| 1% configured loss (1 MB) | 5 | 5/5 complete, mean 1.8 retry rounds | `experiments/results/loss_rates.csv` |
| 5% configured loss | 5 | 5/5 complete, mean 6.8 retry rounds (close to the 8-round budget edge) | same |
| 10% configured loss | 5 | 0/5 complete in single pass; all 5 hit the 8-round cap and persist state for cross-pass resume | same |

Reproducible via `experiments/sweep_tail_race.sh` and `experiments/sweep_loss_rates.sh`. The 8-round budget covers up to ~5% loss in a single pass; beyond that the cross-pass resume mechanism is what closes the gap. F6 reboot demo numbers (real radio, real loss, real PDU power-cycle) land Tuesday 2026-05-05.

## Docs

- **[Command reference](docs/commands.md)**: every command and flag
- **[Configuration reference](docs/configuration.md)**: full config schema, transports, dependency ordering
- **[Building from source](docs/building.md)**: agent cross-compile, APM build, CSP version pinning
- **[Changelog](CHANGELOG.md)**: what shipped in each version, including wire-format compatibility notes
- **[Contributing](CONTRIBUTING.md)**: submodule setup, the CSP version pinning gotcha, manual loopback test recipe

## Requirements

- `satdeploy-agent` on target
- `satdeploy-apm` + [CSH](https://github.com/spaceinventor/csh) on ground station
- A CSP transport between them (CAN bus, KISS serial, or ZMQ for local testing)
- systemd on target (for service management)

## License

MIT
