#!/usr/bin/env bash
# CSV row writer + simple parsers for the experiment harness. The schema is
# fixed across experiments so a single results CSV can be unioned across
# E1/E4/etc. for cross-experiment plotting.

set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/satdeploy/experiments/results}"
mkdir -p "$RESULTS_DIR"

# CSV schema. Add columns to the END to keep older runs compatible.
# v2: link_kind + Gilbert-Elliott + corrupt_pct columns appended for the
# KISS-pty path (UHF stand-in) — see experiments/README.md.
CSV_HEADER='trial_id,timestamp_utc,experiment,approach,link_kind,size_bytes,seed,loss_pct,burst_corr_pct,ge_p,ge_r,ge_loss_good,ge_loss_bad,corrupt_pct,delay_ms,jitter_ms,rate_bps,kill_at_byte,kill_at_s,max_passes,passes_used,wall_seconds,source_sha256,target_sha256,outcome,bytes_on_target,notes'

# csv_init <path>
#
# Creates the file with the header row if it doesn't exist. Idempotent —
# safe to call from every trial.
csv_init() {
    local path="$1"
    if [ ! -f "$path" ]; then
        echo "$CSV_HEADER" > "$path"
    fi
}

# csv_append <path> <field1> <field2> ...
#
# Appends one row. Fields are CSV-escaped (only commas matter for our use;
# we don't expect quotes or newlines in any field). Caller passes fields
# in CSV_HEADER order — count is checked.
csv_append() {
    local path="$1"; shift
    # Field count must match CSV_HEADER. Update both together.
    local expected=27
    if [ "$#" -ne "$expected" ]; then
        echo "csv_append: got $# fields, expected $expected" >&2
        return 1
    fi
    local row=""
    local first=1
    for f in "$@"; do
        if [ $first -eq 1 ]; then row="$f"; first=0
        else row="${row},${f}"
        fi
    done
    echo "$row" >> "$path"
}

# Outcome classifier — used by harness.sh to label each trial.
# Possible values:
#   success           SHA matches, transfer completed within budget
#   sha_mismatch      Transfer claimed success but bytes differ (BUG — investigate)
#   timeout           Wall-clock budget exceeded
#   target_missing    Target file doesn't exist after push
#   csh_error         csh exited non-zero
#   agent_died        Agent process exited unexpectedly mid-trial
#   passes_exhausted  E4-only: max_passes reached without completion

now_utc() { date -u +%FT%TZ; }

# Return wall-clock seconds (float, 3dp) between two `date +%s.%N` reads.
elapsed() {
    local start="$1" end="$2"
    awk "BEGIN { printf \"%.3f\", ${end} - ${start} }"
}
