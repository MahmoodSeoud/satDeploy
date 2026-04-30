# satdeploy experiment harness

Bench experiments that produce CSV evidence for the thesis claim:

> *DTP + cross-pass resume + SHA256 verification delivers a bit-exact binary across an unreliable, intermittent link, where naive approaches fail.*

The harness is one shell script per layer so each piece is inspectable on its own. Everything here is meant to run **inside the dev container** (`./scripts/docker-dev.sh`), which has the agent, csh, zmqproxy, `tc`, and the test fixtures all in one place.

## Layout

```
experiments/
├── README.md                 # this file
├── harness.sh                # one trial = one CSV row
├── lib/
│   ├── netem.sh              # tc qdisc / netem helpers (link impairment)
│   ├── transport.sh          # zmq | kiss | can dispatch + bring-up/teardown
│   ├── impair.py             # KISS-aware pty middleware (UHF stand-in)
│   ├── agent_lifecycle.sh    # start / stop / hard-kill / wait the agent
│   ├── csh_driver.sh         # drive csh non-interactively for `satdeploy push`
│   ├── fixtures.sh           # deterministic-by-seed binary generation
│   └── metrics.sh            # CSV schema + writer
├── runs/
│   ├── e1_baseline.sh        # E1: clean link, n=20 per size
│   ├── e2_loss_curve.sh      # E2: loss sweep
│   └── e4_resume.sh          # E4: kill mid-transfer, count passes-to-complete
└── results/                  # CSV outputs (gitignored)
```

## Three transports — pick the right one for the question you're answering

The harness's `--link {zmq|kiss|can}` flag selects the CSP transport. Each models a different real-world link, with different impairment plumbing.

| Link | Real-world analog | Impairment plumbing | Best for |
|---|---|---|---|
| `zmq`  | (dev convenience only) | `tc netem` on `lo` (loss shadowed by TCP — see below) | E1/E4/E5/E6/E8 — resume, state, crash, correctness regression |
| `kiss` | UHF radio modem (KISS over UART/AX.25) | byte/frame impairment via `impair.py` between two ptys | **E2/E3 loss curves — the headline UHF result** |
| `can`  | Spacecraft internal CAN bus | `tc netem` on `vcan0` (real frame loss) | CAN framing + CFP fragmentation; flatsat-only on Docker Desktop |

In **prod and real testing the satellite link is CAN bus or UHF** — never ZMQ. ZMQ is purely a dev convenience for the engine-room layers (resume, state, correctness).

### TCP caveat for ZMQ

`zmqproxy` is TCP-backed. `tc netem loss 25%` on `lo` drops packets, but TCP retransmits them silently — DTP never observes loss. **Use `--link zmq` only for resume/state/correctness experiments**. For loss curves, use `--link kiss` (or escalate to flatsat with a UDP CSP transport).

### KISS impairment middleware (`lib/impair.py`)

Models a UHF/serial KISS link the way it actually behaves: the radio sends and receives whole AX.25/KISS frames; fades drop frames as units; bit errors corrupt within a frame. The middleware:

- Creates two pty pairs and symlinks the slaves to `/tmp/agent_pty` and `/tmp/ground_pty` (libcsp-friendly char devices).
- Parses KISS framing (`FEND=0xC0`, `FESC=0xDB`, etc.) so loss is applied per-frame, not per-byte.
- Supports two loss models:
  - **Bernoulli** (`--loss-pct N`): each frame independently dropped with probability N.
  - **Gilbert-Elliott** (`--ge-p`, `--ge-r`, `--ge-loss-good`, `--ge-loss-bad`): two-state Markov chain producing realistic burst loss. Standard channel model — Gilbert (1960), Elliott (1963). Mean burst length = 1/r.
- Throttles throughput to channel rate (e.g. 9600 bps for UHF baseline). Bits/byte configurable (default 10 for UART 8-N-1).
- Per-byte bit-flip corruption (excluding FEND/FESC), modelling channel BER post-FEC.
- Optional latency + jitter.
- All RNG seeded by `--seed` for reproducibility — required for thesis.

### CAN on dev (vcan0)

Linux `vcan` is a virtual CAN bus that needs the `vcan` kernel module. **Docker Desktop on macOS uses LinuxKit which doesn't include `vcan`.** Symptom: `transport_setup` for `--link can` fails fast with:

```
[transport.can] failed to create vcan0.
    The vcan kernel module isn't available in this container.
```

If this fires, you have three options:
1. **Run the dev container on a Linux host** where `modprobe vcan` works.
2. **Run with `--privileged`** if the host kernel has `vcan` available but not loaded — usually doesn't help on Docker Desktop because the LinuxKit kernel doesn't ship it.
3. **Skip CAN on dev — escalate to flatsat for CAN testing.** This is the expected path; the harness's CAN code stays useful for flatsat without changes.

## What's runnable on the dev container today

| ID | Experiment | ZMQ | KISS | CAN | Notes |
|----|---|---|---|---|---|
| E1 | Baseline (clean link, all sizes) | ✅ | ✅ | flatsat | Confirms plumbing + bit-exact correctness |
| E4 | Cross-pass resume (kill mid-transfer) | ✅ | ✅ | flatsat | Headline thesis experiment |
| E5 | State-poisoning (re-stage diff hash) | ⏳ | ⏳ | flatsat | Easy follow-up; reuses harness |
| E6 | Crash matrix (SIGINT / SIGKILL / disk-full) | ⏳ | ⏳ | flatsat | Easy follow-up; reuses harness |
| E8 | Bit-exact correctness | ✅ | ✅ | flatsat | Asserted on every trial — implicit |
| E2 | Loss-rate curve (Bernoulli) | ⚠️ TCP shields | ✅ | flatsat | The thesis E2 chapter belongs on KISS |
| E3 | Burst loss (Gilbert-Elliott) | ⚠️ TCP shields | ✅ | flatsat | KISS supports `--ge-*` directly |
| E7 | Scaling | ✅ | ✅ | flatsat | Use `SIZES` env var on E1 runner |

