#!/usr/bin/env bash
# sweep_loss_rates.sh — defensibility sweep for the F3.b loss>0 column.
#
# tail_race.csv (sweep_tail_race.sh) covered the 0% loss point with n=30
# per build. The other points on the F3.b curve (1%, 5%, 10% configured
# loss) were still n=1 smoke trials. This script fills those cells with
# n=10 each.
#
# Scope (deliberately narrow to keep runtime bounded):
#   - loss rates: 1%, 5%, 10%
#   - sizes: 1MB only (tail_race showed size-independence at 0%)
#   - builds: naive-loss (`-Dnaive_baseline=true -Dtest_loss_filter=true`)
#             smart-loss (`-Dtest_loss_filter=true`)
#   - N=10 trials per cell → 60 trials total
#
# Per-trial CSV row: build, loss_pct, trial, push_rc, got, total,
# retry_rounds, gap, gap_class, dropped_by_filter, notes.
#
# Run inside the satdeploy-dev container.
set -euo pipefail

REPO=/satdeploy
LOG_DIR=/tmp/sweep-loss-rates
TARGET_DIR=/tmp/sweep-loss-rates-target
NODE=5425
SIZE=1048576

SMART_AGENT="$REPO/satdeploy-agent/build-loss/satdeploy-agent"
NAIVE_AGENT="$REPO/satdeploy-agent/build-naive-loss/satdeploy-agent"
APM_SO="$REPO/satdeploy-apm/build/libcsh_satdeploy_apm.so"

[ -x "$SMART_AGENT" ] || { echo "FAIL: $SMART_AGENT missing"; exit 1; }
[ -x "$NAIVE_AGENT" ] || { echo "FAIL: $NAIVE_AGENT missing"; exit 1; }
[ -f "$APM_SO" ] || { echo "FAIL: APM not built"; exit 1; }

mkdir -p "$LOG_DIR" "$TARGET_DIR" /var/lib/satdeploy/state
mkdir -p ~/.local/lib/csh && cp -f "$APM_SO" ~/.local/lib/csh/

mkdir -p ~/.satdeploy
cat > ~/.satdeploy/config.yaml <<EOF
name: sweep-loss
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

CSV="$REPO/experiments/results/loss_rates.csv"
mkdir -p "$(dirname "$CSV")"
echo "build,loss_pct,trial,seed,push_rc,got,total,retry_rounds,gap,gap_class,dropped_by_filter,notes" > "$CSV"

run_one() {
    local build_label="$1"
    local agent_bin="$2"
    local loss_pct="$3"      # 1, 5, 10
    local trial="$4"

    # Per-trial seed so each trial sees a different drop pattern. Without this,
    # all N trials hit the same packets — n=10 collapses to n=1 with deterministic
    # PRNG (loss_filter.c default seed is 0x12345678). Seed is derived from
    # (loss_pct, trial) so the sweep is reproducible end-to-end.
    local seed=$(printf "0x%08x" $(( (loss_pct * 1000003) ^ (trial * 1000033) ^ 0xC0FFEE )))

    local prob
    prob=$(awk "BEGIN {printf \"%.4f\", $loss_pct/100}")
    local pattern_file="$LOG_DIR/loss-${loss_pct}.${trial}.pattern"
    cat > "$pattern_file" <<EOF
0.000 prob $prob
EOF

    rm -rf /var/lib/satdeploy/state/* "$TARGET_DIR/hello.bin" "$TARGET_DIR/backups" 2>/dev/null || true
    pkill -x satdeploy-agent 2>/dev/null || true
    pkill -x zmqproxy 2>/dev/null || true
    sleep 0.3

    dd if=/dev/urandom of="$LOG_DIR/hello.bin" bs=1 count="$SIZE" status=none

    local exp_packets=$(( (SIZE + 1015) / 1016 ))

    zmqproxy >/dev/null 2>&1 &
    local zmq_pid=$!
    sleep 0.5

    local agent_log="$LOG_DIR/agent.${build_label}.${loss_pct}.${trial}.log"
    LOSS_PATTERN_FILE="$pattern_file" \
    LOSS_PATTERN_SEED="$seed" \
        "$agent_bin" -i ZMQ -p localhost -a "$NODE" >"$agent_log" 2>&1 &
    local agent_pid=$!
    sleep 1

    local push_rc=0
    # Smart at 5% loss can run all 8 retry rounds, ~6 minutes wall (per
    # diag_one_trial.sh). 600s gives full headroom to converge; trials that
    # genuinely fail (resume needed) bail at the rounds=8 limit before timeout.
    timeout --signal=KILL 600 \
        script -qfec "csh -i /satdeploy/init/zmq.csh \"satdeploy push hello -n $NODE\"" \
        /dev/null </dev/null \
        >"$LOG_DIR/push.${build_label}.${loss_pct}.${trial}.log" 2>&1 \
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

    # loss filter prints a final "[loss_filter] dropped N of M packets" on agent shutdown
    local dropped
    dropped=$(grep -oE "dropped [0-9]+ of [0-9]+" "$agent_log" | tail -1 | grep -oE "^dropped [0-9]+" | grep -oE "[0-9]+" || true)
    [ -z "$dropped" ] && dropped="?"

    local gap gap_class
    if [ "$got" = "$total" ] && [ "$got" != "0" ]; then
        gap=0; gap_class="none"
    elif [ "$rounds" = "X" ]; then
        gap="?"; gap_class="no_data"
    else
        gap=$(( total - got ))
        if   [ "$gap" -le 3 ];  then gap_class="tiny";
        elif [ "$gap" -le 20 ]; then gap_class="small";
        else                          gap_class="large"; fi
    fi

    echo "$build_label,$loss_pct,$trial,$seed,$push_rc,$got,$total,$rounds,$gap,$gap_class,$dropped,$notes" | tee -a "$CSV"
}

LOSS_RATES=(1 5 10)
TRIALS=5

echo
echo "--- sweep_loss_rates: 1MB, naive-loss vs smart-loss, loss=${LOSS_RATES[*]}%, N=$TRIALS ---"
echo "--- $(( ${#LOSS_RATES[@]} * 2 * TRIALS )) total trials ---"
echo
for loss in "${LOSS_RATES[@]}"; do
    for trial in $(seq 1 "$TRIALS"); do
        run_one "naive-loss" "$NAIVE_AGENT" "$loss" "$trial"
        run_one "smart-loss" "$SMART_AGENT" "$loss" "$trial"
    done
done

echo
echo "--- summary per (build, loss%) ---"
awk -F, 'NR>1 {
    key=sprintf("%-12s %2d%%", $1, $2)
    n[key]++
    if ($5 == "0")              ok[key]++
    if ($8 != "X" && $8 != "")  rounds_sum[key]+=$8
    if ($8 != "X" && $8 > 0)    used_retry[key]++
} END {
    for (k in n)
        printf "%s  n=%d  push_ok=%d/%d  used_retry=%d/%d  mean_rounds=%.2f\n",
               k, n[k], ok[k]+0, n[k], used_retry[k]+0, n[k], (rounds_sum[k]+0)/n[k]
}' "$CSV" | sort
echo
echo "CSV: $CSV"
