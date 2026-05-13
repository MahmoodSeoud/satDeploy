#!/usr/bin/env bash
# run-container-build.sh — verify the loss_filter iface-level hook is
# wired correctly. Runs inside the satdeploy-dev container where GNU ld
# is available (Apple's stock ld doesn't support --wrap).
#
# What this script asserts:
#   1. Flight build (-Dtest_loss_filter=false, the default) links without
#      any wrap symbols. Byte-identical-equivalent to upstream libcsp use.
#   2. Test build (-Dtest_loss_filter=true) links with --wrap=csp_qfifo_write
#      and exposes both __wrap_csp_qfifo_write and __real_csp_qfifo_write.
#   3. experiments/test_loss_filter_actions.sh passes all 12 cases here
#      (the wrap_mechanism + wrap_pass_through tests, which skipped on
#      macOS due to Apple ld, run cleanly here).
#
# Exits non-zero on any failure.
#
# Usage (from host):
#   ./scripts/docker-dev.sh ./scripts/run-container-build.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo ">>> Verifying GNU ld supports --wrap"
if ! ld --help 2>&1 | grep -q -- "--wrap"; then
    echo "ERROR: linker does not advertise --wrap support" >&2
    ld --help 2>&1 | head -3 >&2
    exit 1
fi
echo "    GNU ld with --wrap support: ok"

# -------------------------------------------------------------------------
echo
echo ">>> Building FLIGHT variant (-Dtest_loss_filter=false, the default)"
rm -rf satdeploy-agent/build-flight
meson setup satdeploy-agent/build-flight satdeploy-agent
ninja -C satdeploy-agent/build-flight

FLIGHT_BIN=satdeploy-agent/build-flight/satdeploy-agent
[ -x "$FLIGHT_BIN" ] || { echo "ERROR: flight binary not built"; exit 1; }
echo "    built: $FLIGHT_BIN ($(stat -c %s "$FLIGHT_BIN") bytes)"

# Assert no wrap symbols in flight. The final binary is stripped
# (-Wl,-s in link_args), so check the per-object outputs that meson
# leaves under build-flight/satdeploy-agent.p/.
FLIGHT_OBJS=satdeploy-agent/build-flight/satdeploy-agent.p
echo ">>> Flight build: confirming iface hook is NOT compiled in"
if ls "$FLIGHT_OBJS"/src_loss_filter_iface_hook.c.o 2>/dev/null; then
    echo "ERROR: flight build included loss_filter_iface_hook.o" >&2
    exit 1
fi
echo "    iface hook absent from flight build: ok"

# -------------------------------------------------------------------------
echo
echo ">>> Building TEST variant (-Dtest_loss_filter=true)"
rm -rf satdeploy-agent/build-loss
meson setup satdeploy-agent/build-loss satdeploy-agent \
    -Dtest_loss_filter=true
ninja -C satdeploy-agent/build-loss

TEST_BIN=satdeploy-agent/build-loss/satdeploy-agent
[ -x "$TEST_BIN" ] || { echo "ERROR: test binary not built"; exit 1; }
echo "    built: $TEST_BIN ($(stat -c %s "$TEST_BIN") bytes)"

echo ">>> Test build: confirming wrap is wired at the object level"
TEST_OBJS=satdeploy-agent/build-loss/satdeploy-agent.p
HOOK_OBJ="$TEST_OBJS/src_loss_filter_iface_hook.c.o"
[ -f "$HOOK_OBJ" ] || { echo "ERROR: iface hook object missing"; exit 1; }
echo "    iface hook compiled into test build: ok"
echo "    symbols in $HOOK_OBJ:"
nm "$HOOK_OBJ" | grep csp_qfifo_write | sed 's/^/      /'
if ! nm "$HOOK_OBJ" | grep -q " T __wrap_csp_qfifo_write"; then
    echo "ERROR: __wrap_csp_qfifo_write not defined in hook object" >&2
    exit 1
fi
if ! nm "$HOOK_OBJ" | grep -q " U __real_csp_qfifo_write"; then
    echo "ERROR: __real_csp_qfifo_write not referenced from hook object" >&2
    exit 1
fi
echo "    __wrap defined, __real referenced: ok"
# Confirm the --wrap link arg was actually passed to the linker by inspecting
# the meson-introspected link line.
if ! grep -q "wrap=csp_qfifo_write" \
       satdeploy-agent/build-loss/meson-logs/meson-log.txt 2>/dev/null \
    && ! grep -rq "wrap=csp_qfifo_write" \
       satdeploy-agent/build-loss/build.ninja 2>/dev/null; then
    echo "ERROR: -Wl,--wrap=csp_qfifo_write not found in build config" >&2
    exit 1
fi
echo "    -Wl,--wrap=csp_qfifo_write reached the linker: ok"

# -------------------------------------------------------------------------
echo
echo ">>> Running experiment test harness (all 12 cases incl. wrap)"
bash experiments/test_loss_filter_actions.sh

echo
echo ">>> Running Python parser tests"
python3 experiments/lib/test_parse_pass_log.py

# -------------------------------------------------------------------------
echo
echo "========================================"
echo "ALL CONTAINER VERIFICATION CHECKS PASSED"
echo "========================================"
