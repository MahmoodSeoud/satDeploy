#!/usr/bin/env bash
# Drive csh non-interactively to issue `satdeploy push` commands.
#
# csh is built on the slash REPL library. slash_create() calls tcgetattr()
# on stdin and returns NULL on ENOTTY — so csh exits with "Failed to init
# slash" if you just pipe commands or redirect stdin from /dev/null. To run
# csh non-interactively we have to allocate a pty. We use script(1) for
# that — it's in util-linux, always present.
#
# csh's batch contract is `csh -i <init.csh> <command>`: the init file is
# sourced (CSP setup), the trailing argv is run as a single slash command,
# then csh exits. We reuse the existing init/zmq.csh — no per-trial init
# file generation needed.

set -euo pipefail

CSH_BIN="${CSH_BIN:-/usr/local/bin/csh}"
CSH_AGENT_NODE="${CSH_AGENT_NODE:-${AGENT_NODE:-5425}}"

# Resolve the init file from transport.sh. Allows override for one-off
# debugging via $CSH_INIT.
_csh_init_path() {
    if [ -n "${CSH_INIT:-}" ]; then echo "$CSH_INIT"; return; fi
    transport_csh_init
}

# csh_push <app_name> <log_path> [timeout_s]
#
# Issues a single push for <app_name> against the agent at $CSH_AGENT_NODE,
# capturing all stdout/stderr to <log_path>. Returns csh's exit code, or
# 124 if the wall-clock timeout fires (matching coreutils' `timeout`).
#
# Why a timeout: at high loss rates the CSP route discovery / port-20
# request can never complete, and csh would otherwise hang indefinitely
# waiting for a response. The harness's --timeout-s caps the per-pass
# wait so the trial recorded as `timeout` instead of running for hours.
csh_push() {
    local app="$1"
    local log_path="$2"
    local timeout_s="${3:-${PUSH_TIMEOUT_S:-300}}"
    local init_file; init_file="$(_csh_init_path)"

    # script -q quiets the "Script started/done" framing.
    # script -f flushes after each write so the log is live-tail-able.
    # script -e propagates the wrapped command's exit code.
    # Output file = /dev/null means "don't write a typescript"; the wrapped
    # command's stdout/stderr is what we redirect into log_path.
    timeout --signal=KILL "$timeout_s" \
        script -qfec \
            "$CSH_BIN -i $init_file \"satdeploy push $app -n $CSH_AGENT_NODE\"" \
            /dev/null </dev/null >"$log_path" 2>&1
}

# csh_push_async <app_name> <log_path> <pid_out_file>
#
# Same as csh_push but launches in the background. The harness uses this
# for E4 (kill mid-transfer): start the push, sleep, kill the agent, see
# what state survives. The recorded pid is `script`'s pid; killing it
# tears the wrapped csh down too.
csh_push_async() {
    local app="$1"
    local log_path="$2"
    local pid_out="$3"
    local init_file; init_file="$(_csh_init_path)"
    nohup script -qfec \
        "$CSH_BIN -i $init_file \"satdeploy push $app -n $CSH_AGENT_NODE\"" \
        /dev/null </dev/null >"$log_path" 2>&1 &
    echo $! > "$pid_out"
}

# Wait for an async push (PID in <pid_file>) to exit. Returns its exit code,
# or 124 on timeout.
csh_wait() {
    local pid_file="$1"
    local timeout_s="${2:-300}"
    local pid; pid="$(cat "$pid_file")"
    local waited=0
    while [ $waited -lt "$timeout_s" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid" 2>/dev/null || true
            # bash builtin `wait` only knows pids it spawned; for nohup'd
            # ones we just check that the process is gone and return 0.
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    kill -TERM "$pid" 2>/dev/null || true
    return 124
}
