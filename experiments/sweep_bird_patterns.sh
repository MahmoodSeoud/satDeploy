#!/usr/bin/env bash
# sweep_bird_patterns.sh — F3.b sweep at realistic DISCO-2 FER levels.
#
# sweep_loss_rates.sh covered synthetic Bernoulli loss at 1/5/10%. The
# 1/5/10% guesses understate the regime: of 71 in-window DISCO passes,
# every one had FER in [28%, 87%] (median 62%). This sweep runs at those
# realistic levels.
#
# Methodology:
#   - For each of 6 bird passes (spanning the FER distribution), derive a
#     synthetic `prob <fer>` pattern at the trace's measured in-window FER.
#   - Why synthetic-from-measured instead of the raw bird pattern: the bird
#     patterns are 100-660 s timelines but a 1 MB push completes in <5 s,
#     so the timed `down` events fire AFTER the push ends. Using the trace's
#     mean FER + IID Bernoulli delivery actually exercises the link during
#     the push; the burstiness analysis (experiments/burstiness_analysis.py)
#     showed 85% of in-window passes fit Bernoulli, so IID is empirically
#     justified for this dataset.
#
# Scope:
#   - 6 bird passes picked across FER buckets [0.28-0.45), [0.45-0.65), [0.65-0.90)
#   - builds: naive-loss (`-Dnaive_baseline=true -Dtest_loss_filter=true`)
#             smart-loss (`-Dtest_loss_filter=true`)
#   - 1 MB payload (matches sweep_loss_rates for cross-comparison)
#   - 1 trial per (build, pattern) — 12 trials total
#   - timeout=600s per trial (smart at >70% FER can run all 8 retry rounds)
#
# Per-trial CSV row:
#   build, pattern, bird_fer, push_rc, got, total, retry_rounds, gap,
#   gap_class, dropped_by_filter, actual_fer, notes
#
# Expected story:
#   At realistic FER (most patterns >40%), even the smart build won't close
#   a 1MB transfer inside the 8-round budget. That's the dividing line where
#   single-pass retry stops being enough and cross-pass resume earns its keep.
#
# Run inside the satdeploy-dev container:
#   docker run --rm --init -v "$PWD:/satdeploy" -w /satdeploy satdeploy-dev \
#     bash experiments/sweep_bird_patterns.sh

set -euo pipefail

REPO=/satdeploy
LOG_DIR=/tmp/sweep-bird-patterns
TARGET_DIR=/tmp/sweep-bird-patterns-target
NODE=5425
SIZE=1048576

SMART_AGENT="$REPO/satdeploy-agent/build-loss/satdeploy-agent"
NAIVE_AGENT="$REPO/satdeploy-agent/build-naive-loss/satdeploy-agent"
APM_SO="$REPO/satdeploy-apm/build/libcsh_satdeploy_apm.so"

# Build any missing variants. LTO must be OFF for both (the iface hook
# depends on --wrap which LTO defeats).
if [ ! -x "$SMART_AGENT" ]; then
    echo ">>> Building smart-loss variant"
    rm -rf "$REPO/satdeploy-agent/build-loss"
    meson setup "$REPO/satdeploy-agent/build-loss" "$REPO/satdeploy-agent" \
        -Dtest_loss_filter=true -Db_lto=false
    ninja -C "$REPO/satdeploy-agent/build-loss"
fi
if [ ! -x "$NAIVE_AGENT" ]; then
    echo ">>> Building naive-loss variant"
    rm -rf "$REPO/satdeploy-agent/build-naive-loss"
    meson setup "$REPO/satdeploy-agent/build-naive-loss" "$REPO/satdeploy-agent" \
        -Dtest_loss_filter=true -Dnaive_baseline=true -Db_lto=false
    ninja -C "$REPO/satdeploy-agent/build-naive-loss"
fi
if [ ! -f "$APM_SO" ]; then
    echo ">>> Building APM"
    rm -rf "$REPO/satdeploy-apm/build"
    meson setup "$REPO/satdeploy-apm/build" "$REPO/satdeploy-apm"
    ninja -C "$REPO/satdeploy-apm/build"
fi

mkdir -p "$LOG_DIR" "$TARGET_DIR" /var/lib/satdeploy/state
mkdir -p ~/.local/lib/csh && cp -f "$APM_SO" ~/.local/lib/csh/

