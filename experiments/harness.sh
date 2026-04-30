#!/usr/bin/env bash
# Per-trial experiment harness for satdeploy. One invocation = one trial =
# one CSV row.
#
# Stages:
#   1. Fixture: deterministic-by-seed binary written to disk
#   2. Setup: clear target/backup/sidecar, apply netem, (re)start agent
#   3. Push:   one or more passes of `satdeploy push`. For multi-pass tests
#              (E4), each pass may be terminated by an SIGKILL to the agent
#              partway through; the next pass relies on the sidecar to resume.
#   4. Verify: sha256(target_file) ?= sha256(fixture)
#   5. Teardown: stop agent, clear netem
#   6. Record: append CSV row
#
# Usage: see --help.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="$HERE/lib"

# Source order matters: transport.sh exports the helpers used by
# agent_lifecycle.sh and csh_driver.sh.
# shellcheck source=lib/netem.sh
. "$LIB/netem.sh"
# shellcheck source=lib/transport.sh
. "$LIB/transport.sh"
# shellcheck source=lib/agent_lifecycle.sh
. "$LIB/agent_lifecycle.sh"
# shellcheck source=lib/csh_driver.sh
. "$LIB/csh_driver.sh"
# shellcheck source=lib/fixtures.sh
. "$LIB/fixtures.sh"
# shellcheck source=lib/metrics.sh
. "$LIB/metrics.sh"

usage() {
    cat <<'EOF'
Usage: harness.sh [options]

Required:
  --experiment NAME     Experiment label (e1|e4|e5|e6|e8|...)
  --size BYTES          Fixture size in bytes
  --seed INT            Fixture seed (deterministic)
  --csv PATH            CSV output path (created if missing)

Approach:
  --approach NAME       dtp_resume (default), or any label for grouping
  --link KIND           zmq | kiss | can. Default: zmq.
                          zmq:  CSP-over-ZMQ on lo (TCP-backed, dev only)
                          kiss: CSP-over-KISS on a pty pair through impair.py
                                (UHF radio stand-in; --loss-pct / --ge-* etc
                                 are applied here as frame-level impairment)
                          can:  CSP-over-CAN on vcan0 (frame-level loss via
                                netem on vcan0; primarily a smoke transport)

Link impairment:
  --loss-pct N          For kiss: per-frame Bernoulli drop; for can/zmq:
                          netem loss% on the netdev. Default 0.
  --burst-corr N        Netem correlation pct for can/zmq paths (ignored
                          for kiss — use --ge-* for bursty kiss loss).
  --ge-p N              KISS only: Gilbert-Elliott P(G->B) percent.
                          When >0, GE supersedes --loss-pct.
  --ge-r N              KISS only: Gilbert-Elliott P(B->G) percent.
  --ge-loss-good N      KISS only: loss prob in Good state (default 0).
  --ge-loss-bad N       KISS only: loss prob in Bad state (default 100).
  --corrupt-pct N       KISS only: per-byte bit-flip rate (excludes
                          FEND/FESC; models channel BER post-FEC).
  --delay-ms N          One-way latency (default 0)
  --jitter-ms N         Latency jitter (default 0)
  --rate-bps N          Channel-rate cap. KISS: throttles bytes/sec at
                          rate_bps/10 (UART model). zmq/can: tbf rate.

Pass control:
  --max-passes N        Number of pushes to attempt (default 1)
  --kill-at-byte N      Kill agent when target file reaches N bytes
                        (per-pass, all but the last pass)
  --kill-at-s N         Kill agent N seconds into each push
                        (per-pass, all but the last pass)
  --timeout-s N         Per-pass wall-clock timeout (default 300)

Bookkeeping:
  --label STR           Free-form trial id (default: timestamp+pid)
  --notes STR           Free-form notes column in CSV

  -h, --help            Show this message
EOF
}

