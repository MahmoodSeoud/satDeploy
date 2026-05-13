# Wiring the loss filter into the agent

This document describes how the trace-driven loss filter is wired into the
agent. Apply when running F3/F4/F5 thesis experiments. The filter is
**compile-time gated** by `-Dtest_loss_filter=true`. Flight builds get zero
overhead and zero footprint — the symbol does not exist in the binary.

## Architecture: one hook, all packets

```
                  ground (drun ping, csh, satdeploy push)
                                  |
                                  v
                        +---------+----------+
                        |  libcsp router     |
                        |  csp_qfifo_write() |   <-- single chokepoint
                        +---------+----------+
                                  |
              +-------------------+--------------------+
              | (TEST BUILDS ONLY: linker wraps it)    |
              v                                        v
   __wrap_csp_qfifo_write                  __real_csp_qfifo_write
     |                                            (the original)
     | should_drop()? -> free & return
     | apply_latency() -> sleep
     | then call __real_csp_qfifo_write
     v
   deploy_handler        libdtp recv        libparam pull       csh commands
   (port 20)             (ports 7,8)        (libparam)          (any port)
```

Every CSP packet — from any interface (ZMQ, CAN, KISS, ETH, I2C, UDP, loopback)
— flows through libcsp's `csp_qfifo_write` before reaching any application
subsystem. By wrapping that single function at link time, the filter sees
every packet exactly once, regardless of which subsystem owns the connection.

This was previously done with per-call-site hooks (in `deploy_handler.c` and
inside libdtp's recv loop). Those are no longer needed and are **not used**
in the current architecture — both would double-filter the same packet
against the iface-level hook.

## How it's wired

### 1. `src/loss_filter_iface_hook.c`

Provides `__wrap_csp_qfifo_write`. Compiled only when
`-Dtest_loss_filter=true`. Calls `loss_filter_should_drop()` /
`loss_filter_apply_latency()`, then forwards to `__real_csp_qfifo_write`
on the keep path.

### 2. `meson.build`

When `-Dtest_loss_filter=true`:
- Adds `src/loss_filter.c` and `src/loss_filter_iface_hook.c` to the agent
  source list.
- Adds `-DSATDEPLOY_TEST_LOSS_FILTER` to the agent's compile args (NOT
  global — libdtp must build byte-identical to upstream).
- Adds `-Wl,--wrap=csp_qfifo_write` to the agent's link args. The GNU/LLVM
  linker rewrites every call to `csp_qfifo_write` to hit our wrapper, and
  exposes the original as `__real_csp_qfifo_write` for the wrapper to call.

### 3. `src/main.c`

Calls `loss_filter_init()` near the top of main(), BEFORE `csp_init()`,
and registers `loss_filter_close` with atexit():

```c
#include "loss_filter.h"

int main(int argc, char **argv) {
    ...
    if (loss_filter_init() != 0) {
        fprintf(stderr, "loss_filter: pattern file failed to load\n");
        return 1;
    }
    atexit(loss_filter_close);
    ...
    csp_init(...);
    ...
}
```

The init reads `$LOSS_PATTERN_FILE`. If unset, the filter is enabled but
no-op (no events to apply) and every packet passes through unchanged.
If set, the file is parsed at startup and parse errors fail the agent
fast (init returns -1).

## What you get

| Subsystem             | Filtered? |
|-----------------------|-----------|
| Deploy command (port 20)         | yes  |
| DTP data + meta (libdtp recv)    | yes  |
| libparam pull / push             | yes  |
| Loopback packets between threads | yes  |
| Anything that goes through libcsp's router | yes |

The same drop/latency policy applies to every packet — same pattern file,
same statistics counters in `loss_filter_stats()`.

## Build commands

```bash
cd satdeploy-agent

# Test variant (loss filter compiled in, router-level hook active):
# IMPORTANT: -Db_lto=false is REQUIRED. With LTO on (the project default
# for flight), gcc inlines libcsp.a's csp_qfifo_write call sites past the
# linker stage, so -Wl,--wrap=csp_qfifo_write never gets a chance to
# intercept. The hook compiles fine and the wrap-mechanism unit test
# passes against mocked symbols, but the real ZMQ recv path bypasses
# the hook silently. Caught by experiments/test_e2e_bird_pattern.sh.
meson setup build-loss --wipe -Dtest_loss_filter=true -Db_lto=false
ninja -C build-loss

# Flight variant (default — zero filter code, libcsp linked normally):
meson setup build-flight --wipe
ninja -C build-flight
```

Verify the wrap is wired:
```bash
nm build-loss/satdeploy-agent | grep csp_qfifo_write
# Should show __wrap_csp_qfifo_write defined and __real_csp_qfifo_write referenced.

nm build-flight/satdeploy-agent | grep csp_qfifo_write
# Should show only csp_qfifo_write — no wrap symbols.
```

## Running an experiment

```bash
# Pick a pattern file derived from real bird logs:
PATTERN=experiments/results/bird-patterns-window/2026-04-22T135020+0000-csh.window.pattern

LOSS_PATTERN_FILE=$PATTERN \
LOSS_PATTERN_SEED=42 \
  ./build-loss/satdeploy-agent --zmq-endpoint tcp://localhost:9600 ...
```

Output at shutdown:
```
[loss_filter] loaded 27 events from .../*.window.pattern
[loss_filter] final stats: dropped 38 of 412 packets (9.22%)
```

## Tests

Regression coverage lives in `experiments/test_loss_filter_actions.sh`
(10 cases) and `experiments/lib/test_parse_pass_log.py` (16 cases). Run
both before touching anything in `src/loss_filter*.c` or
`experiments/lib/parse_pass_log.py`.

## Why not per-call-site hooks (the previous approach)

The earlier design had `loss_filter_should_drop()` checks inside each
subsystem's read path: one in `deploy_handler.c`, one patched into
`lib/dtp/src/dtp_client.c`. Two reasons that approach was retired:

1. **Coverage gaps.** Anything that ran outside those two call sites
   (libparam pulls, csh commands, RDP control packets) was invisible to
   the filter. F3.b sweeps showed clean transfers regardless of pattern
   content for non-DTP traffic.
2. **Compounded probabilities.** A packet that traveled through libdtp's
   queue and into the application would get filtered at both sites,
   giving an effective drop rate of `1 - (1-p)^2 ≈ 2p` for small `p`.
   Statistically wrong.

The router-level wrap fixes both. `lib/dtp` is no longer patched and
builds byte-identical to upstream — its `#ifdef SATDEPLOY_TEST_LOSS_FILTER`
block is dormant code that the agent's build never activates (the macro
is set only for the agent's own translation units, not the libdtp
subproject).
