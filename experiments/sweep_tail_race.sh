#!/usr/bin/env bash
# sweep_tail_race.sh — N=10 sweep replacing the n=1 smoke + n=6 probe data
# that the F3.b "naive baseline cannot complete transfers" claim was resting on.
#
# Tests one specific claim: at 0% induced loss on ZMQ loopback, does the naive
# build (DTP_MAX_RETRY_ROUNDS=0, no resume) fail to complete transfers because
# of the libdtp tail-race at lib/dtp/src/dtp_client.c:284?
#
# Design:
#   - 3 sizes (256KB, 1MB, 4MB) × 2 builds (naive, smart) × N=10 trials = 60 runs
#   - Always-up loss pattern (smart build); naive has no loss filter compiled in
#   - Each trial: fresh random payload, fresh state dir, fresh zmqproxy, fresh agent
#   - Per trial we record: build, size, push_rc, packets_received/expected,
#     retry_rounds, gap_size (= total - got), gap_class (none/small/large)
#
# The CSV is the source of truth for any "naive vs smart" claim in CLAUDE.md
# or the F3.b figure. n=1 smoke trials are not.
#
# Run inside the satdeploy-dev container.
set -euo pipefail

REPO=/satdeploy
LOG_DIR=/tmp/sweep-tail-race
TARGET_DIR=/tmp/sweep-tail-race-target
NODE=5425

SMART_AGENT="$REPO/satdeploy-agent/build-loss/satdeploy-agent"
NAIVE_AGENT="$REPO/satdeploy-agent/build-naive/satdeploy-agent"
APM_SO="$REPO/satdeploy-apm/build/libcsh_satdeploy_apm.so"

[ -x "$SMART_AGENT" ] || { echo "FAIL: $SMART_AGENT missing — meson setup build-loss -Dtest_loss_filter=true && ninja -C build-loss"; exit 1; }
[ -x "$NAIVE_AGENT" ] || { echo "FAIL: $NAIVE_AGENT missing — meson setup build-naive -Dnaive_baseline=true && ninja -C build-naive"; exit 1; }
[ -f "$APM_SO" ] || { echo "FAIL: APM not built"; exit 1; }

mkdir -p "$LOG_DIR" "$TARGET_DIR" /var/lib/satdeploy/state
mkdir -p ~/.local/lib/csh && cp -f "$APM_SO" ~/.local/lib/csh/

mkdir -p ~/.satdeploy
cat > ~/.satdeploy/config.yaml <<EOF
name: sweep
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

# Always-up pattern — only the smart build's loss filter reads it; naive
# was compiled without -Dtest_loss_filter so it ignores LOSS_PATTERN_FILE entirely.
cat > "$LOG_DIR/up.pattern" <<EOF
0.000 up
EOF

CSV="$REPO/experiments/results/tail_race.csv"
mkdir -p "$(dirname "$CSV")"
echo "build,size_bytes,packets_expected,trial,push_rc,got,total,retry_rounds,gap,gap_class,notes" > "$CSV"

run_one() {
    local build_label="$1"
    local agent_bin="$2"
    local size_bytes="$3"
    local trial="$4"

    rm -rf /var/lib/satdeploy/state/* "$TARGET_DIR/hello.bin" "$TARGET_DIR/backups" 2>/dev/null || true
    pkill -x satdeploy-agent 2>/dev/null || true
    pkill -x zmqproxy 2>/dev/null || true
    sleep 0.3

    # Fresh random payload — keeps any data-dependent pathology from biasing
    # one cell of the matrix.
    dd if=/dev/urandom of="$LOG_DIR/hello.bin" bs=1 count="$size_bytes" status=none

    local exp_packets=$(( (size_bytes + 1015) / 1016 ))   # mtu=1024 minus 8-byte DTP header

    zmqproxy >/dev/null 2>&1 &
    local zmq_pid=$!
    sleep 0.5

    local agent_log="$LOG_DIR/agent.${build_label}.${size_bytes}.${trial}.log"
    LOSS_PATTERN_FILE="$LOG_DIR/up.pattern" \
        "$agent_bin" -i ZMQ -p localhost -a "$NODE" >"$agent_log" 2>&1 &
    local agent_pid=$!
    sleep 1

    local push_rc=0
    timeout --signal=KILL 90 \
        script -qfec "csh -i /satdeploy/init/zmq.csh \"satdeploy push hello -n $NODE\"" \
        /dev/null </dev/null \
        >"$LOG_DIR/push.${build_label}.${size_bytes}.${trial}.log" 2>&1 \
        || push_rc=$?

    kill -TERM "$agent_pid" 2>/dev/null || true
    wait "$agent_pid" 2>/dev/null || true
    kill "$zmq_pid" 2>/dev/null || true

    # Parse the agent log:
    #   success: "[dtp]    complete (G packets, R retry round(s)[, resumed])"
    #   failure: "[dtp]    error: incomplete after R retry round(s) (G/N packets) ..."
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

    local gap gap_class
    if [ "$got" = "$total" ] && [ "$got" != "0" ]; then
        gap=0; gap_class="none"
    elif [ "$rounds" = "X" ]; then
        gap="?"; gap_class="no_data"
    else
        gap=$(( total - got ))
        if   [ "$gap" -le 3 ];  then gap_class="tiny";    # consistent with libdtp tail-race
        elif [ "$gap" -le 20 ]; then gap_class="small";
        else                          gap_class="large"; fi
    fi

    echo "$build_label,$size_bytes,$exp_packets,$trial,$push_rc,$got,$total,$rounds,$gap,$gap_class,$notes" | tee -a "$CSV"
}

SIZES=(262144 1048576 4194304)
TRIALS=10

echo
echo "--- sweep_tail_race: naive vs smart at 0% loss, N=$TRIALS per cell ---"
echo "--- $(( ${#SIZES[@]} * 2 * TRIALS )) total trials ---"
echo
for size in "${SIZES[@]}"; do
    for trial in $(seq 1 "$TRIALS"); do
        run_one "naive" "$NAIVE_AGENT" "$size" "$trial"
        run_one "smart" "$SMART_AGENT" "$size" "$trial"
    done
done

echo
echo "--- summary per (build, size) ---"
awk -F, 'NR>1 {
    key=sprintf("%-6s %7d", $1, $2)
    n[key]++
    if ($5 == "0")              ok[key]++
    if ($8 != "X" && $8 != "")  rounds_sum[key]+=$8
    if ($8 != "X" && $8 > 0)    used_retry[key]++
    if ($10 == "tiny")          tiny[key]++
    if ($10 == "none")          full[key]++
} END {
    for (k in n)
        printf "%s  n=%d  push_ok=%d  full_first_pass=%d  used_retry=%d  tail_gap_1-3=%d  mean_rounds=%.2f\n",
               k, n[k], ok[k]+0, full[k]+0, used_retry[k]+0, tiny[k]+0, (rounds_sum[k]+0)/n[k]
}' "$CSV" | sort
echo
echo "CSV: $CSV"