mkdir -p ~/.satdeploy
cat > ~/.satdeploy/config.yaml <<EOF
name: sweep-bird
zmq_endpoint: tcp://localhost:6000
agent_node: $NODE
ground_node: 4040
backup_dir: $TARGET_DIR/backups
max_backups: 3
apps:
  hello:
    local: $LOG_DIR/hello.bin
    remote: $TARGET_DIR/hello.bin
    service: null
EOF

CSV="$REPO/experiments/results/bird_sweep.csv"
mkdir -p "$(dirname "$CSV")"
echo "build,pattern,bird_fer,push_rc,got,total,retry_rounds,gap,gap_class,dropped_by_filter,actual_fer,notes" > "$CSV"

# Patterns selected by experiments/sweep_bird_patterns.sh source: each
# bucket gets up to 2 picks. Selection is reproducible via random.seed(13)
# in the picker; freezing the names here avoids re-picking on rerun.
PATTERNS=(
    "2026-04-23T120036+0000-csh:0.287"
    "2026-04-25T130120+0000-csh:0.448"
    "2026-04-29T041915+0000-csh:0.456"
    "2026-04-28T134657+0000-csh:0.600"
    "2026-04-28T043543+0000-csh:0.700"
    "2026-04-29T150601+0000-csh:0.859"
)

PATTERN_DIR="$REPO/experiments/results/bird-patterns-window"
DERIVED_DIR="$LOG_DIR/derived"
mkdir -p "$DERIVED_DIR"

