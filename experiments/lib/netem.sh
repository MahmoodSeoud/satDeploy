#!/usr/bin/env bash
# tc qdisc / netem helpers for the experiment harness.
#
# All rules are applied to `lo` because the dev container's CSP transport
# (zmqproxy) terminates on localhost. The harness only ever adds a single
# root qdisc and removes it at end of trial, so concurrent harness runs in
# the same container would collide — the harness traps EXIT to clear.
#
# CAVEAT for thesis: zmqproxy uses TCP. tc netem will drop packets on lo,
# but TCP retransmits absorb the loss before DTP sees it. For real link-
# loss experiments (E2 / E3), use a UDP CSP transport (see
# experiments/README.md) or escalate to flatsat. This file is still useful
# for E6 crash-matrix scenarios that combine kill-with-loss.

set -euo pipefail

NETEM_IFACE="${NETEM_IFACE:-lo}"

netem_clear() {
    # `tc qdisc del root` errors when no qdisc exists. Suppress — we want
    # idempotent cleanup. Trying to detect "is there a qdisc" via `tc qdisc
    # show` is more brittle than just swallowing the expected ENOENT.
    tc qdisc del dev "$NETEM_IFACE" root 2>/dev/null || true
}

# netem_apply <loss_pct> [burst_corr_pct] [delay_ms] [jitter_ms] [rate_bps]
# Examples:
#   netem_apply 0                        # passthrough (just removes any rule)
#   netem_apply 10                       # 10% Bernoulli loss
#   netem_apply 25 50                    # 25% loss with 50% correlation (burst)
#   netem_apply 5 0 50 10                # 5% loss + 50ms ±10ms latency
#   netem_apply 5 0 50 10 9600           # ...plus 9600 bps cap (UHF target)
netem_apply() {
    local loss_pct="${1:-0}"
    local burst_corr_pct="${2:-0}"
    local delay_ms="${3:-0}"
    local jitter_ms="${4:-0}"
    local rate_bps="${5:-0}"

    netem_clear

    if [ "$loss_pct" = "0" ] && [ "$delay_ms" = "0" ] && [ "$rate_bps" = "0" ]; then
        return 0  # nothing to do, leave lo unconfigured
    fi

    local netem_args=()
    if [ "$loss_pct" != "0" ]; then
        if [ "$burst_corr_pct" != "0" ]; then
            netem_args+=("loss" "${loss_pct}%" "${burst_corr_pct}%")
        else
            netem_args+=("loss" "${loss_pct}%")
        fi
    fi
    if [ "$delay_ms" != "0" ]; then
        netem_args+=("delay" "${delay_ms}ms" "${jitter_ms}ms")
    fi

    # Rate limiting: tbf parent, netem child. tbf `burst` and `latency` are
    # tuned for low-bandwidth tests; if you push these much higher than the
    # 9600 bps target the queue fills before netem fires.
    if [ "$rate_bps" != "0" ]; then
        tc qdisc add dev "$NETEM_IFACE" root handle 1: tbf \
            rate "${rate_bps}bit" burst 1500 latency 200ms
        if [ "${#netem_args[@]}" -gt 0 ]; then
            tc qdisc add dev "$NETEM_IFACE" parent 1: handle 10: netem "${netem_args[@]}"
        fi
    else
        tc qdisc add dev "$NETEM_IFACE" root netem "${netem_args[@]}"
    fi
}

netem_show() {
    tc qdisc show dev "$NETEM_IFACE"
}
