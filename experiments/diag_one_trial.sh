#!/usr/bin/env bash
# diag_one_trial.sh — diagnostic: run one smart-loss trial at 5% with a long
# timeout, preserve the agent log, see whether the previous timeouts were
# real wedge or just budget too tight.
set -euo pipefail

REPO=/satdeploy
LOG_DIR=/tmp/diag-one-trial
TARGET_DIR=/tmp/diag-one-trial-target
NODE=5425
SIZE=1048576

SMART_AGENT="$REPO/satdeploy-agent/build-loss/satdeploy-agent"
APM_SO="$REPO/satdeploy-apm/build/libcsh_satdeploy_apm.so"

mkdir -p "$LOG_DIR" "$TARGET_DIR" /var/lib/satdeploy/state
mkdir -p ~/.local/lib/csh && cp -f "$APM_SO" ~/.local/lib/csh/

mkdir -p ~/.satdeploy
cat > ~/.satdeploy/config.yaml <<EOF
name: diag
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

cat > "$LOG_DIR/loss-5.pattern" <<EOF
0.000 prob 0.0500
EOF

rm -rf /var/lib/satdeploy/state/* "$TARGET_DIR/hello.bin" "$TARGET_DIR/backups" 2>/dev/null || true
pkill -x satdeploy-agent 2>/dev/null || true
pkill -x zmqproxy 2>/dev/null || true
sleep 0.3

dd if=/dev/urandom of="$LOG_DIR/hello.bin" bs=1 count="$SIZE" status=none

zmqproxy >/dev/null 2>&1 &
ZMQ_PID=$!
sleep 0.5

LOSS_PATTERN_FILE="$LOG_DIR/loss-5.pattern" \
    "$SMART_AGENT" -i ZMQ -p localhost -a "$NODE" >"$LOG_DIR/agent.log" 2>&1 &
AGENT_PID=$!
sleep 1

echo "=== START $(date +%T) ==="
START=$(date +%s)
RC=0
timeout --signal=KILL 600 \
    script -qfec "csh -i /satdeploy/init/zmq.csh \"satdeploy push hello -n $NODE\"" \
    /dev/null </dev/null \
    >"$LOG_DIR/push.log" 2>&1 \
    || RC=$?
END=$(date +%s)
echo "=== END $(date +%T) wall=$((END-START))s rc=$RC ==="

kill -TERM "$AGENT_PID" 2>/dev/null || true
wait "$AGENT_PID" 2>/dev/null || true
kill "$ZMQ_PID" 2>/dev/null || true

echo
echo "--- agent log tail ---"
tail -25 "$LOG_DIR/agent.log"
echo
echo "--- complete/incomplete/dropped lines ---"
grep -E "complete|incomplete|dropped" "$LOG_DIR/agent.log" | head -20
echo
echo "--- retry round count ---"
grep -cE "round [0-9]+: " "$LOG_DIR/agent.log" || true
