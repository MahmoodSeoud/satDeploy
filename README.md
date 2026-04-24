<p align="center">
  <img src="docs/assets/hero.webp" alt="A DISCO-2 cubesat in orbit above Earth" width="860">
</p>

<h1 align="center">satDeploy</h1>

![satDeploy pushing and rolling back a local test app in 20 seconds](demo/demo.gif)

<sub><i><code>satdeploy demo</code> against a local throwaway target. Same hashing, backups, git provenance, and rollback code paths as a production deploy.</i></sub>

Recently, we flew [DISCO-2](https://discosat.dk/v2_disco-2/), a 3U student CubeSat, and then spent weeks trying to recreate what was on it.


The payload ran a Yocto Linux image with several apps on it, each on its own release cadence, each updated the same way: rebuild locally, copy the binary over USB or SCP, and post "I updated the binary" in Slack. By launch, nobody could list every commit running on the hardware with confidence. After launch, rebuilding the same set on our flatsat took weeks of chasing memory and old tmux sessions, and we still ran into lib version mismatches we hadn't known were there.

satDeploy is what we built so it doesn't happen again. Every deploy is versioned, hash-verified, and tagged with the git commit it came from. Every file can be rolled back with one command. It works over SSH for networked targets on the bench, and over [CSP](https://github.com/spaceinventor/libcsp) (CAN bus, KISS serial, ZMQ) for air-gapped satellite links.

> DISCO-2 is a 3U student CubeSat from Aarhus University, SDU, and ITU Copenhagen, launched on [SpaceX Transporter-16](https://x.com/i/broadcasts/1kJzDMgwZAvKv) (March 30, 2026) to image Arctic glaciers from a 510 km sun-synchronous orbit. Coverage: [Danish Space News](https://danishspacenews.substack.com/p/disco-2-one-of-the-most-ambitious), [The Danish Dream](https://thedanishdream.com/danish-society/science/danish-students-launch-satellite-to-track-melting-arctic/), [project site](https://projects.au.dk/ausat/disco-2).

> **Early stage, but heading to orbit.** We built satDeploy *after* DISCO-2 launched, so the current payload is flying without it. The next uplink window will push satDeploy to the DISCO-2 payload, and every deploy after that will be versioned, hash-verified, and rollback-able from the ground. Right now it runs on our flatsat, and we're actively putting it in front of other satellite teams — the more hardware it sees on the bench, the more rough edges we find and fix together before anything flies. If you run a satellite program, we'd love to see it on your flatsat. [Open an issue](https://github.com/MahmoodSeoud/satBuild/issues/new) or reach out.

## Try it now

Zero dependencies beyond Python 3.8+ and git. One command from zero to a working demo:

```bash
pipx install git+https://github.com/MahmoodSeoud/satDeploy@v0.4.0
satdeploy demo
```

Don't have `pipx`? `python3 -m pip install --user pipx && python3 -m pipx ensurepath` gets you there. It's what handles PEP 668 on recent Linux so you don't have to set up a venv by hand.

Prefer a venv for development? Clone + editable install still works:

```bash
git clone https://github.com/MahmoodSeoud/satDeploy && cd satDeploy
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
satdeploy demo
```

> **Not on PyPI yet.** We're deliberately holding the tag-only install path until the config schema stabilises and the first pilot has landed — avoids version-yank pain while `iterate`, `validate`, and CSP-iterate are still moving. `pipx install git+…@vX.Y.Z` gives you the same one-line UX.

`satdeploy demo` sets up a throwaway git repo, a local target directory, and a sample `test_app`, so you can exercise the whole loop on your laptop without any hardware.

**The daily loop** — edit-to-running in one command:

```bash
satdeploy iterate test_app       # deploy → restart → health-check
satdeploy watch test_app         # same loop, fires on every save
```

**The safety net** — every deploy versioned, hash-verified, rollback-able:

```bash
satdeploy status                 # what's running, which hash, which commit
satdeploy list test_app          # all previous versions
satdeploy rollback test_app      # undo to previous, in one command
satdeploy demo stop              # tear it all down
```

The demo uses a local directory as the "target". Swap the config for SSH (below) to hit real hardware.

## Deploy to real hardware

### SSH (networked target)

Your target has network access. You don't need any C components, just the Python CLI.

```bash
satdeploy init                   # select "ssh", enter your target's IP
```

Then edit `~/.satdeploy/config.yaml`:

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

Deploy:

```bash
satdeploy push controller
satdeploy status
satdeploy rollback controller        # undo
satdeploy logs controller            # service logs
```

### CSP (air-gapped target, CAN/serial)

For air-gapped targets reachable only over CSP (CAN bus, KISS serial, ZMQ), use the **satdeploy-apm** C module inside [CSH](https://github.com/spaceinventor/csh). The Python CLI handles SSH only. CSP networking is handled natively in C by the APM, which talks directly to `satdeploy-agent` on the target.

| Piece | Where it runs | How to get it |
|-------|---------------|---------------|
| **satdeploy-apm** | Ground station (inside CSH) | [Build the APM](docs/building.md#satdeploy-apm-ground-station-native) |
| `satdeploy-agent` | Target satellite | [Yocto recipe or cross-compile](docs/building.md#satdeploy-agent-target-cross-compiled) |
| [CSH](https://github.com/spaceinventor/csh) | Ground station | Bridges ZMQ ↔ CAN/serial |

Start the agent on the target:

```bash
satdeploy-agent -i CAN  -p can0           # CAN bus
satdeploy-agent -i KISS -p /dev/ttyS1     # Serial link
satdeploy-agent -i ZMQ  -p localhost      # ZMQ (local testing only)
```

From CSH on the ground station:

```
satdeploy push controller
satdeploy status
satdeploy rollback controller
```

Both the Python CLI (SSH deploys) and the APM (CSP deploys) write to the same `history.db`, so `satdeploy status` shows a unified view regardless of transport.

If you just want to see the workflow without any of this, use `satdeploy demo`.

## Docs

- **[Command reference](docs/commands.md)**: every command and flag
- **[Configuration reference](docs/configuration.md)**: full config schema, transports, dependency ordering
- **[Building from source](docs/building.md)**: Python CLI, agent cross-compile, APM build, CSP version pinning

## Requirements

- Python 3.8+
- git (for the demo, and for provenance tracking on real deploys)
- SSH access to target *(SSH transport)*
- `satdeploy-agent` on target + `satdeploy-apm` + [CSH](https://github.com/spaceinventor/csh) on ground station *(CSP transport)*
- systemd on target

## License

MIT