## Prerequisites

The harness needs:

- `tc` (`iproute2`) — added to `Dockerfile.dev`
- `--cap-add=NET_ADMIN` — added to `scripts/docker-dev.sh`
- `--init` — added to `scripts/docker-dev.sh` to reap zombie processes (without it, accumulating `<defunct>` agent/csh/script processes wedge subsequent docker exec calls)
- The agent and APM built (`build-all` inside the container)
- Test config at `~/.satdeploy/config.yaml` (auto-installed by `docker-entry.sh`)

If you started the container before this commit landed, **rebuild the image**: `./scripts/docker-dev.sh` triggers a rebuild because `Dockerfile.dev` changed.

## Running

Inside the container, **stop the auto-launched agent in the right tmux pane first** (Ctrl-C in that pane), then:

```bash
cd /satdeploy

# E1 — baseline ZMQ. ~3 minutes for 20×3 trials at clean link.
./experiments/runs/e1_baseline.sh

# E4 — cross-pass resume on ZMQ. ~5 minutes for 5×3 trials at 5MB.
./experiments/runs/e4_resume.sh

# E2 — loss curve on KISS (the real one — ~3 min at default sizes/N).
LINK=kiss ./experiments/runs/e2_loss_curve.sh
```

Single ad-hoc trial:

```bash
# 100 KB through KISS at 5% Bernoulli loss
./experiments/harness.sh \
    --experiment smoke --link kiss \
    --size 102400 --seed 1 \
    --loss-pct 5 \
    --csv /tmp/smoke.csv --label first-run

# Same but Gilbert-Elliott burst model (heavy fades)
./experiments/harness.sh \
    --experiment smoke --link kiss \
    --size 102400 --seed 1 \
    --ge-p 5 --ge-r 50 --ge-loss-bad 80 \
    --csv /tmp/smoke.csv --label burst-run

# UHF throughput floor — 9600 bps throttle on a 1 KB push
./experiments/harness.sh \
    --experiment smoke --link kiss \
    --size 1024 --seed 1 \
    --rate-bps 9600 \
    --csv /tmp/smoke.csv --label uhf-rate
```

Override defaults via env vars on the runners:

```bash
N=5 SIZES="1024 102400" ./experiments/runs/e1_baseline.sh
SIZE=10485760 KILL_FRACS="0.10 0.50 0.90" MAX_PASSES=6 ./experiments/runs/e4_resume.sh
LINK=kiss N=10 LOSSES="0 1 2 5 10" ./experiments/runs/e2_loss_curve.sh
```

## CSV schema

One row per trial. Schema is fixed (defined in `lib/metrics.sh` as `CSV_HEADER`). New columns are appended only — older results stay readable.

```
trial_id, timestamp_utc, experiment, approach, link_kind,
size_bytes, seed,
loss_pct, burst_corr_pct, ge_p, ge_r, ge_loss_good, ge_loss_bad, corrupt_pct,
delay_ms, jitter_ms, rate_bps,
kill_at_byte, kill_at_s, max_passes, passes_used,
wall_seconds, source_sha256, target_sha256,
outcome, bytes_on_target, notes
```

`outcome` ∈ `success | sha_mismatch | timeout | target_missing | csh_error | agent_died | passes_exhausted`. Anything other than `success` warrants opening the per-trial logs in `/tmp/satdeploy-experiments/<label>/`.

## Analysis quickies

```bash
# Success rate by experiment / link
awk -F, 'NR>1 { tot[$3"|"$5]++; if ($25=="success") ok[$3"|"$5]++ }
         END { for (k in tot) print k, ok[k]"/"tot[k] }' \
    experiments/results/*.csv

# Loss curve: success rate vs --loss-pct (KISS only)
awk -F, 'NR>1 && $5=="kiss" { tot[$8]++; if ($25=="success") ok[$8]++ }
         END { for (l in tot) print l"%", ok[l]"/"tot[l] }' \
    experiments/results/e2_loss_curve.csv | sort -n
```

For real plotting pull the CSVs into pandas / R / your tool of choice. The schema is intentionally flat to make this trivial.

## Per-trial debugging

Every trial gets a directory at `/tmp/satdeploy-experiments/<label>/`:

- `pass-N.agent.log` — agent stdout/stderr per pass
- `pass-N.csh.log` — csh stdout (the `satdeploy push` output you'd see interactively)

KISS trials also leave `/tmp/satdeploy-experiments/impair.log` with frame-level stats:

```
impair[a->b]: in=538B out=421B frames=14 dropped=2
impair[b->a]: in=2466B out=2437B frames=23 dropped=1
```

The `dropped` count is your ground truth for "how many frames did DTP have to recover."

## Known limitations

- **Concurrent trials don't work.** The harness owns the single agent process, the single root qdisc on `lo`/`vcan0`, and (for KISS) the single impair.py instance. Run sequentially.
- **The "no-resume" control for E4 doesn't exist yet.** A clean comparison requires a build flag that no-ops the `session_state_*` calls in `dtp_client.c`. Without it, E4's evidence is "passes_used to completion" being small (2-3) instead of "never," which is suggestive but not a direct comparison.
- **CSP request/response on port 20 has no application-level retry**, so at very high frame loss the deploy command itself never reaches the agent. The harness records this as `outcome=timeout`. This is a real finding — DTP's retry loop only protects the data channel.
- **CAN dev tier requires a Linux host.** Docker Desktop's LinuxKit kernel doesn't ship `vcan`. Document and escalate.
