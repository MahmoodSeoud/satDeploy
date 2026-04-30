#!/usr/bin/env bash
# Agent lifecycle helpers — start fresh, kill cleanly, kill mid-transfer,
# wait for readiness. The harness owns the agent process during a trial,
# so the user must stop the docker-entry tmux pane's agent before running
# experiments (or run inside a clean container).

set -euo pipefail

AGENT_BIN="${AGENT_BIN:-/satdeploy/satdeploy-agent/build-native/satdeploy-agent}"
AGENT_NODE="${AGENT_NODE:-5425}"
AGENT_LOG_DIR="${AGENT_LOG_DIR:-/tmp/satdeploy-experiments}"
AGENT_PIDFILE="${AGENT_PIDFILE:-${AGENT_LOG_DIR}/agent.pid}"

# Sidecar/target/backup paths the agent uses. Cleared between trials except
# in resume tests, where the sidecar is the whole point.
SESSION_STATE_DIR="${SESSION_STATE_DIR:-/var/lib/satdeploy/state}"
TARGET_DIR="${TARGET_DIR:-/tmp/satdeploy-target}"
BACKUP_DIR="${BACKUP_DIR:-/tmp/satdeploy-backups}"

mkdir -p "$AGENT_LOG_DIR" "$TARGET_DIR" "$BACKUP_DIR"

agent_running() {
    [ -f "$AGENT_PIDFILE" ] && kill -0 "$(cat "$AGENT_PIDFILE")" 2>/dev/null
}

agent_pid() {
    [ -f "$AGENT_PIDFILE" ] && cat "$AGENT_PIDFILE"
}

# Stop the agent gracefully, falling back to SIGKILL after 2s. Removes
# the pidfile on success. No-op if not running.
agent_stop() {
    if ! agent_running; then
        rm -f "$AGENT_PIDFILE"
        return 0
    fi
    local pid; pid="$(cat "$AGENT_PIDFILE")"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
        sleep 0.2
    fi
    rm -f "$AGENT_PIDFILE"
}

# Kill the agent immediately with SIGKILL — used to simulate a pass-window
# cutoff or a hard agent crash mid-transfer. This is the destructive sibling
# of agent_stop().
agent_kill_hard() {
    if ! agent_running; then return 0; fi
    kill -KILL "$(cat "$AGENT_PIDFILE")" 2>/dev/null || true
    rm -f "$AGENT_PIDFILE"
}

# Start the agent in the background. Re-uses an existing zmqproxy
# (docker-entry starts one); if it's not running, starts one.
#
# Args:
#   $1 — log file path. Absolute paths are used as-is; bare names are
#        joined to AGENT_LOG_DIR. Default: AGENT_LOG_DIR/agent.log.
agent_start() {
    local logarg="${1:-agent.log}"
    local logfile
    case "$logarg" in
        /*) logfile="$logarg" ;;
        *)  logfile="${AGENT_LOG_DIR}/${logarg}" ;;
    esac
    mkdir -p "$(dirname "$logfile")"

    if agent_running; then
        echo "agent_start: already running (pid $(agent_pid)); call agent_stop first" >&2
        return 1
    fi

    # Transport must already be up (caller's job — see transport_setup).
    # The agent process is what we own here; restarting it across passes
    # must NOT bounce zmqproxy / impair.py / vcan0 because that would
    # reset link state mid-experiment.

    # Build the agent's interface args. Word-split intentional — these are
    # multi-token strings ("-i ZMQ -p localhost -a 5425").
    local agent_args
    agent_args="$(transport_agent_args)"
    # shellcheck disable=SC2086
    nohup "$AGENT_BIN" $agent_args >"$logfile" 2>&1 &
    echo $! > "$AGENT_PIDFILE"

    # Brief grace period for CSP/DTP server bind. The agent doesn't print
    # a stable "ready" string in release builds, so we just wait a short
    # tick and trust that bind happens within ~200ms on localhost.
    sleep 0.3

    if ! agent_running; then
        echo "[agent_lifecycle] agent died during startup; log:" >&2
        tail -20 "$logfile" >&2
        return 1
    fi
    return 0
}

# Clear all transfer/backup state. Sidecar removal is opt-in via $1=keep.
agent_reset_state() {
    local sidecar_mode="${1:-clear}"  # "keep" preserves sidecars (resume test)
    rm -rf "${TARGET_DIR:?}/"*  2>/dev/null || true
    rm -rf "${BACKUP_DIR:?}/"*  2>/dev/null || true
    if [ "$sidecar_mode" != "keep" ]; then
        rm -rf "${SESSION_STATE_DIR:?}/"*  2>/dev/null || true
    fi
}

# Read the on-target file size, in bytes, for an app's `remote` path.
# Returns 0 if file doesn't exist (so callers can compute progress).
target_size() {
    local path="$1"
    [ -f "$path" ] || { echo 0; return; }
    stat -c%s "$path"
}
