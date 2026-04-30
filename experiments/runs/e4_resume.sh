#!/usr/bin/env bash
# E4 — Cross-pass resume.
#
# What it proves: when the agent is killed mid-transfer (simulating a
# pass-window cutoff), the next push for the same (app, expected_hash)
# resumes from the persisted bitmap instead of restarting at byte 0.
#
# Method: for each fixture size, kill the agent at K% of the file, then
# allow up to MAX_PASSES additional passes to complete. Compare:
#   - bytes_on_target after kill (should be > 0)
#   - passes_used to complete (resume should make this 2 or 3, not "never")
#   - SHA match on final byte
#
# This is the headline experiment. The control (no-resume) requires a
# different code path — for now the comparison is implicit: "single-pass
# under this kill schedule" already shown to fail for files larger than
# what fits in a pass.
#
# Output: experiments/results/e4_resume.csv

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HARNESS="$(dirname "$HERE")/harness.sh"
CSV="${CSV:-/satdeploy/experiments/results/e4_resume.csv}"
N="${N:-5}"
# Use 5MB (telemetry slot) by default — large enough to kill mid-transfer
# without spending forever, small enough to complete within the timeout.
SIZE="${SIZE:-5242880}"
# Kill at 25% / 50% / 75% of the file
KILL_FRACS_DEFAULT="0.25 0.50 0.75"
KILL_FRACS="${KILL_FRACS:-$KILL_FRACS_DEFAULT}"
MAX_PASSES="${MAX_PASSES:-4}"

echo "[E4] csv=$CSV n=$N size=$SIZE kill_fracs=$KILL_FRACS max_passes=$MAX_PASSES"

trial=0
for frac in $KILL_FRACS; do
    kill_at_byte="$(awk "BEGIN { printf \"%d\", $SIZE * $frac }")"
    for ((i=1; i<=N; i++)); do
        trial=$((trial + 1))
        seed="$((4000 + trial))"
        label="e4-frac${frac}-${i}"
        "$HARNESS" \
            --experiment e4 \
            --approach dtp_resume \
            --size "$SIZE" \
            --seed "$seed" \
            --csv "$CSV" \
            --label "$label" \
            --kill-at-byte "$kill_at_byte" \
            --max-passes "$MAX_PASSES" \
            --timeout-s 180 \
            --notes "kill_at_${frac}"
    done
done

echo "[E4] done. Inspect: $CSV"
