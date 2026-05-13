#!/usr/bin/env python3
"""diff_stress_test.py — compare old vs new bird-pattern stats.

Run after the parser fixes (cadence detection + three-zone Markov) to see
which passes actually changed.

Usage:
    python3 experiments/diff_stress_test.py \\
        /tmp/bird-patterns-window.OLD \\
        experiments/results/bird-patterns-window
"""
import json
import sys
from pathlib import Path


def load_stats(d: Path) -> dict[str, dict]:
    out = {}
    for sp in sorted(d.glob("*.stats.json")):
        out[sp.stem.replace(".stats", "")] = json.loads(sp.read_text())
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old = load_stats(Path(sys.argv[1]))
    new = load_stats(Path(sys.argv[2]))

    changed = []
    for key in sorted(set(old) & set(new)):
        o, n = old[key], new[key]
        diffs = []
        # First-OK timestamp drift = cadence-fix impact.
        old_first_t = o.get("comm_window", {}).get("first_ok_t_s")
        new_first_t = n.get("comm_window", {}).get("first_ok_t_s")
        if old_first_t != new_first_t:
            diffs.append(f"first_ok_t_s {old_first_t} -> {new_first_t}")
        # Window duration drift.
        old_dur = o.get("comm_window", {}).get("duration_s")
        new_dur = n.get("comm_window", {}).get("duration_s")
        if old_dur != new_dur:
            diffs.append(f"duration_s {old_dur} -> {new_dur}")
        # Markov P(F|F) shift (old=overall, new exposes overall + in_window).
        old_pff = o.get("markov", {}).get("P_F_given_F")
        new_pff = n.get("markov_overall", {}).get("P_F_given_F")
        if old_pff != new_pff:
            diffs.append(f"markov_overall P(F|F) {old_pff} -> {new_pff}")
        # Quality shift.
        if o.get("quality") != n.get("quality"):
            diffs.append(f"quality {o.get('quality')} -> {n.get('quality')}")
        if diffs:
            changed.append((key, diffs))

    # New-only fields (in-window Markov, in-window max fail run).
    sample_key = next(iter(new), None)
    new_fields = []
    if sample_key:
        for k in ("markov_in_window", "markov_aos_los",
                 "in_window_fail_runs", "max_in_window_fail_run"):
            if k in new[sample_key] and k not in (old.get(sample_key) or {}):
                new_fields.append(k)

    print(f"# Pattern regeneration stress-test report")
    print()
    print(f"OLD: {sys.argv[1]} ({len(old)} files)")
    print(f"NEW: {sys.argv[2]} ({len(new)} files)")
    print()
    print(f"## New stat fields exposed")
    for f in new_fields:
        print(f"- `{f}`")
    print()
    print(f"## Passes with changed stats: {len(changed)}/{len(set(old) & set(new))}")
    print()
    for key, diffs in changed:
        print(f"### {key}")
        for d in diffs:
            print(f"- {d}")
        # Show the new in-window Markov for this pass as the headline number.
        mw = new[key].get("markov_in_window", {})
        if mw:
            print(f"- in-window P(F|F) = {mw.get('P_F_given_F')}, "
                  f"P(F|O) = {mw.get('P_F_given_O')}")
        max_iw = new[key].get("max_in_window_fail_run")
        if max_iw is not None:
            print(f"- max in-window fail run = {max_iw}")
        print()

    # Aggregate: how does in-window P(F|F) distribute across passes?
    iw_pffs = [n.get("markov_in_window", {}).get("P_F_given_F")
               for n in new.values()
               if n.get("markov_in_window", {}).get("P_F_given_F") is not None]
    iw_pffs.sort()
    if iw_pffs:
        print("## In-window P(F|F) distribution (lower = less bursty)")
        print()
        n_p = len(iw_pffs)
        print(f"- passes with in-window data: {n_p}")
        print(f"- min: {iw_pffs[0]:.3f}")
        print(f"- median: {iw_pffs[n_p // 2]:.3f}")
        print(f"- max: {iw_pffs[-1]:.3f}")
        print(f"- p90: {iw_pffs[int(n_p * 0.9)]:.3f}")
        bursty = sum(1 for p in iw_pffs if p > 0.7)
        print(f"- bursty (P(F|F) > 0.7): {bursty} / {n_p} = "
              f"{bursty / n_p * 100:.0f}%")

    # Max in-window fail-run distribution.
    iw_runs = [n.get("max_in_window_fail_run") for n in new.values()
               if n.get("max_in_window_fail_run") is not None]
    iw_runs.sort()
    if iw_runs:
        n_r = len(iw_runs)
        print()
        print("## Max in-window fail-run distribution (longest burst per pass)")
        print()
        print(f"- median: {iw_runs[n_r // 2]}")
        print(f"- max: {iw_runs[-1]}")
        ge_10 = sum(1 for r in iw_runs if r >= 10)
        print(f"- passes with run >= 10: {ge_10} / {n_r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
