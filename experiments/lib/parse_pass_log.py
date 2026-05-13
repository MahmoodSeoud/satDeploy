#!/usr/bin/env python3
"""
parse_pass_log.py — convert real DISCO pass logs into loss-pattern files.

Status: STUB. The actual parsing logic depends on what format the lab's
operations team produces. This stub:
  - Documents the expected interface.
  - Implements the format-agnostic transformations (scale, shift, splice,
    validate) that work on already-parsed pattern files.
  - Has a clear `_parse_real_log()` extension point with TODO markers.

Once we know what the real logs look like (CSV from a logger, GreatViewer
output, raw CSP receive timestamps, JSON telemetry, ...), wire it into
`_parse_real_log` and the rest of this script just works.

Pattern format documented in experiments/loss-pattern-format.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Iterable


# --------------------------------------------------------------------------
# Pattern model
# --------------------------------------------------------------------------

@dataclasses.dataclass
class PatternEvent:
    """One line in a pattern file: a timestamp + an action."""
    t_offset_s: float          # seconds from pattern start
    action: str                # 'up' | 'down' | 'prob' | 'clear'
    prob: float | None = None  # for action='prob' only

    def render(self) -> str:
        if self.action == "prob":
            assert self.prob is not None
            return f"{self.t_offset_s:.3f}  prob {self.prob:.4f}"
        return f"{self.t_offset_s:.3f}  {self.action}"


@dataclasses.dataclass
class Pattern:
    """A list of events plus optional metadata header lines."""
    events: list[PatternEvent]
    metadata: dict[str, str] = dataclasses.field(default_factory=dict)
    header_comments: list[str] = dataclasses.field(default_factory=list)

    def render(self) -> str:
        lines: list[str] = []
        lines.extend(f"# {c}" for c in self.header_comments)
        for k, v in self.metadata.items():
            lines.append(f"# pass_meta:{k}={v}")
        lines.append("")
        for ev in self.events:
            lines.append(ev.render())
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Pattern file I/O
# --------------------------------------------------------------------------

def load_pattern(path: Path) -> Pattern:
    """Read a .pattern file and return a Pattern."""
    events: list[PatternEvent] = []
    metadata: dict[str, str] = {}
    header_comments: list[str] = []
    with path.open() as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                stripped = line.lstrip("#").strip()
                if stripped.startswith("pass_meta:"):
                    payload = stripped[len("pass_meta:"):].strip()
                    for chunk in payload.split():
                        if "=" in chunk:
                            k, v = chunk.split("=", 1)
                            metadata[k] = v
                else:
                    header_comments.append(stripped)
                continue
            parts = line.split()
            t = float(parts[0])
            action = parts[1]
            prob = float(parts[2]) if action == "prob" else None
            events.append(PatternEvent(t_offset_s=t, action=action, prob=prob))
    return Pattern(events=events, metadata=metadata,
                   header_comments=header_comments)


def write_pattern(path: Path, pat: Pattern) -> None:
    path.write_text(pat.render())


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(pat: Pattern) -> list[str]:
    """Return a list of error messages; empty list = valid."""
    errors: list[str] = []
    valid_actions = {"up", "down", "prob", "clear"}
    last_t = -1.0
    for i, ev in enumerate(pat.events):
        if ev.t_offset_s < 0:
            errors.append(f"event[{i}]: negative t_offset {ev.t_offset_s}")
        if ev.t_offset_s < last_t:
            errors.append(
                f"event[{i}]: timestamps not monotonic "
                f"({ev.t_offset_s} < previous {last_t})"
            )
        last_t = max(last_t, ev.t_offset_s)
        if ev.action not in valid_actions:
            errors.append(f"event[{i}]: unknown action '{ev.action}'")
        if ev.action == "prob":
            if ev.prob is None or not (0.0 <= ev.prob <= 1.0):
                errors.append(f"event[{i}]: prob must be in [0,1], got {ev.prob}")
    return errors


# --------------------------------------------------------------------------
# Transformations
# --------------------------------------------------------------------------

def scale(pat: Pattern, factor: float) -> Pattern:
    """
    Multiply drop intensity by `factor`.

    For up/down patterns: stretch each `down` interval by `factor`.
    For `prob` segments: multiply probability by `factor` (clamped to [0,1]).

    factor=0.5 -> half the drops; factor=2.0 -> twice the drops.
    """
    if factor < 0:
        raise ValueError("scale factor must be non-negative")
    out_events: list[PatternEvent] = []
    state = "up"
    interval_start = 0.0
    for ev in pat.events:
        if ev.action == "down":
            state = "down"
            interval_start = ev.t_offset_s
            out_events.append(PatternEvent(ev.t_offset_s, "down"))
        elif ev.action == "up" and state == "down":
            recorded_len = ev.t_offset_s - interval_start
            new_len = recorded_len * factor
            new_up_t = interval_start + new_len
            out_events.append(PatternEvent(new_up_t, "up"))
            state = "up"
        elif ev.action == "prob":
            new_prob = max(0.0, min(1.0, (ev.prob or 0.0) * factor))
            out_events.append(PatternEvent(ev.t_offset_s, "prob", new_prob))
        else:
            out_events.append(PatternEvent(ev.t_offset_s, ev.action, ev.prob))
    out_meta = dict(pat.metadata)
    out_meta["scaled_by"] = f"{factor}"
    return Pattern(events=out_events, metadata=out_meta,
                   header_comments=pat.header_comments + [
                       f"Derived: scaled by {factor}x"
                   ])


def shift(pat: Pattern, dt_s: float) -> Pattern:
    """Add `dt_s` to every timestamp. Negative values not supported."""
    if dt_s < 0:
        raise ValueError("negative shift not supported")
    out_events = [PatternEvent(ev.t_offset_s + dt_s, ev.action, ev.prob)
                  for ev in pat.events]
    out_meta = dict(pat.metadata)
    out_meta["shifted_by_s"] = f"{dt_s}"
    return Pattern(events=out_events, metadata=out_meta,
                   header_comments=pat.header_comments + [
                       f"Derived: shifted by {dt_s} s"
                   ])


def build_pass_window(pass_len_s: float, gap_s: float,
                      total_len_s: float) -> Pattern:
    """
    Build a synthetic pass-window pattern for F5.

    Yields: link up for pass_len_s, down for gap_s, up for pass_len_s, ...
    until total_len_s is exceeded.
    """
    events: list[PatternEvent] = []
    t = 0.0
    state = "up"
    events.append(PatternEvent(0.0, "up"))
    while t < total_len_s:
        if state == "up":
            t += pass_len_s
            events.append(PatternEvent(t, "down"))
            state = "down"
        else:
            t += gap_s
            events.append(PatternEvent(t, "up"))
            state = "up"
    return Pattern(
        events=events,
        metadata={
            "pattern_kind": "synthetic_pass_window",
            "pass_len_s": str(pass_len_s),
            "gap_s": str(gap_s),
        },
        header_comments=[
            f"Synthetic pass-window pattern: {pass_len_s}s up / {gap_s}s down",
        ],
    )


# --------------------------------------------------------------------------
# Real-log parsing — DISCO-2 bird tmux session captures
# --------------------------------------------------------------------------
#
# Input format: `csh -i ...` session captured via `script(1)` or tmux logging,
# with terminal escape codes intact. Operators run commands via the `drun`
# Doppler-runner wrapper, which prints these per attempt:
#
#     [drun] Range rate: -5.918 km/s, Doppler@100M: +1974.1 Hz
#     [drun] RX: 437083628 Hz (doppler: +8628.1), TX: 437066372 Hz (...)
#     [drun] Successfully set radio frequencies on node 4032
#     [drun] Executing: ping
#     Ping node 5386 size 0 timeout 5000: Reply in 1269 [ms]    <-- OK
#     -- or --
#     Ping node 5386 size 0 timeout 5000: No reply              <-- FAIL
#
# For non-ping commands the outcome line is either real output or
# `No response`. We handle pings (the cleanest signal) explicitly and let
# the caller filter by command type.

_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_ANSI_OTHER = re.compile(r"\x1b[()][\x20-\x7e]")

_RR = re.compile(
    r"\[drun\] Range rate:\s*(-?\d+\.\d+)\s*km/s,\s*Doppler@100M:\s*"
    r"([+-]?\d+\.\d+)\s*Hz"
)
_EXEC = re.compile(r"\[drun\] Executing:\s*(.+?)\s*$")
_PING_OK = re.compile(r"^Ping node\s+\d+\s.*Reply in\s+(\d+)\s*\[ms\]")
_PING_FAIL = re.compile(r"^Ping node\s+\d+\s.*No reply\s*$")
_NORESP = re.compile(r"^No response\s*$")
# A successful hk retrieve dumps a "Timestamp NNNN" line first.
_HK_OK = re.compile(r"^Timestamp\s+\d+\s*$")


def _strip_ansi(text: str) -> str:
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OTHER.sub("", text)
    return text.replace("\r", "").replace("\x1b", "")


@dataclasses.dataclass
class Attempt:
    """One command attempt against the bird, with outcome.

    Pings give per-attempt OK/FAIL with RTT — the cleanest loss signal.
    hk retrieve gives OK (data dumped) or FAIL (No response) without RTT.
    """
    idx: int
    t_offset_s: float          # idx * interval_s
    range_rate_kms: float | None
    doppler_hz: float | None
    command: str               # e.g., "ping", "ping 5386", "hk retrieve -o"
    outcome: str               # "OK" | "FAIL"
    rtt_ms: int | None         # only for ping OK


def parse_bird_log(path: Path, interval_s: float = 5.0,
                   command_filter: str = "ping") -> list[Attempt]:
    """Walk a CSH session capture, yield attempts of the chosen command.

    The bird's operators typically run `watch -n 5000 drun <cmd>` (5 s cadence)
    during a pass. `interval_s` reflects that and is used to assign
    monotonic timestamps to attempts since drun itself does not log a clock.

    The parser is outcome-driven: it counts each outcome line (Ping reply /
    No reply / No response / hk data dump) as one attempt. The `Executing:`
    headers are decorative — `watch` re-fires the command every cycle but
    only prints the header on the first invocation, so header-driven parsing
    would miss the bulk of attempts.

    command_filter:
      - "ping"  : count Ping reply / No reply lines as attempts
      - "hk"    : count Timestamp / No response lines as hk attempts
      - "all"   : both kinds; outcome lines are tagged by which regex matched
    """
    text = _strip_ansi(path.read_text(encoding="utf-8", errors="replace"))
    attempts: list[Attempt] = []
    cur_rr: float | None = None
    cur_dop: float | None = None
    want_ping = command_filter in ("ping", "all")
    want_hk = command_filter in ("hk", "all")

    def _push(command: str, outcome: str, rtt_ms: int | None) -> None:
        idx = len(attempts)
        attempts.append(Attempt(
            idx=idx, t_offset_s=idx * interval_s,
            range_rate_kms=cur_rr, doppler_hz=cur_dop,
            command=command, outcome=outcome, rtt_ms=rtt_ms,
        ))

    # Track which command type is "currently being watched" so that a bare
    # `No response` (used by hk) doesn't get attributed to ping. Updated on
    # every `Executing:` line; defaults to None.
    last_executing_kind: str | None = None

    for raw in text.split("\n"):
        line = raw.rstrip()

        m = _RR.search(line)
        if m:
            cur_rr = float(m.group(1))
            cur_dop = float(m.group(2))
            continue

        m = _EXEC.search(line)
        if m:
            cmd_lower = m.group(1).strip().lower()
            if cmd_lower.startswith("ping"):
                last_executing_kind = "ping"
            elif cmd_lower.startswith("hk "):
                last_executing_kind = "hk"
            else:
                last_executing_kind = "other"
            continue

        # Ping outcomes — unambiguous, prefixed by "Ping node".
        if want_ping:
            m = _PING_OK.match(line)
            if m:
                _push("ping", "OK", int(m.group(1)))
                continue
            if _PING_FAIL.match(line):
                _push("ping", "FAIL", None)
                continue

        # hk retrieve outcomes — only attributable when an hk command is the
        # active one. Otherwise "No response" might belong to a different
        # command type.
        if want_hk and last_executing_kind == "hk":
            if _HK_OK.match(line):
                _push("hk", "OK", None)
                continue
            if _NORESP.match(line):
                _push("hk", "FAIL", None)
                continue

    return attempts


def attempts_to_stats(attempts: list[Attempt]) -> dict:
    """Compute per-pass summary statistics that inform simulator design."""
    n = len(attempts)
    if n == 0:
        return {"attempts": 0, "successes": 0, "overall_success_rate": 0.0}
    ok = [a for a in attempts if a.outcome == "OK"]
    n_ok = len(ok)

    rtts = [a.rtt_ms for a in ok if a.rtt_ms is not None]
    rtt_block: dict = {"count": len(rtts)}
    if rtts:
        rtt_block.update({
            "min": min(rtts), "max": max(rtts),
            "median": median(rtts), "mean": round(mean(rtts), 1),
        })

    # Comm window = span from first OK to last OK.
    first_ok = next((a for a in attempts if a.outcome == "OK"), None)
    last_ok = next((a for a in reversed(attempts) if a.outcome == "OK"), None)
    window: dict = {}
    if first_ok is not None and last_ok is not None:
        window_attempts = [a for a in attempts
                           if first_ok.t_offset_s <= a.t_offset_s
                           <= last_ok.t_offset_s]
        window_ok = sum(1 for a in window_attempts if a.outcome == "OK")
        window = {
            "first_ok_idx": first_ok.idx,
            "first_ok_t_s": first_ok.t_offset_s,
            "last_ok_idx": last_ok.idx,
            "last_ok_t_s": last_ok.t_offset_s,
            "duration_s": last_ok.t_offset_s - first_ok.t_offset_s,
            "attempts_inside": len(window_attempts),
            "successes_inside": window_ok,
            "success_rate_inside": (window_ok / len(window_attempts)
                                    if window_attempts else 0.0),
        }

    # Failure-run length distribution.
    fail_runs: list[int] = []
    cur = 0
    for a in attempts:
        if a.outcome == "FAIL":
            cur += 1
        else:
            if cur > 0:
                fail_runs.append(cur)
            cur = 0
    if cur > 0:
        fail_runs.append(cur)
    fail_run_hist = dict(sorted(Counter(fail_runs).items()))

    # Markov / Gilbert-Elliott transition probabilities. Counted over
    # consecutive attempt pairs; not split by window vs dead-zone because
    # the simulator's pattern file will encode dead zones explicitly.
    n_FF = n_FO = n_OF = n_OO = 0
    for prev, nxt in zip(attempts, attempts[1:]):
        a, b = prev.outcome, nxt.outcome
        if a == "FAIL" and b == "FAIL": n_FF += 1
        elif a == "FAIL" and b == "OK": n_FO += 1
        elif a == "OK" and b == "FAIL": n_OF += 1
        elif a == "OK" and b == "OK":   n_OO += 1
    markov: dict = {}
    if (n_FF + n_FO) > 0:
        markov["P_O_given_F"] = round(n_FO / (n_FF + n_FO), 3)
        markov["P_F_given_F"] = round(n_FF / (n_FF + n_FO), 3)
    if (n_OF + n_OO) > 0:
        markov["P_O_given_O"] = round(n_OO / (n_OF + n_OO), 3)
        markov["P_F_given_O"] = round(n_OF / (n_OF + n_OO), 3)

    # Pass quality classification.
    duration_s = window.get("duration_s", 0.0)
    inside_rate = window.get("success_rate_inside", 0.0)
    if duration_s >= 60 and inside_rate >= 0.30:
        quality = "excellent"
    elif duration_s >= 30 and inside_rate >= 0.15:
        quality = "good"
    elif duration_s >= 10:
        quality = "marginal"
    elif n_ok == 0:
        quality = "blackout"
    else:
        quality = "failed"

    return {
        "attempts": n,
        "successes": n_ok,
        "overall_success_rate": round(n_ok / n, 4),
        "rtt_ms": rtt_block,
        "comm_window": window,
        "fail_runs": fail_run_hist,
        "max_fail_run": max(fail_runs) if fail_runs else 0,
        "markov": markov,
        "quality": quality,
    }


def attempts_to_pattern(attempts: list[Attempt], stats: dict,
                        source_label: str,
                        window_only: bool = False,
                        time_compress: float = 1.0) -> Pattern:
    """Turn per-attempt outcomes into a loss-pattern file.

    Mapping rules:
      - Before the first OK: link is DOWN (AOS dead zone).
      - First OK onward, within window: emit toggle events that match
        each actual FAIL/OK transition. This is faithful — bursts you
        see in the real log play out at the same temporal granularity
        in the simulator (subject to time_compress).
      - After the last OK: link is DOWN (LOS dead zone).

    window_only=True trims the pattern to the comm window starting at t=0,
    omitting the AOS and LOS dead zones. Useful for F3.b loss-rate sweeps
    where you want to test inside the usable window.

    time_compress<1 plays the pass faster (e.g. 0.1 → 60s instead of 600s).
    """
    win = stats.get("comm_window", {})
    if not win:
        # No successes in the entire log: emit a single permanent-down.
        return Pattern(
            events=[PatternEvent(0.0, "down")],
            metadata={
                "source_log": source_label,
                "quality": stats.get("quality", "blackout"),
            },
            header_comments=[
                f"Derived from bird log: {source_label}",
                "No successful attempts in log — entire pass dead.",
            ],
        )

    first_t = win["first_ok_t_s"]
    last_t = win["last_ok_t_s"]
    events: list[PatternEvent] = []

    def push(t: float, action: str, prob: float | None = None) -> None:
        # Skip duplicate consecutive states.
        if events and events[-1].action == action and events[-1].prob == prob:
            return
        events.append(PatternEvent(t * time_compress, action, prob))

    if window_only:
        # Window-only: start at t=0 by shifting the timeline to first OK.
        push(0.0, "up")
        prev_state = "up"
        for a in attempts:
            if a.t_offset_s < first_t or a.t_offset_s > last_t:
                continue
            local_t = a.t_offset_s - first_t
            new_state = "up" if a.outcome == "OK" else "down"
            if new_state != prev_state:
                push(local_t, new_state)
                prev_state = new_state
        # End the window with a clean state — leave whatever the last toggle was.
    else:
        # Full pass: dead zone, then mirrored sequence, then dead zone.
        push(0.0, "down")
        prev_state = "down"
        for a in attempts:
            new_state = "up" if a.outcome == "OK" else "down"
            if new_state != prev_state:
                push(a.t_offset_s, new_state)
                prev_state = new_state
        # Add an explicit LOS down event one tick after last attempt if not already.
        last_attempt_t = attempts[-1].t_offset_s + 5.0
        if prev_state != "down":
            push(last_attempt_t, "down")

    meta = {
        "source_log": source_label,
        "quality": stats["quality"],
        "attempts": str(stats["attempts"]),
        "successes": str(stats["successes"]),
        "window_duration_s": f"{win['duration_s']:.1f}",
        "window_success_rate": f"{win['success_rate_inside']:.3f}",
        "max_fail_run": str(stats["max_fail_run"]),
        "time_compress": f"{time_compress}",
        "scope": "window_only" if window_only else "full_pass",
    }
    if stats.get("rtt_ms", {}).get("count", 0) > 0:
        meta["rtt_median_ms"] = str(stats["rtt_ms"]["median"])

    return Pattern(
        events=events,
        metadata=meta,
        header_comments=[
            f"Derived from bird log: {source_label}",
            f"Pass quality: {stats['quality']}",
            (f"Comm window: {win['duration_s']:.0f} s, "
             f"{win['success_rate_inside']*100:.0f}% success rate inside"),
            ("WARNING: current loss_filter has no latency injection. Real "
             f"RTT median was {stats.get('rtt_ms', {}).get('median', '?')} "
             "ms; simulator RTT is microseconds. Retry-timeout tests over "
             "this pattern will under-stress libdtp."),
        ],
    )


def _parse_real_log(path: Path,
                    loss_definition: str = "csp_timeout",
                    interval_s: float = 5.0,
                    command_filter: str = "ping",
                    window_only: bool = False,
                    time_compress: float = 1.0) -> tuple[Pattern, dict]:
    """Convert a DISCO-2 bird CSH session log into a Pattern + stats dict.

    `loss_definition` is accepted for spec compatibility but the bird logs
    only carry one signal — per-attempt OK/FAIL on the chosen command —
    so all three values map to the same parse path.
    """
    _ = loss_definition  # reserved
    attempts = parse_bird_log(path, interval_s=interval_s,
                              command_filter=command_filter)
    stats = attempts_to_stats(attempts)
    pat = attempts_to_pattern(attempts, stats, source_label=path.name,
                              window_only=window_only,
                              time_compress=time_compress)
    return pat, stats


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="Validate a pattern file")
    p_validate.add_argument("path", type=Path)

    p_scale = sub.add_parser("scale", help="Scale drop intensity")
    p_scale.add_argument("path", type=Path)
    p_scale.add_argument("--factor", type=float, required=True)
    p_scale.add_argument("--out", type=Path, required=True)

    p_shift = sub.add_parser("shift", help="Shift timestamps")
    p_shift.add_argument("path", type=Path)
    p_shift.add_argument("--dt", type=float, required=True)
    p_shift.add_argument("--out", type=Path, required=True)

    p_pw = sub.add_parser("build-pass-window",
                          help="Generate synthetic pass-window pattern")
    p_pw.add_argument("--pass-len-s", type=float, required=True)
    p_pw.add_argument("--gap-s", type=float, required=True)
    p_pw.add_argument("--total-len-s", type=float, required=True)
    p_pw.add_argument("--out", type=Path, required=True)

    p_parse = sub.add_parser("from-log",
                             help="Parse a real pass log into a pattern")
    p_parse.add_argument("path", type=Path)
    p_parse.add_argument("--loss-def", default="csp_timeout",
                         choices=["csp_timeout", "modem_lock", "crc_failure"])
    p_parse.add_argument("--cmd-filter", default="ping",
                         choices=["ping", "hk", "all"],
                         help="Which drun commands to use as loss signal")
    p_parse.add_argument("--interval-s", type=float, default=5.0,
                         help="Spacing between attempts (drun watch cadence)")
    p_parse.add_argument("--window-only", action="store_true",
                         help="Emit only the comm window starting at t=0")
    p_parse.add_argument("--time-compress", type=float, default=1.0,
                         help="Multiplier on pattern time (0.1 = 10x faster)")
    p_parse.add_argument("--out", type=Path, required=True)
    p_parse.add_argument("--sidecar-json", type=Path, default=None,
                         help="Also write a JSON file with per-pass stats")

    p_batch = sub.add_parser("batch-from-logs",
                             help="Convert a directory of bird logs at once")
    p_batch.add_argument("log_dir", type=Path)
    p_batch.add_argument("--out-dir", type=Path, required=True)
    p_batch.add_argument("--cmd-filter", default="ping",
                         choices=["ping", "hk", "all"])
    p_batch.add_argument("--interval-s", type=float, default=5.0)
    p_batch.add_argument("--window-only", action="store_true")
    p_batch.add_argument("--time-compress", type=float, default=1.0)
    p_batch.add_argument("--min-attempts", type=int, default=20,
                         help="Skip logs with fewer than this many attempts")
    p_batch.add_argument("--index-csv", type=Path, default=None,
                         help="Write a per-pass index CSV (quality, stats)")

    args = ap.parse_args()

    if args.cmd == "validate":
        pat = load_pattern(args.path)
        errors = validate(pat)
        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"OK: {args.path} ({len(pat.events)} events)")
        return 0

    if args.cmd == "scale":
        pat = load_pattern(args.path)
        write_pattern(args.out, scale(pat, args.factor))
        return 0

    if args.cmd == "shift":
        pat = load_pattern(args.path)
        write_pattern(args.out, shift(pat, args.dt))
        return 0

    if args.cmd == "build-pass-window":
        pat = build_pass_window(args.pass_len_s, args.gap_s, args.total_len_s)
        write_pattern(args.out, pat)
        return 0

    if args.cmd == "from-log":
        pat, stats = _parse_real_log(
            args.path, args.loss_def,
            interval_s=args.interval_s,
            command_filter=args.cmd_filter,
            window_only=args.window_only,
            time_compress=args.time_compress,
        )
        write_pattern(args.out, pat)
        if args.sidecar_json is not None:
            args.sidecar_json.write_text(json.dumps(stats, indent=2) + "\n")
        print(
            f"{args.path.name}: quality={stats['quality']} "
            f"attempts={stats['attempts']} successes={stats['successes']} "
            f"({stats['overall_success_rate']*100:.0f}%)"
        )
        return 0

    if args.cmd == "batch-from-logs":
        args.out_dir.mkdir(parents=True, exist_ok=True)
        index_rows: list[dict] = []
        processed = skipped = 0
        for log_path in sorted(args.log_dir.glob("*.log")):
            try:
                pat, stats = _parse_real_log(
                    log_path, "csp_timeout",
                    interval_s=args.interval_s,
                    command_filter=args.cmd_filter,
                    window_only=args.window_only,
                    time_compress=args.time_compress,
                )
            except Exception as e:
                print(f"SKIP {log_path.name}: {e}", file=sys.stderr)
                skipped += 1
                continue
            if stats["attempts"] < args.min_attempts:
                skipped += 1
                continue
            scope = "window" if args.window_only else "full"
            stem = log_path.stem.replace(":", "")
            pattern_path = args.out_dir / f"{stem}.{scope}.pattern"
            stats_path = args.out_dir / f"{stem}.stats.json"
            write_pattern(pattern_path, pat)
            stats_path.write_text(json.dumps(stats, indent=2) + "\n")
            index_rows.append({
                "log": log_path.name,
                "pattern": pattern_path.name,
                "quality": stats["quality"],
                "attempts": stats["attempts"],
                "successes": stats["successes"],
                "success_rate": stats["overall_success_rate"],
                "window_s": stats.get("comm_window", {}).get("duration_s", 0),
                "window_success_rate": stats.get("comm_window", {})
                .get("success_rate_inside", 0),
                "max_fail_run": stats.get("max_fail_run", 0),
                "rtt_median_ms": stats.get("rtt_ms", {}).get("median", ""),
            })
            processed += 1
        if args.index_csv is not None and index_rows:
            import csv
            with args.index_csv.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(index_rows[0].keys()))
                w.writeheader()
                w.writerows(index_rows)
        # Quality breakdown summary.
        qcounts: Counter[str] = Counter(r["quality"] for r in index_rows)
        print(f"processed={processed} skipped={skipped}")
        for q, c in sorted(qcounts.items(), key=lambda kv: -kv[1]):
            print(f"  {q}: {c}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
