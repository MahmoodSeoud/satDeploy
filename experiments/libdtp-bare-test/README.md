# libdtp-bare-test

A clean-room minimal libdtp client + server, built to **falsify or confirm**
the tail-race claim in `CLAUDE.md` independently of any satdeploy code.

## What this tests

CLAUDE.md asserts that one `dtp_start_transfer()` call frequently fails to
deliver the full payload at 0% link loss because of how libdtp's receive
loop terminates. The empirical backing is `experiments/results/tail_race.csv`
(29/30 fails for the naive build).

That measurement was taken with the full satdeploy stack — agent, APM,
deploy handler, protobuf, slash module, all of it. If any of that wrapping
were the actual cause, you couldn't tell from `tail_race.csv` alone.

This test strips everything else away. Two binaries, ZMQ loopback,
single `dtp_start_transfer`, no retry. If the same failure rate reproduces,
the cause is libdtp. If it doesn't, satdeploy's wrapping is implicated.

## What's deliberately absent

The bare client does **not** do any of the things `satdeploy-agent/src/dtp_client.c`
does on top of libdtp:

- no retry loop (`DTP_MAX_RETRY_ROUNDS=0` equivalent)
- no `request_meta.intervals[]` patching
- no cross-pass resume / sidecar
- no `expected_hash` gate
- no `dtp_session_t` resume flag (passes `false`)

The only hook installed is `on_data_packet`, which writes received bytes
at `info.data_offset` and marks the packet seq in a local bitmap. The
bitmap is read once after `dtp_start_transfer` returns to count gaps —
nothing feeds back into libdtp.

## Layout

```
src/
  csp_init.{h,c}    bring CSP up over the local broker
  server.c          register one file as a payload, run dtp_server_main
  client.c          one dtp_start_transfer, write JSON stats to stdout
  mini_zmqproxy.c   minimal CSP-over-ZMQ broker (see "On zmqproxy" below)
scripts/
  sweep.sh          3 sizes × N=30 trials, append to results/ CSV
results/            output lands here (bare_tail_race.csv tracked)
meson.build
```

The build pulls libcsp/libparam/libdtp from `subprojects/` symlinks that
point at `../../satdeploy-agent/lib/{csp,param,dtp}`. The symlinks are
created at setup time (see Build below) and not tracked in git, since
meson rejects relative `subproject_dir` and we don't want to duplicate
the submodules.

## On zmqproxy

The libcsp-bundled `examples/zmqproxy` has a heap-corruption bug: its
unconditional capture/logging thread allocates a 1024-byte
`csp_packet_t` and `memcpy`s the full ZMQ message into `frame_begin`,
overflowing once `csp:buffer_size` exceeds 1024 (which it does for any
real DTP/CSP build). The process dies with `malloc(): corrupted top
size` mid-transfer.

That bug is **unrelated to the libdtp tail race** but produces results
that look identical at first glance — packets stop arriving, transfer
fails. To keep the broker out of the failure mode under test, this
project ships its own `mini-zmqproxy`: an XSUB↔XPUB `zmq_proxy` with
nothing else. Same wire protocol; no capture thread; no heap bug.

Worth filing upstream against libcsp separately — it's a real defect,
just not the one we're investigating here.

## Build

Run from the satdeploy-dev container (or any environment with the same
toolchain — meson, ninja, gcc, libcrypto, zmqproxy, libcsp build deps).

```bash
# 1. Make sure the agent's submodules are checked out — the bare test
#    builds against satdeploy-agent/lib/{csp,param,dtp}, sharing the same
#    libdtp pinned by the production agent.
cd /satdeploy
git submodule update --init --recursive

# 2. Wire the agent's lib trees into our subprojects/ via symlink. Meson
#    rejects relative subproject_dir, and we don't want to duplicate the
#    submodules, so symlinks bridge the two.
cd experiments/libdtp-bare-test
mkdir -p subprojects
ln -sfn ../../../satdeploy-agent/lib/csp   subprojects/csp
ln -sfn ../../../satdeploy-agent/lib/param subprojects/param
ln -sfn ../../../satdeploy-agent/lib/dtp   subprojects/dtp

# 3. Build native x86 — no ARM cross-compile. This is for falsification
#    on the loopback transport, not flight hardware.
meson setup build
ninja -C build   # produces bare-dtp-server, bare-dtp-client, mini-zmqproxy
```

## Run

```bash
# default sweep: 3 sizes × 30 trials
./scripts/sweep.sh

# smaller smoke run
TRIALS=3 SIZES="262144" ./scripts/sweep.sh

# different node/port:
SERVER_NODE=4040 CLIENT_NODE=5425 \
BARE_ZMQ_SUB_PORT=6000 BARE_ZMQ_PUB_PORT=7000 \
./scripts/sweep.sh
```

Output: `results/bare_tail_race.csv` with columns:

```
size_bytes, trial, push_rc, got, expected, gap, sha256_match,
first_gap_seq, last_gap_seq, duration_ms
```

`gap` is `expected - got` (number of unreceived seqs after the single
transfer attempt). `sha256_match=1` means the file ended up bit-identical
to what the server registered.

## Interpreting the result

Compare to `experiments/results/tail_race.csv` (the naive-build column).
Same cells (256K / 1M / 4M, 0% loss, ZMQ loopback) so the comparison is
direct.

| Outcome                                       | Conclusion                                                                                                                              |
|-----------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| ~28-29/30 cells fail with 1-2 packet tail gap | Reproduces. The behaviour is in libdtp, the framing in CLAUDE.md is correct, `DTP_MAX_RETRY_ROUNDS=8` is load-bearing as documented.    |
| 30/30 succeed (or near it)                    | Does not reproduce on bare libdtp. Something in satdeploy's wrapping (e.g. APM payload-id refresh, server thread teardown, ZMQ HWM) is causing the gap. CLAUDE.md needs rewriting. |
| Mostly succeed but a few fail in odd places   | Genuine but rare race. The 28/30 number from `tail_race.csv` was probably amplified by an unrelated satdeploy-side issue.               |

Whatever comes back, the result of this sweep should be cited next to
`tail_race.csv` in CLAUDE.md so future readers see both numbers.

## Caveats

- Native x86 over ZMQ loopback. Flight conditions (CAN, real loss, longer
  RTT) are **not** what this measures — it deliberately mirrors the
  conditions that produced `tail_race.csv` to keep the comparison apples
  to apples.
- The libdtp version exercised is whatever `satdeploy-agent/lib/dtp` is
  pinned at. If that submodule moves, re-run.
- We don't try to flush the CSP recv queue or otherwise work around
  anything inside libdtp. The point is to see what plain libdtp does.