# Defaults
EXPERIMENT=""
APPROACH="dtp_resume"
LINK="zmq"
SIZE_BYTES=""
SEED=""
LOSS_PCT="0"
BURST_CORR="0"
GE_P="0"
GE_R="0"
GE_LOSS_GOOD="0"
GE_LOSS_BAD="100"
CORRUPT_PCT="0"
DELAY_MS="0"
JITTER_MS="0"
RATE_BPS="0"
KILL_AT_BYTE="0"
KILL_AT_S="0"
MAX_PASSES="1"
TIMEOUT_S="300"
CSV_PATH=""
LABEL=""
NOTES=""

while [ $# -gt 0 ]; do
    case "$1" in
        --experiment)    EXPERIMENT="$2"; shift 2;;
        --approach)      APPROACH="$2"; shift 2;;
        --link)          LINK="$2"; shift 2;;
        --size)          SIZE_BYTES="$2"; shift 2;;
        --seed)          SEED="$2"; shift 2;;
        --loss-pct)      LOSS_PCT="$2"; shift 2;;
        --burst-corr)    BURST_CORR="$2"; shift 2;;
        --ge-p)          GE_P="$2"; shift 2;;
        --ge-r)          GE_R="$2"; shift 2;;
        --ge-loss-good)  GE_LOSS_GOOD="$2"; shift 2;;
        --ge-loss-bad)   GE_LOSS_BAD="$2"; shift 2;;
        --corrupt-pct)   CORRUPT_PCT="$2"; shift 2;;
        --delay-ms)      DELAY_MS="$2"; shift 2;;
        --jitter-ms)     JITTER_MS="$2"; shift 2;;
        --rate-bps)      RATE_BPS="$2"; shift 2;;
        --kill-at-byte)  KILL_AT_BYTE="$2"; shift 2;;
        --kill-at-s)     KILL_AT_S="$2"; shift 2;;
        --max-passes)    MAX_PASSES="$2"; shift 2;;
        --timeout-s)     TIMEOUT_S="$2"; shift 2;;
        --csv)           CSV_PATH="$2"; shift 2;;
        --label)         LABEL="$2"; shift 2;;
        --notes)         NOTES="$2"; shift 2;;
        -h|--help)       usage; exit 0;;
        *) echo "Unknown option: $1" >&2; usage; exit 2;;
    esac
done

case "$LINK" in
    zmq|kiss|can) ;;
    *) echo "Bad --link '$LINK' — must be zmq|kiss|can" >&2; exit 2;;
esac
export LINK_KIND="$LINK"

for required in EXPERIMENT SIZE_BYTES SEED CSV_PATH; do
    if [ -z "${!required}" ]; then
        echo "Missing --${required,,}" >&2
        usage
        exit 2
    fi
done

if [ -z "$LABEL" ]; then
    LABEL="$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi

# Per-trial run dir for logs. Keeps stdout/stderr from the agent and from
# each csh push so post-mortem on a failed trial is possible.
RUN_DIR="${AGENT_LOG_DIR}/${LABEL}"
mkdir -p "$RUN_DIR"

# Always tear down on exit, regardless of why we're exiting. tc rules,
# stale agents, and lingering impair.py / pty symlinks would poison
# subsequent trials.
cleanup() {
    set +e
    netem_clear
    agent_stop
    transport_teardown
}
trap cleanup EXIT

csv_init "$CSV_PATH"

# ---- 1. Fixture ----------------------------------------------------------
fixture_path="$(fixture_make "$SIZE_BYTES" "$SEED")"
source_sha="$(fixture_sha256 "$fixture_path")"
app_name="$(fixture_install_into_config "$fixture_path" "")"

# Resolve the remote path the agent will write to. Pulled from
# ~/.satdeploy/config.yaml without requiring a YAML parser — just grep the
# `remote:` field in the matching app block. Brittle but contained.
remote_path="$(awk -v app="$app_name" '
    $0 ~ "^  "app":$" { in_app = 1; next }
    in_app && /^  [a-zA-Z]/ && !/^    / { in_app = 0 }
    in_app && /remote:/ { print $2; exit }
