# Loss pattern file format

A loss pattern file describes when the link between ground station and spacecraft is up vs down, on a fine-grained timeline. The agent's `loss_filter` reads this file at startup and uses it to decide which incoming CSP packets to drop during testing — replicating real-world packet loss without needing real-world RF.

## Why this format

Three constraints shaped the design:

1. **Human-readable** — you should be able to open a pattern file in a text editor, see "yep, link went down for 700 ms at t=12.5", and trust that's what's loaded.
2. **Easy to derive from real telemetry** — the lab's pass logs naturally produce timestamped lock/unlock events. Direct mapping to our format.
3. **Easy to scale and combine** — multiplying drop rates, time-shifting, splicing two patterns together: all simple line-level transforms.

A simple line-oriented format meets all three. We don't need binary efficiency — pattern files are typically a few kB even for hour-long passes.

## Format

```
# header lines (optional, ignored)
# can include metadata for human use
# lines starting with `#` are comments

<t_offset_seconds>  <action>
```

### Time

`t_offset_seconds` is a non-negative float, seconds since the start of the pattern (= start of the satdeploy push from the agent's wall clock). Resolution: milliseconds (3 decimal places sufficient).

### Actions

| Action | Meaning |
|---|---|
| `up` | Link comes up. From this point forward, packets pass through. (Default state at t=0 is `up`.) |
| `down` | Link goes down. From this point forward, every CSP packet is dropped, until the next `up`. |
| `prob <p>` | From this point, drop each packet independently with probability `p` (0.0–1.0). IID Bernoulli — not realistic for RF but useful for sanity tests. |
| `clear` | Reset to the initial `up` state — clears any lingering `prob` or `gilbert`. |
| `latency <ms>` | Sleep `ms` ms before delivering each non-dropped packet. Orthogonal to drop actions and persists across `up` / `down` / `prob` / `gilbert` until another `latency` event (use `latency 0` to disable). Models the real radio's RTT floor — DISCO-2 UHF measured at ~1268 ms median for a 0-byte ping (n=218 non-loopback pings across 82 in-window passes; see `experiments/results/burstiness_analysis.md` for the source set). |
| `gilbert <p_GG> <p_BB> <drop_G> <drop_B>` | Switch to a two-state Markov burst-loss model. Good state drops at `drop_G`; Bad state drops at `drop_B`. After each packet, stay in Good with probability `p_GG`, else flip to Bad (symmetric for `p_BB`). Cancelled by any subsequent `up` / `down` / `clear` / `prob`. Reserve this for high-loss links or scenarios where bursts dominate; see `experiments/results/burstiness_analysis.md` for when it's actually needed. |

`up` / `down` are most faithful when you have per-packet outcome data (e.g. derived from `drun ping` sequences in DISCO-2 bird logs via `experiments/lib/parse_pass_log.py`). `prob` is appropriate when you have aggregate frame-loss stats over windows AND the loss pattern is roughly IID — which is the typical case for DISCO-2 in-window traffic (85% of passes fit Bernoulli per the stress-test analysis). `gilbert` is the right model when you know mean loss rate but the in-window pattern is verifiably bursty (about 9% of DISCO-2 passes per the same analysis). Mix-and-match within one file:

```
0.000    latency 1268        # real link RTT floor (DISCO-2 UHF median)
0.000    up
12.500   down                # lock loss starts
13.200   up                  # lock recovered
30.000   gilbert 0.95 0.6 0.05 0.95
                             # marginal-SNR window with bursts:
                             #   Good state (95% steady, 5% drop)
                             #   Bad state  (60% steady, 95% drop)
60.000   clear               # back to clean (latency persists)
```

### Beyond the last entry

Once the wall clock passes the last entry's timestamp, the link state stays at whatever the last entry set it to. So a pattern that ends in `down` permanently kills the link from that point on (useful for "pass ends" scenarios). A pattern ending in `up` (or naturally clean) lets every packet through after the recorded portion.

## Examples

### Synthetic — Bernoulli loss for sanity testing

```
# synthetic_5pct_bernoulli.pattern
# Constant 5% loss for filter sanity checks.
# Use this to verify the loss filter is wired correctly before
# trusting recorded patterns.

0.000  prob 0.05
```

### Synthetic — pass-window simulation (8 min up, 30 s down, repeating)

```
# passwin_8m_30s.pattern
# Compressed orbit pass cycle for F5 cross-pass resume tests.
# Real passes are 8 min separated by ~6 hours; we compress dead time
# to 30 s since the protocol is wall-clock invariant.

0.000     up
480.000   down
510.000   up
990.000   down
1020.000  up
1500.000  down
1530.000  up
```

### Recorded — real DISCO pass with two short fades

```
# pass_2026-04-15_dtu1.pattern
# Recorded from DISCO pass 2026-04-15T14:32:00Z, 8m12s duration.
# Source: ground station receive log, filter = "CSP packet not delivered
# to application within 1.0 s of expected arrival".
# Generated by: experiments/lib/parse_pass_log.py --from <input> > <this>

# pass_meta:duration_s=492 packets_attempted=1247 packets_received=1089
# pass_meta:total_drops=158 fade_events=2 max_fade_s=2.65

0.000   up
12.500  down
13.200  up        # first fade — 700 ms (typical short fade)
217.800 down
220.450 up        # second fade — 2.65 s (longer scintillation event)
```

## Programmatic transformations

The harness needs a few common transformations, all implementable as text-level operations on the pattern file. These live in `experiments/lib/parse_pass_log.py`.

### Scaling drop intensity

`--scale 2.0` doubles the down-time. Each `down`/`up` interval gets stretched by the factor (or duplicated). At `0.5` we keep half the events; at `2.0` we lengthen each gap or splice an additional gap nearby.

### Time-shifting

`--shift 30.0` adds 30 seconds to every timestamp. Useful for testing whether a pattern's specific timing matters (does DTP fail because the gap landed exactly during metadata exchange?).

### Splicing

`--splice <other.pattern>` concatenates two patterns. Useful for stitching together "good first half + bad second half" scenarios.

### Validation

`parse_pass_log.py --validate path.pattern` checks the file is well-formed before the agent reads it: timestamps monotonic, actions known, no impossible states.

## Where it's used

- `LOSS_PATTERN_FILE` env var picks the file at agent startup.
- The agent's loss filter (`satdeploy-agent/src/loss_filter.c`) parses on init, indexes by binary search at runtime.
- The harness exports `LOSS_PATTERN_FILE` per trial; CSV row records the pattern name in `notes`.
- Compile-time gated by `-DSATDEPLOY_TEST_LOSS_FILTER` so flight builds never include the filter, even by accident.
