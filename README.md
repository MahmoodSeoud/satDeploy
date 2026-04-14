```
  ███████╗ █████╗ ████████╗██████╗ ███████╗██████╗ ██╗      ██████╗ ██╗   ██╗
  ██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔════╝██╔══██╗██║     ██╔═══██╗╚██╗ ██╔╝
  ███████╗███████║   ██║   ██║  ██║█████╗  ██████╔╝██║     ██║   ██║ ╚████╔╝
  ╚════██║██╔══██║   ██║   ██║  ██║██╔══╝  ██╔═══╝ ██║     ██║   ██║  ╚██╔╝
  ██████╔╝██║  ██║   ██║   ██████╔╝███████╗██║     ███████╗╚██████╔╝   ██║
  ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═════╝ ╚══════╝╚═╝     ╚══════╝ ╚═════╝    ╚═╝
```

We shipped [DISCO-2](https://discosat.dk/v2_disco-2/) and had trouble replicating what it was running on a test flatsat.


Months of incremental updates over USB drives, ad-hoc SCP scripts, and Slack messages saying "I updated the binary." By launch, nobody could point at the hardware and say which commit was on it. After launch, we spent weeks trying to recreate the state on our flatsat from memory and old tmux sessions.

satdeploy is what we built so it doesn't happen again. Every deploy is versioned, hash-verified, and tagged with the git commit it came from. Every file can be rolled back with one command. It works over SSH for networked targets on the bench, and over [CSP](https://github.com/spaceinventor/libcsp) (CAN bus, KISS serial, ZMQ) for air-gapped satellite links.

> DISCO-2 is a 3U student CubeSat from Aarhus University, SDU, and ITU Copenhagen, launched on SpaceX Transporter-16 (March 30, 2026) to image Arctic glaciers from a 510 km sun-synchronous orbit. Coverage: [Danish Space News](https://danishspacenews.substack.com/p/disco-2-one-of-the-most-ambitious), [The Danish Dream](https://thedanishdream.com/danish-society/science/danish-students-launch-satellite-to-track-melting-arctic/), [project site](https://projects.au.dk/ausat/disco-2).

> **Early stage — but heading to orbit.** We built satdeploy *after* DISCO-2 launched, so the current payload is flying without it. The next uplink window will push satdeploy to the DISCO-2 payload, and every deploy after that will be versioned, hash-verified, and rollback-able from the ground. Until then it runs on our flatsat and we're looking for other satellite teams to try it before we trust it in orbit ourselves. Does this fit your workflow? What's missing? [Open an issue](https://github.com/MahmoodSeoud/satBuild/issues) or reach out.

## Try it now

Zero dependencies beyond Python and git.

```bash
pipx install satdeploy   # or: pip install satdeploy
satdeploy demo           
```

`satdeploy demo` sets up a throwaway git repo + local target directory and pre-installs `test_app` v1.0.0. Then try the real product loop:

```bash
satdeploy status              # See what's deployed
satdeploy push test_app       # Deploy v2 (new hash, new commit)
satdeploy rollback test_app   # Undo — one command, git tag carries through
satdeploy demo stop           # Tear it down when you're done
```

Real output:

```
$ satdeploy status
  demo · local · /Users/you/.satdeploy/demo/target

  APP       HEALTH         DEPLOYED  GIT            AGE
  ────────  ─────────────  ────────  ─────────────  ────────
  test_app  ● running      32c0702b  main@0c7e8fb2  just now

$ satdeploy push test_app

  ● Deploying test_app → demo

     32c0702b  main@0c7e8fb2        (current)
     5f3413a2  main@1f1750a6        (new)

  ✓  backup      20260414-124916-32c0702b.bak
  ✓  upload      0.1 KB · sha256 5f3413a2
  ✓  verify      checksum ok
  ·  service     no service configured — skipped

  Deployed in 0.03s.  Rollback with: satdeploy rollback test_app

$ satdeploy list test_app
  test_app  · 2 versions

     HASH      GIT            TIMESTAMP            STATUS
   · 32c0702b  main@0c7e8fb2  2026-04-14 12:49:16  backup
   → 5f3413a2  main@1f1750a6  2026-04-14 12:49:16  deployed

$ satdeploy rollback test_app

  → Rolling back test_app on demo

     5f3413a2  →  32c0702b

  ● Rolled back test_app to 32c0702b

$ satdeploy status
  demo · local · /Users/you/.satdeploy/demo/target

  APP       HEALTH         DEPLOYED  GIT            AGE
  ────────  ─────────────  ────────  ─────────────  ────────
  test_app  ● running      32c0702b  main@0c7e8fb2  just now
```

The thing to notice: every line shows both a file hash *and* the git commit it came from, and that link survives rollback. "What's live right now" is always a real commit SHA you can `git show`. That's the whole pitch.

The demo uses the same `LocalTransport`, history database, backup semantics, and rollback logic as a real deployment — every hash and git commit is real. When you're ready for hardware, run `satdeploy init`.

## Deploy to real hardware

### SSH (networked target)

Your target has network access. You don't need any C components — just the Python CLI.

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

### CSP (air-gapped target, CAN/serial) — experimental

The CSP path has more moving parts. You need three pieces running:

| Piece | Where it runs | How to get it |
|-------|---------------|---------------|
| Python CLI *or* CSH APM | Ground station | `pip install satdeploy` or [build the APM](docs/building.md#satdeploy-apm-ground-station-native) |
| `satdeploy-agent` | Target satellite | [Yocto recipe or cross-compile](docs/building.md#satdeploy-agent-target-cross-compiled) |
| [CSH](https://github.com/spaceinventor/csh) | Ground station | Bridges ZMQ ↔ CAN/serial |

Start the agent on the target:

```bash
satdeploy-agent -i CAN  -p can0           # CAN bus
satdeploy-agent -i KISS -p /dev/ttyS1     # Serial link
satdeploy-agent -i ZMQ  -p localhost      # ZMQ (local testing only)
```

On the ground station, `satdeploy init` (select "csp") gives you a config like:

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

Then `satdeploy push controller` works the same as SSH.

If you just want to see the workflow without any of this, use `satdeploy demo`.

## Docs

- **[Command reference](docs/commands.md)** — every command and flag
- **[Configuration reference](docs/configuration.md)** — full config schema, transports, dependency ordering
- **[Building from source](docs/building.md)** — Python CLI, agent cross-compile, APM build, CSP version pinning

## Requirements

- Python 3.8+
- git (for the demo, and for provenance tracking on real deploys)
- SSH access to target *(SSH transport)*
- `satdeploy-agent` on target + [CSH](https://github.com/spaceinventor/csh) on ground station *(CSP transport)*
- systemd on target

## License

MIT