' /root/.satdeploy/config.yaml)"
if [ -z "$remote_path" ]; then
    remote_path="/tmp/satdeploy-target/${app_name}"
fi

# ---- 2. Setup -----------------------------------------------------------
# Initialize outcome to empty so the early-fail path below can set it
# before the push loop runs (set -u would otherwise blow up on the
# `[ -n "$outcome" ]` check).
outcome=""
target_sha=""

agent_stop  # ensure no leftover from docker-entry's tmux pane
agent_reset_state clear  # clear sidecar — fresh transfer

# Impairment routing differs by link kind:
#   - kiss: pass loss/burst/corrupt/rate to impair.py via env vars; no netem.
#   - zmq:  netem on lo (delay/throttle work; loss is shadowed by TCP).
#   - can:  netem on vcan0 (drops CAN frames at the netdev layer).
case "$LINK_KIND" in
    kiss)
        export IMPAIR_LOSS_PCT="$LOSS_PCT"
        export IMPAIR_GE_P="$GE_P"
        export IMPAIR_GE_R="$GE_R"
        export IMPAIR_GE_LOSS_GOOD="$GE_LOSS_GOOD"
        export IMPAIR_GE_LOSS_BAD="$GE_LOSS_BAD"
        export IMPAIR_CORRUPT_PCT="$CORRUPT_PCT"
        export IMPAIR_RATE_BPS="$RATE_BPS"
        export IMPAIR_LATENCY_MS="$DELAY_MS"
        export IMPAIR_JITTER_MS="$JITTER_MS"
        export IMPAIR_SEED="$SEED"
        ;;
    can)
        NETEM_IFACE=vcan0 netem_apply "$LOSS_PCT" "$BURST_CORR" "$DELAY_MS" "$JITTER_MS" "$RATE_BPS"
        ;;
    zmq)
        netem_apply "$LOSS_PCT" "$BURST_CORR" "$DELAY_MS" "$JITTER_MS" "$RATE_BPS"
        ;;
esac

# Bring the transport up once for the entire trial — we restart the agent
# across passes (E4) but the link state (impair.py / vcan0 / zmqproxy)
# stays consistent.
if ! transport_setup; then
    # transport already wrote a useful error to stderr; record an outcome
    # row so the CSV reflects the failed trial.
    outcome="agent_died"
fi

start_ts="$(date +%s.%N)"
passes_used=0

