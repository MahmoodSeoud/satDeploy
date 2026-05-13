#!/usr/bin/env python3
"""burstiness_analysis.py — verdict on Bernoulli vs Gilbert for in-window
DISCO-2 loss patterns.

For each pass with in-window data, compare observed fail-run length
distribution against the Bernoulli (IID) null hypothesis. If observed long
runs are dramatically more frequent than Bernoulli predicts, the
Gilbert-Elliott action is justified. Otherwise, the simpler `prob` action
is enough.

Bernoulli math: if every packet has independent failure probability p
(= in-window FER), the run-length distribution is geometric:
    P(run length == k) = p^(k-1) * (1 - p)   for k >= 1
Expected number of runs of length k, given N total runs:
    E_k = N * p^(k-1) * (1 - p)

We compute the burst-ratio metric per pass:
    burst_ratio = observed_long_runs / expected_long_runs_under_bernoulli
where long = run length >= 5 (chosen because Bernoulli at the typical
in-window FER ~0.5-0.65 produces almost zero runs of length 5+; if real
data has many, that's the bursty fingerprint).

Verdict:
    burst_ratio < 2x   : Bernoulli is fine, gilbert action unjustified
    burst_ratio 2-5x   : moderate burstiness, gilbert helpful
    burst_ratio > 5x   : strong burstiness, gilbert clearly needed
"""
import json
import sys
from pathlib import Path


def expected_geometric_count(p: float, k: int, n_runs: int) -> float:
    """Expected count of runs of length exactly k under Bernoulli(p)."""
    if not (0.0 < p < 1.0):
        return 0.0
    return n_runs * (p ** (k - 1)) * (1.0 - p)


def expected_long_runs(p: float, n_runs: int, threshold: int) -> float:
    """Expected total count of runs with length >= threshold under
    Bernoulli(p). Sum of the geometric tail: p^(t-1) * n_runs."""
    if not (0.0 < p < 1.0):
        return 0.0
    return n_runs * (p ** (threshold - 1))