run_one() {
    local build_label="$1"
    local agent_bin="$2"
    local pattern_name="$3"
    local bird_fer="$4"

    # Synthesize a Bernoulli pattern at the trace's measured in-window FER.
    # See methodology comment at top of file.
    #
    # Why the 2.0 s lead-in `up`: the synthetic loss starts at t=0 of the
    # agent's wall clock, which is also when the DEPLOY protobuf RPC on
    # CSP port 20 fires (2-4 packets). Dropping those packets stalls the
    # deploy before DTP ever starts. 2 s is plenty for the handshake and
    # is short enough that bulk DTP still spans 99%+ of the push.
    local pattern_file="$DERIVED_DIR/${pattern_name}.bernoulli.pattern"
    cat > "$pattern_file" <<EOF
# Derived from bird trace: $pattern_name
# In-window FER (measured): $bird_fer
# Delivered as IID Bernoulli; see burstiness_analysis.md for justification.
0.000 up
2.000 prob $bird_fer
EOF

    # Seed derived from pattern name so naive/smart see the same PRNG
    # sequence against the same pattern (apples-to-apples comparison).
    local seed
    seed=$(printf "0x%08x" $(( 0x$(echo -n "$pattern_name" | md5sum | cut -c1-8) ^ 0xC0FFEE )))

    rm -rf /var/lib/satdeploy/state/* "$TARGET_DIR/hello.bin" "$TARGET_DIR/backups" 2>/dev/null || true
    pkill -x satdeploy-agent 2>/dev/null || true
    pkill -x zmqproxy 2>/dev/null || true
    sleep 0.3

    dd if=/dev/urandom of="$LOG_DIR/hello.bin" bs=1 count="$SIZE" status=none

    local exp_packets=$(( (SIZE + 1015) / 1016 ))

    zmqproxy >/dev/null 2>&1 &
    local zmq_pid=$!
    sleep 0.5

    local agent_log="$LOG_DIR/agent.${build_label}.${pattern_name}.log"
    LOSS_PATTERN_FILE="$pattern_file" \
    LOSS_PATTERN_SEED="$seed" \
        "$agent_bin" -i ZMQ -p localhost -a "$NODE" >"$agent_log" 2>&1 &
    local agent_pid=$!
    sleep 1

    local push_rc=0
    timeout --signal=KILL 600 \
        script -qfec "csh -i $REPO/init/zmq.csh \"satdeploy push hello -n $NODE\"" \
        /dev/null </dev/null \
        >"$LOG_DIR/push.${build_label}.${pattern_name}.log" 2>&1 \
        || push_rc=$?

    kill -TERM "$agent_pid" 2>/dev/null || true
    wait "$agent_pid" 2>/dev/null || true
    kill "$zmq_pid" 2>/dev/null || true

    local got total rounds notes=""
    local complete_line incomplete_line
    complete_line=$(grep -oE "complete \([0-9]+ packets, [0-9]+ retry round" "$agent_log" | tail -1 || true)
    incomplete_line=$(grep -oE "incomplete after [0-9]+ retry round\(s\) \([0-9]+/[0-9]+ packets\)" "$agent_log" | tail -1 || true)

    if [ -n "$complete_line" ]; then
        got=$(echo "$complete_line"   | grep -oE "\([0-9]+"        | tr -d '(')
        total="$got"
        rounds=$(echo "$complete_line" | grep -oE ", [0-9]+ retry"  | grep -oE "[0-9]+")
    elif [ -n "$incomplete_line" ]; then
        rounds=$(echo "$incomplete_line" | grep -oE "after [0-9]+"  | grep -oE "[0-9]+")
        got=$(echo "$incomplete_line"    | grep -oE "\([0-9]+/"     | tr -d '(/')
        total=$(echo "$incomplete_line"  | grep -oE "/[0-9]+ packets" | grep -oE "[0-9]+")
    else
        got=0; total="$exp_packets"; rounds="X"; notes="no_status_line"
    fi

    local dropped
    dropped=$(grep -oE "dropped [0-9]+ of [0-9]+" "$agent_log" | tail -1 | grep -oE "^dropped [0-9]+" | grep -oE "[0-9]+" || true)
    [ -z "$dropped" ] && dropped="?"

    # actual_fer = dropped_by_filter / total_seen_by_filter
    local total_seen actual_fer
    total_seen=$(grep -oE "dropped [0-9]+ of [0-9]+" "$agent_log" | tail -1 | grep -oE "of [0-9]+" | grep -oE "[0-9]+" || true)
    if [ -n "$total_seen" ] && [ "$total_seen" != "0" ] && [ "$dropped" != "?" ]; then
        actual_fer=$(awk "BEGIN {printf \"%.4f\", $dropped/$total_seen}")
    else
        actual_fer="?"
    fi

    local gap gap_class
    if [ "$got" = "$total" ] && [ "$got" != "0" ]; then
        gap=0; gap_class="none"
    elif [ "$rounds" = "X" ]; then
        gap="?"; gap_class="no_data"
    else
        gap=$(( total - got ))
        if   [ "$gap" -le 3 ];  then gap_class="tiny";
        elif [ "$gap" -le 20 ]; then gap_class="small";
        elif [ "$gap" -le 100 ]; then gap_class="medium";
        else                          gap_class="large"; fi
    fi

    echo "$build_label,$pattern_name,$bird_fer,$push_rc,$got,$total,$rounds,$gap,$gap_class,$dropped,$actual_fer,$notes" | tee -a "$CSV"
}

echo
echo "--- sweep_bird_patterns: 1MB, naive-loss vs smart-loss, ${#PATTERNS[@]} real DISCO patterns ---"
echo "--- $(( ${#PATTERNS[@]} * 2 )) total trials ---"
echo

for pp in "${PATTERNS[@]}"; do
    pattern_name="${pp%%:*}"
    bird_fer="${pp##*:}"
    run_one "naive-loss" "$NAIVE_AGENT" "$pattern_name" "$bird_fer"
    run_one "smart-loss" "$SMART_AGENT" "$pattern_name" "$bird_fer"
done

echo
echo "--- summary per (build, pattern) ---"
awk -F, 'NR>1 {
    key=sprintf("%-12s FER=%s", $1, $3)
    n[key]++
    if ($4 == "0")              push_ok[key]++
    if ($7 != "X" && $7 != "")  rounds_sum[key]+=$7
    if ($7 != "X" && $7 > 0)    used_retry[key]++
    if ($9 == "none")           complete[key]++
} END {
    for (k in n)
        printf "%s  push_ok=%d/%d  complete=%d/%d  used_retry=%d/%d  mean_rounds=%.1f\n",
               k, push_ok[k]+0, n[k], complete[k]+0, n[k], used_retry[k]+0, n[k],
               (rounds_sum[k]+0)/n[k]
}' "$CSV" | sort
echo
echo "--- by build (aggregate) ---"
awk -F, 'NR>1 {
    n[$1]++
    if ($9 == "none") complete[$1]++
    if ($7 != "X")    rounds_sum[$1]+=$7
} END {
    for (b in n)
        printf "%-12s  complete=%d/%d  mean_rounds=%.1f\n",
               b, complete[b]+0, n[b], (rounds_sum[b]+0)/n[b]
}' "$CSV" | sort
echo
echo "CSV: $CSV"