# ---- 3. Push loop -------------------------------------------------------
# Skip the loop entirely if transport_setup failed — outcome is already
# set to agent_died and there's nothing to push.
[ -n "$outcome" ] && MAX_PASSES=0
for ((pass=1; pass <= MAX_PASSES; pass++)); do
    passes_used=$pass
    push_log="${RUN_DIR}/pass-${pass}.csh.log"
    agent_log="${RUN_DIR}/pass-${pass}.agent.log"
    pid_file="${RUN_DIR}/pass-${pass}.csh.pid"

    if ! agent_start "$agent_log"; then
        outcome="agent_died"
        break
    fi

    # Decide whether this pass should be killed mid-transfer. We kill on
    # all but the final pass — the final pass is the one we expect to
    # complete the transfer.
    should_kill_this_pass=0
    if [ "$pass" -lt "$MAX_PASSES" ]; then
        if [ "$KILL_AT_BYTE" != "0" ] || [ "$KILL_AT_S" != "0" ]; then
            should_kill_this_pass=1
        fi
    fi

    if [ "$should_kill_this_pass" = "1" ]; then
        csh_push_async "$app_name" "$push_log" "$pid_file"
        # Poll for kill condition. Whichever fires first wins.
        deadline=$(( $(date +%s) + TIMEOUT_S ))
        killed=0
        while [ "$(date +%s)" -lt "$deadline" ]; do
            if [ "$KILL_AT_BYTE" != "0" ]; then
                cur="$(target_size "$remote_path")"
                if [ "$cur" -ge "$KILL_AT_BYTE" ]; then
                    agent_kill_hard
                    killed=1
                    break
                fi
            fi
            if [ "$KILL_AT_S" != "0" ]; then
                # KILL_AT_S is per-pass; we measure since the loop started.
                # (Not since fixture stage — kill timing should be insensitive
                # to fixture generation cost.)
                : # handled by sleep cadence below
            fi
            sleep 0.1
            # Has the push finished on its own (e.g., transfer completed
            # before kill threshold)? If so, break out — no kill needed.
            if ! kill -0 "$(cat "$pid_file" 2>/dev/null)" 2>/dev/null; then
                break
            fi
        done

        # Time-based kill (if configured) — applied in addition to the
        # byte-based check above, simpler scheduling.
        if [ "$killed" = "0" ] && [ "$KILL_AT_S" != "0" ]; then
            sleep "$KILL_AT_S"
            agent_kill_hard
            killed=1
        fi

        # Wait for csh to notice and exit (it'll error out when its CSP
        # request times out). We don't care about its rc here — kill is
        # the expected event.
        csh_wait "$pid_file" 30 || true
        # Don't tear down sidecar — that's the whole point of this pass.
        continue
    fi

    # Final-pass (or single-pass) push — synchronous, walled by TIMEOUT_S.
    # Pass our timeout into csh_push so a hung CSP handshake at high loss
    # doesn't run for hours.
    set +e
    csh_push "$app_name" "$push_log" "$TIMEOUT_S"
    rc=$?
    set -e
    if [ "$rc" = "124" ] || [ "$rc" = "137" ]; then
        outcome="timeout"
    fi
    # Otherwise csh's exit code isn't reliable (it returns nonzero on
    # graceful exit too) — let the SHA check below decide success/fail.

    # Verify outcome.
    if [ ! -f "$remote_path" ]; then
        outcome="${outcome:-target_missing}"
        break
    fi
    target_sha="$(sha256sum "$remote_path" | awk '{print $1}')"
    if [ "$target_sha" = "$source_sha" ]; then
        outcome="success"
    else
        outcome="sha_mismatch"
    fi
    break
done

if [ -z "$outcome" ]; then
    # Fell out of the loop without setting outcome — must be passes_exhausted.
    outcome="passes_exhausted"
    if [ -f "$remote_path" ]; then
        target_sha="$(sha256sum "$remote_path" | awk '{print $1}')"
    fi
fi

end_ts="$(date +%s.%N)"
wall_seconds="$(elapsed "$start_ts" "$end_ts")"
bytes_on_target="$(target_size "$remote_path")"

csv_append "$CSV_PATH" \
    "$LABEL" \
    "$(now_utc)" \
    "$EXPERIMENT" \
    "$APPROACH" \
    "$LINK" \
    "$SIZE_BYTES" \
    "$SEED" \
    "$LOSS_PCT" \
    "$BURST_CORR" \
    "$GE_P" \
    "$GE_R" \
    "$GE_LOSS_GOOD" \
    "$GE_LOSS_BAD" \
    "$CORRUPT_PCT" \
    "$DELAY_MS" \
    "$JITTER_MS" \
    "$RATE_BPS" \
    "$KILL_AT_BYTE" \
    "$KILL_AT_S" \
    "$MAX_PASSES" \
    "$passes_used" \
    "$wall_seconds" \
    "$source_sha" \
    "${target_sha:-}" \
    "$outcome" \
    "$bytes_on_target" \
    "${NOTES// /_}"

echo "[harness] ${LABEL} ${EXPERIMENT}/${APPROACH} link=${LINK} size=${SIZE_BYTES} seed=${SEED} -> ${outcome} (${wall_seconds}s, ${passes_used} pass(es))"