def analyze(stats: dict) -> dict | None:
    """Return per-pass burstiness verdict, or None if no in-window data."""
    in_runs_hist = stats.get("in_window_fail_runs") or {}
    if not in_runs_hist:
        return None

    # Convert string keys back to ints (from JSON load).
    runs_hist = {int(k): v for k, v in in_runs_hist.items()}
    total_runs = sum(runs_hist.values())
    if total_runs < 3:
        return None  # too few runs for any meaningful comparison

    # Estimate Bernoulli p from in-window FER.
    win = stats.get("comm_window") or {}
    fer = 1.0 - (win.get("success_rate_inside") or 0.0)
    if not (0.05 < fer < 0.95):
        return None  # extreme FER makes the comparison meaningless

    threshold = 5
    observed_long = sum(c for k, c in runs_hist.items() if k >= threshold)
    expected_long = expected_long_runs(fer, total_runs, threshold)

    if expected_long < 0.5:
        # Bernoulli essentially predicts zero long runs. Use the actual
        # observed count as an absolute fingerprint.
        if observed_long == 0:
            verdict = "bernoulli_fits"
            burst_ratio = 1.0
        else:
            burst_ratio = observed_long / max(expected_long, 0.01)
            verdict = "strong_burstiness"
    else:
        burst_ratio = observed_long / expected_long
        if burst_ratio < 2.0:
            verdict = "bernoulli_fits"
        elif burst_ratio < 5.0:
            verdict = "moderate_burstiness"
        else:
            verdict = "strong_burstiness"

    return {
        "fer": round(fer, 3),
        "n_runs": total_runs,
        "max_run": max(runs_hist),
        "observed_long_runs_ge5": observed_long,
        "expected_long_runs_ge5_bernoulli": round(expected_long, 2),
        "burst_ratio": round(burst_ratio, 1),
        "verdict": verdict,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    if not src.is_dir():
        print(f"ERROR: {src} is not a directory", file=sys.stderr)
        return 1

    results = []
    for sp in sorted(src.glob("*.stats.json")):
        stats = json.loads(sp.read_text())
        verdict = analyze(stats)
        if verdict is not None:
            verdict["pass"] = sp.stem.replace(".stats", "")
            results.append(verdict)

    # Print Markdown report.
    print("# Burstiness analysis: Bernoulli vs Gilbert for in-window loss\n")
    print(f"Analyzed {len(results)} passes with usable in-window data.\n")
    print("## Method\n")
    print(
        "For each pass, the in-window failure-run length distribution is\n"
        "compared to the Bernoulli null hypothesis with matching mean FER.\n"
        "Under Bernoulli IID loss, run lengths are geometrically distributed:\n"
        "P(run = k) = p^(k-1) * (1-p). Long-run count = sum of tail.\n"
        "\n"
        "Burst ratio = observed runs of length >= 5 / Bernoulli-expected.\n"
        "  < 2x  : Bernoulli fits, Gilbert unjustified\n"
        "  2-5x  : moderate burstiness\n"
        "  > 5x  : strong burstiness, Gilbert clearly needed\n"
    )

    verdict_counts: dict[str, int] = {}
    for r in results:
        verdict_counts[r["verdict"]] = verdict_counts.get(r["verdict"], 0) + 1
    print("## Aggregate verdict\n")
    for v, c in sorted(verdict_counts.items(), key=lambda kv: -kv[1]):
        print(f"- **{v}**: {c} / {len(results)} passes "
              f"({c / len(results) * 100:.0f}%)")
    print()

    # Show the most bursty and the least bursty passes.
    by_ratio = sorted(results, key=lambda r: r["burst_ratio"], reverse=True)
    print("## Top 5 bursty passes\n")
    print("| Pass | FER | n_runs | max_run | obs >=5 | exp >=5 | ratio |")
    print("|------|-----|--------|---------|---------|---------|-------|")
    for r in by_ratio[:5]:
        print(f"| {r['pass']} | {r['fer']} | {r['n_runs']} | "
              f"{r['max_run']} | {r['observed_long_runs_ge5']} | "
              f"{r['expected_long_runs_ge5_bernoulli']} | "
              f"{r['burst_ratio']}x |")
    print()
    print("## Top 5 Bernoulli-fitting passes\n")
    print("| Pass | FER | n_runs | max_run | obs >=5 | exp >=5 | ratio |")
    print("|------|-----|--------|---------|---------|---------|-------|")
    for r in by_ratio[-5:]:
        print(f"| {r['pass']} | {r['fer']} | {r['n_runs']} | "
              f"{r['max_run']} | {r['observed_long_runs_ge5']} | "
              f"{r['expected_long_runs_ge5_bernoulli']} | "
              f"{r['burst_ratio']}x |")
    print()

    print("## Verdict\n")
    bernoulli_count = verdict_counts.get("bernoulli_fits", 0)
    moderate_count = verdict_counts.get("moderate_burstiness", 0)
    strong_count = verdict_counts.get("strong_burstiness", 0)
    pct_bernoulli = bernoulli_count / len(results) * 100
    pct_strong = strong_count / len(results) * 100

    if pct_bernoulli > 70:
        print(
            f"**Bernoulli is sufficient for most DISCO-2 passes** "
            f"({pct_bernoulli:.0f}% fit). The `gilbert` action is overkill "
            f"for typical traffic. Reserve it for the {pct_strong:.0f}% of "
            f"passes that show strong burstiness."
        )
    elif pct_strong > 30:
        print(
            f"**Gilbert is justified for a meaningful fraction of passes** "
            f"({pct_strong:.0f}% show strong burstiness). Both actions have "
            f"real use cases — Bernoulli for typical, Gilbert for the bursty "
            f"tail."
        )
    else:
        print(
            f"**Mixed picture.** Bernoulli fits {pct_bernoulli:.0f}%, "
            f"moderate burstiness in {moderate_count / len(results) * 100:.0f}%, "
            f"strong in {pct_strong:.0f}%. Use Gilbert with measured Markov "
            f"params for the bursty subset; default to `prob` otherwise."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
