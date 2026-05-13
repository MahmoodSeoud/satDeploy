#!/usr/bin/env python3
"""
analyze_bird_sweep.py — turn bird_sweep.csv into a thesis-ready report.

Input:  experiments/results/bird_sweep.csv
        (produced by experiments/sweep_bird_patterns.sh)
Output: experiments/results/bird_sweep_report.md

The report answers: at realistic DISCO-2 in-window FER levels, where does
the naive baseline fail and where does the smart build still earn its
keep? It also flags where the 8-round retry budget is insufficient and
cross-pass resume becomes load-bearing.
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "experiments" / "results" / "bird_sweep.csv"
OUT = ROOT / "experiments" / "results" / "bird_sweep_report.md"


def load_rows(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def fmt_int(x: str) -> str:
    return x if x.isdigit() else "?"


def summarize(rows: list[dict]) -> str:
    by_build: dict[str, list[dict]] = {}
    for r in rows:
        by_build.setdefault(r["build"], []).append(r)

    lines: list[str] = []
    lines.append("# Bird-pattern sweep: F3.b at realistic DISCO-2 FER")
    lines.append("")
    lines.append(
        "Each row is one 1 MB push under a synthetic Bernoulli pattern "
        "at the in-window FER measured from a real DISCO-2 bird trace. "
        "See `experiments/sweep_bird_patterns.sh` for methodology; "
        "Bernoulli delivery is justified by `burstiness_analysis.md` "
        "(85% of in-window passes fit a geometric tail)."
    )
    lines.append("")
    lines.append("## Per-trial results")
    lines.append("")
    lines.append(
        "| build | pattern | bird FER | actual FER | got/total | gap | retry rounds | outcome |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        outcome = (
            "complete" if r["gap_class"] == "none"
            else "incomplete (cross-pass resume needed)" if r["retry_rounds"].isdigit() and int(r["retry_rounds"]) >= 8
            else "failed mid-transfer" if r["push_rc"] != "0"
            else f"gap={r['gap']}"
        )
        lines.append(
            f"| {r['build']} | {r['pattern']} | {r['bird_fer']} | "
            f"{r['actual_fer']} | {r['got']}/{r['total']} | {r['gap']} | "
            f"{r['retry_rounds']} | {outcome} |"
        )

    lines.append("")
    lines.append("## Aggregate by build")
    lines.append("")
    lines.append("| build | n | complete | mean retry rounds | hit retry cap (rounds=8) | mean actual FER |")
    lines.append("|---|---|---|---|---|---|")
    for build, rs in sorted(by_build.items()):
        n = len(rs)
        complete = sum(1 for r in rs if r["gap_class"] == "none")
        rounds = [int(r["retry_rounds"]) for r in rs if r["retry_rounds"].isdigit()]
        mean_rounds = statistics.mean(rounds) if rounds else 0
        hit_cap = sum(1 for r in rs if r["retry_rounds"].isdigit() and int(r["retry_rounds"]) >= 8)
        fers = [float(r["actual_fer"]) for r in rs if r["actual_fer"] not in {"?", ""}]
        mean_fer = statistics.mean(fers) if fers else 0.0
        lines.append(
            f"| {build} | {n} | {complete}/{n} | {mean_rounds:.1f} | "
            f"{hit_cap}/{n} | {mean_fer:.3f} |"
        )

    lines.append("")
    lines.append("## Verdict")
    lines.append("")

    smart = by_build.get("smart-loss", [])
    naive = by_build.get("naive-loss", [])
    smart_complete = sum(1 for r in smart if r["gap_class"] == "none")
    smart_hit_cap = sum(1 for r in smart if r["retry_rounds"].isdigit() and int(r["retry_rounds"]) >= 8)
    naive_complete = sum(1 for r in naive if r["gap_class"] == "none")

    lines.append(
        f"- **Naive baseline:** completes {naive_complete}/{len(naive)} single-pass "
        "at realistic DISCO FER. The retry loop is not optional decoration."
    )
    lines.append(
        f"- **Smart build:** completes {smart_complete}/{len(smart)} single-pass; "
        f"{smart_hit_cap}/{len(smart)} hit the 8-round retry cap and persist "
        "session state for cross-pass resume."
    )

    if smart_hit_cap > 0:
        lines.append("")
        lines.append(
            f"- **Where cross-pass resume earns its keep:** the {smart_hit_cap} "
            "patterns above are exactly the regime where a single pass can't "
            "close the transfer. The on-disk bitmap sidecar is what carries the "
            "partial state into the next pass; F5 reboot demo measures that "
            "end-to-end."
        )

    lines.append("")
    lines.append("## Source")
    lines.append("")
    lines.append(f"- Raw CSV: `experiments/results/bird_sweep.csv` ({len(rows)} trials)")
    lines.append("- Harness: `experiments/sweep_bird_patterns.sh`")
    lines.append("- Bird-pattern stats: `experiments/results/bird-patterns-window/*.stats.json`")

    return "\n".join(lines) + "\n"


def main() -> int:
    if not CSV.exists():
        print(f"error: {CSV} not found — run sweep_bird_patterns.sh first", file=sys.stderr)
        return 1
    rows = load_rows(CSV)
    if not rows:
        print(f"error: {CSV} has no rows", file=sys.stderr)
        return 1
    OUT.write_text(summarize(rows))
    print(f"wrote {OUT} ({len(rows)} trials)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
