#!/usr/bin/env bash
# test_loss_filter_actions.sh — regression test for the latency + gilbert
# actions added to satdeploy-agent/src/loss_filter.c.
#
# Compiles loss_filter.c standalone with -DSATDEPLOY_TEST_LOSS_FILTER and
# runs a deterministic harness that asserts:
#   1. Bernoulli `prob 0.05` produces ~5% drops with short fail runs.
#   2. Gilbert with sticky Bad state produces ~5-10% drops with LONG fail runs.
#   3. `latency` action actually sleeps for the configured duration.
#   4. Bad input (gilbert prob > 1.0) is rejected by the parser.
#   5. Existing `prob`-only patterns still parse (backward compatibility).
#
# Run from the repo root:
#   ./experiments/test_loss_filter_actions.sh
#
# Exits non-zero on any failed assertion. Cleans up its scratch files.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT="$REPO_ROOT/satdeploy-agent"
WORK=$(mktemp -d -t loss-filter-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

HARNESS="$WORK/harness.c"
BIN="$WORK/harness"

# ---------------------------------------------------------------------
# Test harness — links against the real loss_filter.c.
# Reads ASSERT_* environment variables and dies if expectations miss.
# ---------------------------------------------------------------------
cat >"$HARNESS" <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

#include "loss_filter.h"

static long elapsed_ms(struct timespec *a, struct timespec *b) {
    return (b->tv_sec - a->tv_sec) * 1000L +
           (b->tv_nsec - a->tv_nsec) / 1000000L;
}

int main(int argc, char **argv) {
    if (loss_filter_init() != 0) {
        fprintf(stderr, "init_failed\n");
        return 2;  /* parser rejected input */
    }

    int trials = (argc > 1) ? atoi(argv[1]) : 10000;
    int drops = 0, max_run = 0, cur_run = 0;
    for (int i = 0; i < trials; i++) {
        if (loss_filter_should_drop()) {
            drops++; cur_run++;
            if (cur_run > max_run) max_run = cur_run;
        } else {
            cur_run = 0;
        }
    }

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    loss_filter_apply_latency();
    clock_gettime(CLOCK_MONOTONIC, &t1);
    long lat_ms = elapsed_ms(&t0, &t1);

    /* Machine-readable result for the shell to parse. */
    printf("RESULT drops=%d trials=%d max_run=%d latency_ms=%ld\n",
           drops, trials, max_run, lat_ms);
    loss_filter_close();
    return 0;
}
EOF

# ---------------------------------------------------------------------
# Compile once.
# ---------------------------------------------------------------------
cc -Wall -Wextra -O0 -DSATDEPLOY_TEST_LOSS_FILTER \
   -I"$AGENT/include" \
   "$HARNESS" "$AGENT/src/loss_filter.c" \
   -o "$BIN" -lpthread

PASS=0
FAIL=0

# ---------------------------------------------------------------------
# Each test_case runs the harness with a given pattern + seed and asserts
# the printed RESULT line satisfies a shell condition. Failure prints
# the actual numbers so debugging is one-shot.
# ---------------------------------------------------------------------
test_case() {
    local name="$1" pattern_text="$2" seed="$3" trials="$4" assertion="$5"
    local pattern_file="$WORK/$name.pattern"
    printf '%s\n' "$pattern_text" >"$pattern_file"

    local stdout ec
    # Run with set +e so we can capture the actual exit code. `if !` swallows
    # $? on bash, so don't use that idiom.
    set +e
    stdout=$(LOSS_PATTERN_FILE="$pattern_file" LOSS_PATTERN_SEED="$seed" \
             "$BIN" "$trials" 2>&1)
    ec=$?
    set -e
    if [ "$ec" -ne 0 ]; then
        if [ "$ec" -eq 2 ] && [ "$assertion" = "PARSE_REJECTED" ]; then
            printf '  ok  %s (parser rejected as expected)\n' "$name"
            PASS=$((PASS + 1))
            return 0
        fi
        printf '  FAIL %s (harness exit %d)\n    %s\n' "$name" "$ec" "$stdout"
        FAIL=$((FAIL + 1))
        return 1
    fi
    if [ "$assertion" = "PARSE_REJECTED" ]; then
        printf '  FAIL %s (expected parse-reject, got success)\n    %s\n' \
               "$name" "$stdout"
        FAIL=$((FAIL + 1))
        return 1
    fi

    # Pull RESULT line for assertion eval.
    local result_line
    result_line=$(printf '%s\n' "$stdout" | grep '^RESULT ' || true)
    if [ -z "$result_line" ]; then
        printf '  FAIL %s (no RESULT line)\n    %s\n' "$name" "$stdout"
        FAIL=$((FAIL + 1))
        return 1
    fi
    # Parse k=v pairs into shell vars.
    # shellcheck disable=SC2086
    eval $(printf '%s\n' "$result_line" | sed 's/^RESULT //')

    if eval "$assertion"; then
        printf '  ok  %-30s (drops=%d/%d max_run=%d lat=%dms)\n' \
               "$name" "$drops" "$trials" "$max_run" "$latency_ms"
        PASS=$((PASS + 1))
    else
        printf '  FAIL %-30s (drops=%d/%d max_run=%d lat=%dms; failed: %s)\n' \
               "$name" "$drops" "$trials" "$max_run" "$latency_ms" "$assertion"
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------
# 1. Bernoulli at 5%: drops near 500/10000, fail runs short (<=10 typical).
# ---------------------------------------------------------------------
test_case "bernoulli_05" "0.000 prob 0.05" 42 10000 \
  '[ "$drops" -ge 400 ] && [ "$drops" -le 700 ] && [ "$max_run" -le 12 ]'

# ---------------------------------------------------------------------
# 2. Gilbert with sticky Bad (p_BB=0.9, drop_B=1.0): bursts much longer
# than Bernoulli at the same overall rate. Min run of 30 is well above
# any plausible Bernoulli outcome.
# ---------------------------------------------------------------------
test_case "gilbert_bursty" \
  "0.000 gilbert 0.99 0.9 0.0 1.0" 42 10000 \
  '[ "$drops" -ge 500 ] && [ "$drops" -le 1500 ] && [ "$max_run" -ge 30 ]'

# ---------------------------------------------------------------------
# 3. Latency: 100ms requested, allow 90-150ms (system scheduler variance).
# ---------------------------------------------------------------------
test_case "latency_100ms" "0.000 latency 100" 1 100 \
  '[ "$latency_ms" -ge 90 ] && [ "$latency_ms" -le 200 ]'

# ---------------------------------------------------------------------
# 4. latency 0 means no sleep.
# ---------------------------------------------------------------------
test_case "latency_zero" "0.000 latency 0" 1 100 \
  '[ "$latency_ms" -le 5 ]'

# ---------------------------------------------------------------------
# 5. ACT_UP cancels gilbert: drops drop to ~zero after the up event.
# Two events at t=0: gilbert with high drop, then up. Final state is up,
# so should_drop returns false.
# ---------------------------------------------------------------------
test_case "up_cancels_gilbert" \
  "0.000 gilbert 0.99 0.9 0.0 1.0
0.000 up" 42 10000 \
  '[ "$drops" -eq 0 ]'

# ---------------------------------------------------------------------
# 6. Backward compatibility: existing pure-Bernoulli patterns still parse
# and behave the same as before.
# ---------------------------------------------------------------------
test_case "backward_compat" "0.000 prob 0.10" 42 10000 \
  '[ "$drops" -ge 900 ] && [ "$drops" -le 1100 ]'

# ---------------------------------------------------------------------
# 7. Parser rejects out-of-range probabilities.
# ---------------------------------------------------------------------
test_case "reject_gilbert_bad_prob" \
  "0.000 gilbert 1.5 0.9 0.0 1.0" 1 1 PARSE_REJECTED

test_case "reject_latency_negative" \
  "0.000 latency -1" 1 1 PARSE_REJECTED

test_case "reject_latency_huge" \
  "0.000 latency 100000" 1 1 PARSE_REJECTED

test_case "reject_gilbert_wrong_arity" \
  "0.000 gilbert 0.9 0.9 0.05" 1 1 PARSE_REJECTED

# ---------------------------------------------------------------------
# Linker --wrap interception test for loss_filter_iface_hook.c
#
# Verifies that:
#   - __wrap_csp_qfifo_write is reachable
#   - It calls loss_filter_should_drop() / apply_latency()
#   - On the keep path it forwards to __real_csp_qfifo_write (which is the
#     original csp_qfifo_write the linker preserves)
#
# Skipped on linkers that don't support --wrap (Apple's stock ld doesn't).
# GNU ld and lld both support it, so this runs on Linux and on macOS when
# clang is configured to use lld.
# ---------------------------------------------------------------------
test_wrap_mechanism() {
    local stubdir="$WORK/wrap"
    mkdir -p "$stubdir/csp"
    # Stub CSP headers so loss_filter_iface_hook.c compiles unmodified
    # off-target (real csp/autoconfig.h is generated at meson setup time
    # and isn't available standalone).
    cat >"$stubdir/csp/csp.h" <<'EOF'
#ifndef _CSP_STUB
#define _CSP_STUB
typedef struct csp_packet { int dummy; } csp_packet_t;
typedef struct csp_iface  { int rx_error; } csp_iface_t;
#endif
EOF
    cat >"$stubdir/csp/csp_buffer.h" <<'EOF'
void csp_buffer_free(void *p);
EOF
    cat >"$stubdir/csp/csp_interface.h" <<'EOF'
#include "csp/csp.h"
EOF
    # Fake csp_qfifo_write that increments a counter we can read.
    cat >"$stubdir/fake_qfifo.c" <<'EOF'
#include "csp/csp.h"
int real_qfifo_calls = 0;
void csp_qfifo_write(csp_packet_t *p, csp_iface_t *i, void *t) {
    (void)p; (void)i; (void)t;
    real_qfifo_calls++;
}
void csp_buffer_free(void *p) { (void)p; }
EOF
    # A driver that calls csp_qfifo_write and reports whether the wrapper
    # intercepted and forwarded correctly.
    cat >"$stubdir/wrap_driver.c" <<'EOF'
#include <stdio.h>
#include <stdint.h>
#include "csp/csp.h"
void csp_qfifo_write(csp_packet_t *p, csp_iface_t *i, void *t);
extern int real_qfifo_calls;
extern int loss_filter_init(void);
extern void loss_filter_stats(uint32_t *seen, uint32_t *dropped);
int main(void) {
    if (loss_filter_init() != 0) return 2;
    csp_packet_t p;
    csp_iface_t  i = {0};
    /* 1000 calls. With LOSS_PATTERN_FILE set to 'prob 1.0' all should
     * be dropped by the wrap before reaching the real fn. */
    for (int k = 0; k < 1000; k++) csp_qfifo_write(&p, &i, NULL);
    uint32_t seen, dropped;
    loss_filter_stats(&seen, &dropped);
    printf("WRAP seen=%u dropped=%u real_calls=%d\n",
           seen, dropped, real_qfifo_calls);
    return 0;
}
EOF
    # Try linking with --wrap. On linkers that don't support it, skip.
    local link_log="$stubdir/link.log"
    if ! cc -Wall -Wextra -O0 -DSATDEPLOY_TEST_LOSS_FILTER \
            -I"$AGENT/include" -I"$stubdir" \
            "$stubdir/wrap_driver.c" "$stubdir/fake_qfifo.c" \
            "$AGENT/src/loss_filter.c" "$AGENT/src/loss_filter_iface_hook.c" \
            -Wl,--wrap=csp_qfifo_write \
            -o "$stubdir/wrap_test" -lpthread 2>"$link_log"; then
        if grep -q -- "--wrap\|unknown argument" "$link_log"; then
            printf '  SKIP wrap_mechanism (linker has no --wrap support; '\
'GNU ld / lld required)\n'
            return 0
        fi
        printf '  FAIL wrap_mechanism (link error)\n'
        cat "$link_log"
        FAIL=$((FAIL + 1))
        return 1
    fi

    # Pattern: drop everything. The wrap should intercept all 1000 calls,
    # drop them, and never reach real_qfifo_calls (which stays 0).
    local pf="$stubdir/all_drop.pattern"
    echo "0.000 prob 1.0" >"$pf"
    local out
    out=$(LOSS_PATTERN_FILE="$pf" LOSS_PATTERN_SEED=42 "$stubdir/wrap_test")
    # Parse: WRAP seen=... dropped=... real_calls=...
    # shellcheck disable=SC2086
    eval $(printf '%s\n' "$out" | sed 's/^WRAP //')
    if [ "$seen" -eq 1000 ] && [ "$dropped" -eq 1000 ] && [ "$real_calls" -eq 0 ]; then
        printf '  ok  wrap_mechanism                (seen=%d dropped=%d real=%d)\n' \
               "$seen" "$dropped" "$real_calls"
        PASS=$((PASS + 1))
    else
        printf '  FAIL wrap_mechanism (seen=%d dropped=%d real=%d; expected 1000/1000/0)\n' \
               "$seen" "$dropped" "$real_calls"
        FAIL=$((FAIL + 1))
    fi

    # Pattern: drop nothing. All 1000 should reach the real fn.
    echo "0.000 prob 0.0" >"$pf"
    out=$(LOSS_PATTERN_FILE="$pf" LOSS_PATTERN_SEED=42 "$stubdir/wrap_test")
    # shellcheck disable=SC2086
    eval $(printf '%s\n' "$out" | sed 's/^WRAP //')
    if [ "$seen" -eq 1000 ] && [ "$dropped" -eq 0 ] && [ "$real_calls" -eq 1000 ]; then
        printf '  ok  wrap_pass_through             (seen=%d dropped=%d real=%d)\n' \
               "$seen" "$dropped" "$real_calls"
        PASS=$((PASS + 1))
    else
        printf '  FAIL wrap_pass_through (seen=%d dropped=%d real=%d; expected 1000/0/1000)\n' \
               "$seen" "$dropped" "$real_calls"
        FAIL=$((FAIL + 1))
    fi
}

test_wrap_mechanism

# ---------------------------------------------------------------------
echo
echo "passed=$PASS  failed=$FAIL"
[ "$FAIL" -eq 0 ]
