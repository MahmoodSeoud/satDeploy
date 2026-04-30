#!/usr/bin/env bash
# E2 — Loss-rate curve.
#
# What it proves: success rate stays at 100% (or degrades gracefully) as
# the per-frame loss probability climbs, thanks to DTP's selective-repeat
# retry loop. The expected-shape result for the thesis is:
#
#   success rate
#       ^
#   100 +────────────────────────────_____
#       |                                 \____
#       |                                      \___
#       |                                          \____
#     0 +-------+-------+-------+-------+-------+-------+
#       0       5       10      20      30      40      50  loss%
#
# The headline finding is that DTP holds the line at 5–10% loss where a
# naive single-shot push would already be failing.
#
# Default link is KISS — pty pair with frame-level loss via impair.py,
# which is the realistic UHF model. Use LINK=zmq if you specifically want
# the TCP-shielded comparison row in the same CSV (mostly for writeup
# completeness).
#
# Output: experiments/results/e2_loss_curve.csv

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HARNESS="$(dirname "$HERE")/harness.sh"
CSV="${CSV:-/satdeploy/experiments/results/e2_loss_curve.csv}"
LINK="${LINK:-kiss}"
N="${N:-10}"
SIZE="${SIZE:-102400}"   # 100 KB — fast enough to sweep many cells
LOSSES_DEFAULT="0 1 2 5 10 20"
LOSSES="${LOSSES:-$LOSSES_DEFAULT}"

echo "[E2] csv=$CSV link=$LINK n=$N size=$SIZE losses=$LOSSES"

trial=0
for loss in $LOSSES; do
    for ((i=1; i<=N; i++)); do
        trial=$((trial + 1))
        seed="$((2000 + trial))"
        label="e2-${LINK}-loss${loss}-${i}"
        "$HARNESS" \
            --experiment e2 \
            --approach dtp_resume \
            --link "$LINK" \
            --size "$SIZE" \
            --seed "$seed" \
            --loss-pct "$loss" \
            --csv "$CSV" \
            --label "$label" \
            --max-passes 1 \
            --timeout-s 90 \
            --notes "loss${loss}pct"
    done
done

echo "[E2] done. Inspect: $CSV"
