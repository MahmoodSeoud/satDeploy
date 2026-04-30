#!/usr/bin/env bash
# Transport abstraction for the experiment harness.
#
# Three link kinds are supported, each chosen via $LINK_KIND (or
# `--link <kind>` on the harness):
#
#   zmq   CSP-over-ZMQ on localhost. Dev convenience. ZMQ is TCP-backed
#         so link-layer loss isn't observed by DTP — use this for
#         resume / state / correctness experiments only.
#
#   kiss  CSP-over-KISS on a pty pair, with experiments/lib/impair.py
#         in the middle for byte/frame-level impairment. Models a UHF
#         radio link. Use this for loss-curve experiments on dev.
#
#   can   CSP-over-CAN on vcan0. Models the spacecraft internal bus.
#         vcan has near-zero loss (no hardware errors), so this tier
#         tests CAN framing + CFP fragmentation, not loss recovery.
#
# Each transport exposes:
#   transport_setup           bring up brokers/devices/middleware
#   transport_teardown        tear them down (idempotent)
#   transport_agent_args      args to pass `satdeploy-agent` (-i ... -p ...)
#   transport_csh_init        path to the csh init file

set -euo pipefail

LINK_KIND="${LINK_KIND:-zmq}"
AGENT_NODE="${AGENT_NODE:-5425}"

# Pty paths used by KISS mode. The harness clears these before each
# trial; impair.py recreates them.
KISS_AGENT_PTY="${KISS_AGENT_PTY:-/tmp/agent_pty}"
KISS_GROUND_PTY="${KISS_GROUND_PTY:-/tmp/ground_pty}"
KISS_BAUD="${KISS_BAUD:-9600}"

# CAN device name. Override with VCAN_DEV if you want multiple parallel
# vcan instances or a real can0.
VCAN_DEV="${VCAN_DEV:-vcan0}"

# Impairment params (used only by KISS). Set by harness.sh from CLI flags.
IMPAIR_LOSS_PCT="${IMPAIR_LOSS_PCT:-0}"
IMPAIR_GE_P="${IMPAIR_GE_P:-0}"
IMPAIR_GE_R="${IMPAIR_GE_R:-0}"
IMPAIR_GE_LOSS_GOOD="${IMPAIR_GE_LOSS_GOOD:-0}"
IMPAIR_GE_LOSS_BAD="${IMPAIR_GE_LOSS_BAD:-100}"
IMPAIR_CORRUPT_PCT="${IMPAIR_CORRUPT_PCT:-0}"
IMPAIR_RATE_BPS="${IMPAIR_RATE_BPS:-0}"
IMPAIR_LATENCY_MS="${IMPAIR_LATENCY_MS:-0}"
IMPAIR_JITTER_MS="${IMPAIR_JITTER_MS:-0}"
IMPAIR_SEED="${IMPAIR_SEED:-}"

IMPAIR_PIDFILE="${IMPAIR_PIDFILE:-/tmp/satdeploy-experiments/impair.pid}"
IMPAIR_LOG="${IMPAIR_LOG:-/tmp/satdeploy-experiments/impair.log}"
IMPAIR_READY="${IMPAIR_READY:-/tmp/satdeploy-experiments/impair.ready}"

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------------
# ZMQ
# --------------------------------------------------------------------------

_zmq_setup() {
    if ! pgrep -x zmqproxy >/dev/null 2>&1; then
        nohup zmqproxy >"${AGENT_LOG_DIR:-/tmp}/zmqproxy.log" 2>&1 &
        sleep 0.3
    fi
}
_zmq_teardown() { :; }
_zmq_agent_args() { echo "-i ZMQ -p localhost -a ${AGENT_NODE}"; }
_zmq_csh_init() { echo "/satdeploy/init/zmq.csh"; }

# --------------------------------------------------------------------------
# KISS — pty pair + impair.py
# --------------------------------------------------------------------------

