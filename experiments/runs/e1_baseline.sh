#!/usr/bin/env bash
# E1 — Baseline characterization.
#
# What it proves: on a clean link, satdeploy delivers a bit-exact binary
# end-to-end across all fixture sizes. Establishes:
#   - throughput ceiling (no impairments)
#   - zero-loss success rate (must be 100% — any failure is a real bug)
#   - SHA-bit-exact correctness (every trial)
#
# Output: experiments/results/e1_baseline.csv

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HARNESS="$(dirname "$HERE")/harness.sh"
CSV="${CSV:-/satdeploy/experiments/results/e1_baseline.csv}"
N="${N:-20}"          # trials per (size) cell
SIZES_DEFAULT="1024 102400 5242880"  # 1KB, 100KB, 5MB by default
SIZES="${SIZES:-$SIZES_DEFAULT}"

echo "[E1] csv=$CSV n=$N sizes=$SIZES"

trial=0
for size in $SIZES; do
    for ((i=1; i<=N; i++)); do
        trial=$((trial + 1))
        seed="$((1000 + trial))"
        label="e1-${size}-${i}"
        "$HARNESS" \
            --experiment e1 \
            --approach dtp_resume \
            --size "$size" \
            --seed "$seed" \
            --csv "$CSV" \
            --label "$label" \
            --max-passes 1 \
            --timeout-s 120 \
            --notes "baseline"
    done
done

echo "[E1] done. Inspect: $CSV"
