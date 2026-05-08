#!/usr/bin/env bash
#
# sweep.sh — bare libdtp tail-race falsification sweep.
#
# Mirrors the methodology of experiments/sweep_tail_race.sh, but with the
# bare-dtp-server / bare-dtp-client binaries instead of the full satdeploy
# agent + APM stack. The only thing being exercised is libdtp itself, over
# ZMQ loopback at 0% induced loss.
#
# What we're falsifying: the claim in CLAUDE.md that one dtp_start_transfer
# call frequently fails to deliver the full payload at 0% link loss because
# of libdtp's receive-loop termination behavior.
#
# If the bare client also fails 28-29 of 30 trials with a 1-2 packet tail
# gap, the behaviour is in libdtp. If it succeeds 30/30, satdeploy's
# wrapping is the cause and the "tail race" framing in CLAUDE.md needs
# revision.
#
# Output: experiments/libdtp-bare-test/results/bare_tail_race.csv
#   columns: size_bytes,trial,push_rc,got,expected,gap,sha256_match,
#            first_gap_seq,last_gap_seq,duration_ms
#
# Run from inside the satdeploy-dev container, after:
#   git submodule update --init --recursive
#   cd experiments/libdtp-bare-test && meson setup build && ninja -C build
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${BARE_BUILD_DIR:-$HERE/build}"
SERVER_BIN="$BUILD_DIR/bare-dtp-server"
CLIENT_BIN="$BUILD_DIR/bare-dtp-client"

[ -x "$SERVER_BIN" ] || { echo "FAIL: $SERVER_BIN not built — run 'ninja -C build' first" >&2; exit 1; }
[ -x "$CLIENT_BIN" ] || { echo "FAIL: $CLIENT_BIN not built — run 'ninja -C build' first" >&2; exit 1; }

# Use our minimal in-tree proxy. The libcsp-bundled examples/zmqproxy has a
# heap-corruption bug in its capture thread (see src/mini_zmqproxy.c comment).
ZMQPROXY="${ZMQPROXY:-$BUILD_DIR/mini-zmqproxy}"
[ -x "$ZMQPROXY" ] || {
  echo "FAIL: $ZMQPROXY not built — run 'ninja -C build mini-zmqproxy'" >&2
  exit 1
}
command -v sha256sum >/dev/null || { echo "FAIL: sha256sum not in PATH" >&2; exit 1; }

TRIALS="${TRIALS:-30}"
SIZES_DEFAULT="262144 1048576 4194304"   # 256K, 1M, 4M — same cells as tail_race.csv
read -r -a SIZES <<<"${SIZES:-$SIZES_DEFAULT}"

SERVER_NODE="${SERVER_NODE:-4040}"
CLIENT_NODE="${CLIENT_NODE:-5425}"
PAYLOAD_ID="${PAYLOAD_ID:-7}"

WORK_DIR="$(mktemp -d -t bare-dtp-sweep.XXXXXX)"
trap 'rm -rf "$WORK_DIR"; pkill -P $$ 2>/dev/null || true' EXIT

OUT_CSV="$HERE/results/bare_tail_race.csv"
mkdir -p "$(dirname "$OUT_CSV")"
echo "size_bytes,trial,push_rc,got,expected,gap,sha256_match,first_gap_seq,last_gap_seq,duration_ms" > "$OUT_CSV"

echo "bare-dtp sweep: trials=$TRIALS sizes=(${SIZES[*]}) -> $OUT_CSV" >&2

run_trial() {
	local size="$1" trial="$2"
	local payload="$WORK_DIR/payload.bin"
	local out="$WORK_DIR/recv.bin"

	# Best-effort cleanup of any residue from the previous trial.
	pkill -x bare-dtp-server 2>/dev/null || true
	pkill -x zmqproxy        2>/dev/null || true
	rm -f "$payload" "$out"
	sleep 0.2

	dd if=/dev/urandom of="$payload" bs="$size" count=1 status=none
	local src_hash
	src_hash="$(sha256sum "$payload" | awk '{print $1}')"

	"$ZMQPROXY" >/dev/null 2>&1 &
	local zmq_pid=$!
	sleep 0.4

	"$SERVER_BIN" "$SERVER_NODE" "$PAYLOAD_ID" "$payload" \
		>"$WORK_DIR/server.log" 2>&1 &
	local srv_pid=$!
	sleep 0.5

	local client_json client_rc=0
	# 90s budget mirrors sweep_tail_race.sh — generous enough for 4MB at default
	# throughput, tight enough that a wedged session shows up as a timeout.
	BARE_CLIENT_NODE="$CLIENT_NODE" \
	client_json="$(timeout --signal=KILL 90 \
		"$CLIENT_BIN" "$SERVER_NODE" "$PAYLOAD_ID" "$size" "$out" \
		2>>"$WORK_DIR/client.log")" || client_rc=$?

	kill -TERM "$srv_pid" 2>/dev/null || true
	wait "$srv_pid" 2>/dev/null || true
	kill "$zmq_pid" 2>/dev/null || true
	wait "$zmq_pid" 2>/dev/null || true

	# Parse the single-line JSON the client emitted. Keep it shell-only —
	# no jq dependency in the experiment harness.
	local rc got expected gap dur first last hash
	if [ -n "${client_json:-}" ]; then
		rc=$(printf '%s' "$client_json" | sed -n 's/.*"rc":\([-0-9]*\).*/\1/p')
		got=$(printf '%s' "$client_json" | sed -n 's/.*"got":\([0-9]*\).*/\1/p')
		expected=$(printf '%s' "$client_json" | sed -n 's/.*"expected":\([0-9]*\).*/\1/p')
		gap=$(printf '%s' "$client_json" | sed -n 's/.*"gap":\([0-9]*\).*/\1/p')
		dur=$(printf '%s' "$client_json" | sed -n 's/.*"duration_ms":\([0-9]*\).*/\1/p')
		first=$(printf '%s' "$client_json" | sed -n 's/.*"first_gap_seq":\(-\{0,1\}[0-9]*\).*/\1/p')
		last=$(printf '%s' "$client_json" | sed -n 's/.*"last_gap_seq":\(-\{0,1\}[0-9]*\).*/\1/p')
		hash=$(printf '%s' "$client_json" | sed -n 's/.*"sha256":"\([0-9a-f]*\)".*/\1/p')
	else
		rc="$client_rc"; got=0; expected=0; gap=0; dur=0; first=-1; last=-1; hash=""
	fi

	local sha_match=0
	[ -n "$hash" ] && [ "$hash" = "$src_hash" ] && sha_match=1

	echo "$size,$trial,$rc,${got:-0},${expected:-0},${gap:-0},$sha_match,${first:--1},${last:--1},${dur:-0}" \
		| tee -a "$OUT_CSV" >/dev/null
	printf "  size=%d trial=%d rc=%s got=%s/%s gap=%s sha=%d\n" \
		"$size" "$trial" "${rc:-?}" "${got:-?}" "${expected:-?}" "${gap:-?}" "$sha_match" >&2
}

for size in "${SIZES[@]}"; do
	for trial in $(seq 1 "$TRIALS"); do
		run_trial "$size" "$trial"
	done
done

echo "Done. CSV: $OUT_CSV" >&2