_kiss_setup() {
    mkdir -p "$(dirname "$IMPAIR_PIDFILE")" "$(dirname "$IMPAIR_LOG")"

    # Tear down any prior impair.py before starting a new one.
    _kiss_teardown

    rm -f "$IMPAIR_READY"

    # Build impair.py args.
    local args=(
        "$THIS_DIR/impair.py"
        --agent-link "$KISS_AGENT_PTY"
        --ground-link "$KISS_GROUND_PTY"
        --ready-file "$IMPAIR_READY"
        --bits-per-byte 10
    )
    if [ "$IMPAIR_GE_P" != "0" ] || [ "$IMPAIR_GE_R" != "0" ]; then
        args+=(
            --ge-p "$IMPAIR_GE_P"
            --ge-r "$IMPAIR_GE_R"
            --ge-loss-good "$IMPAIR_GE_LOSS_GOOD"
            --ge-loss-bad "$IMPAIR_GE_LOSS_BAD"
        )
    elif [ "$IMPAIR_LOSS_PCT" != "0" ]; then
        args+=(--loss-pct "$IMPAIR_LOSS_PCT")
    fi
    [ "$IMPAIR_CORRUPT_PCT"  != "0" ] && args+=(--corrupt-pct "$IMPAIR_CORRUPT_PCT")
    [ "$IMPAIR_RATE_BPS"     != "0" ] && args+=(--rate-bps "$IMPAIR_RATE_BPS")
    [ "$IMPAIR_LATENCY_MS"   != "0" ] && args+=(--latency-ms "$IMPAIR_LATENCY_MS")
    [ "$IMPAIR_JITTER_MS"    != "0" ] && args+=(--jitter-ms "$IMPAIR_JITTER_MS")
    [ -n "$IMPAIR_SEED" ]              && args+=(--seed "$IMPAIR_SEED")

    nohup python3 "${args[@]}" >"$IMPAIR_LOG" 2>&1 &
    echo $! > "$IMPAIR_PIDFILE"

    # Wait for impair.py to set up the ptys.
    local waited=0
    while [ $waited -lt 30 ]; do
        if [ -f "$IMPAIR_READY" ] && [ -L "$KISS_AGENT_PTY" ] && [ -L "$KISS_GROUND_PTY" ]; then
            return 0
        fi
        if ! kill -0 "$(cat "$IMPAIR_PIDFILE")" 2>/dev/null; then
            echo "[transport.kiss] impair.py died during startup. Log:" >&2
            tail -20 "$IMPAIR_LOG" >&2
            return 1
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
    echo "[transport.kiss] impair.py didn't become ready within 3s" >&2
    tail -20 "$IMPAIR_LOG" >&2
    return 1
}

_kiss_teardown() {
    if [ -f "$IMPAIR_PIDFILE" ]; then
        local pid; pid="$(cat "$IMPAIR_PIDFILE")"
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
            for _ in 1 2 3 4; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.25
            done
            kill -KILL "$pid" 2>/dev/null || true
        fi
        rm -f "$IMPAIR_PIDFILE"
    fi
    rm -f "$KISS_AGENT_PTY" "$KISS_GROUND_PTY" "$IMPAIR_READY"
}

_kiss_agent_args() {
    echo "-i KISS -p ${KISS_AGENT_PTY} -b ${KISS_BAUD} -a ${AGENT_NODE}"
}

_kiss_csh_init() { echo "/satdeploy/init/kiss.csh"; }

# --------------------------------------------------------------------------
# CAN — vcan0
# --------------------------------------------------------------------------

_can_setup() {
    # vcan needs the kernel module. If it's not loaded and we can't
    # modprobe it (Docker containers usually can't), fail loudly with a
    # useful hint instead of a cryptic ip-link error.
    if ! ip link show "$VCAN_DEV" >/dev/null 2>&1; then
        if ! modprobe vcan 2>/dev/null; then
            : # might already be loaded; ip link will tell us
        fi
        if ! ip link add dev "$VCAN_DEV" type vcan 2>/dev/null; then
            echo "[transport.can] failed to create $VCAN_DEV." >&2
            echo "    The vcan kernel module isn't available in this container." >&2
            echo "    On Docker Desktop (macOS), you may need a host-side modprobe vcan" >&2
            echo "    in the LinuxKit VM, or run with --privileged." >&2
            return 1
        fi
    fi
    ip link set up "$VCAN_DEV" 2>/dev/null || true
}

_can_teardown() {
    # Leave vcan0 up across trials — bringing it down is slow and
    # subsequent trials would just bring it up again. The harness only
    # needs to ensure no stale agent is bound.
    :
}

_can_agent_args() { echo "-i CAN -p ${VCAN_DEV} -a ${AGENT_NODE}"; }
_can_csh_init() { echo "/satdeploy/init/can.csh"; }

# --------------------------------------------------------------------------
# Public dispatcher
# --------------------------------------------------------------------------

transport_setup() {
    case "$LINK_KIND" in
        zmq)  _zmq_setup ;;
        kiss) _kiss_setup ;;
        can)  _can_setup ;;
        *) echo "transport_setup: unknown LINK_KIND=$LINK_KIND" >&2; return 1 ;;
    esac
}

transport_teardown() {
    case "$LINK_KIND" in
        zmq)  _zmq_teardown ;;
        kiss) _kiss_teardown ;;
        can)  _can_teardown ;;
        *) ;; # unknown — no-op on teardown
    esac
}

transport_agent_args() {
    case "$LINK_KIND" in
        zmq)  _zmq_agent_args ;;
        kiss) _kiss_agent_args ;;
        can)  _can_agent_args ;;
        *) echo "transport_agent_args: unknown LINK_KIND=$LINK_KIND" >&2; return 1 ;;
    esac
}

transport_csh_init() {
    case "$LINK_KIND" in
        zmq)  _zmq_csh_init ;;
        kiss) _kiss_csh_init ;;
        can)  _can_csh_init ;;
        *) echo "transport_csh_init: unknown LINK_KIND=$LINK_KIND" >&2; return 1 ;;
    esac
}
