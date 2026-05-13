#!/usr/bin/env python3
"""Unit tests for parse_pass_log.py.

Run with:
    python3 experiments/lib/test_parse_pass_log.py
    # or
    python3 -m unittest experiments.lib.test_parse_pass_log
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_pass_log import (
    _parse_real_log,
    _strip_ansi,
    attempts_to_pattern,
    attempts_to_stats,
    load_pattern,
    parse_bird_log,
    validate,
)


def _write_log(text: str) -> Path:
    """Write a temp .log file containing `text`, return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class StripAnsiTests(unittest.TestCase):
    def test_csi_sequences_removed(self):
        s = "\x1b[31mred\x1b[0m"
        self.assertEqual(_strip_ansi(s), "red")

    def test_carriage_returns_removed(self):
        self.assertEqual(_strip_ansi("a\r\nb"), "a\nb")

    def test_plain_text_passthrough(self):
        self.assertEqual(_strip_ansi("hello world"), "hello world")


class CadenceDetectionTests(unittest.TestCase):
    """The stress test found that not every pass uses `watch -n 5000`.
    Mixed cadences appear in real logs (5000, 2000, 500, 50, 5 ms). The
    parser must read the actual cadence from the CSH watch banner instead
    of hardcoding 5s."""

    def test_default_5s_when_no_banner(self):
        log = (
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1267 [ms]\n"
        )
        path = _write_log(log)
        attempts = parse_bird_log(path)
        self.assertEqual([a.t_offset_s for a in attempts], [0.0, 5.0, 10.0])

    def test_uses_watch_banner_cadence(self):
        # 2000ms banner -> 2.0s between attempts
        log = (
            'Executing "drun ping " each 2000 ms - press <enter> to stop\n'
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1265 [ms]\n"
        )
        path = _write_log(log)
        attempts = parse_bird_log(path)
        self.assertEqual([a.t_offset_s for a in attempts], [0.0, 2.0, 4.0])

    def test_cadence_switch_mid_log(self):
        # Two watch invocations at different cadences. Attempts before the
        # switch use the first cadence; attempts after use the second.
        log = (
            'Executing "drun ping " each 5000 ms - press <enter> to stop\n'
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            'Executing "drun ping " each 500 ms - press <enter> to stop\n'
            "Ping node 5386 size 0 timeout 5000: Reply in 1267 [ms]\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1265 [ms]\n"
        )
        path = _write_log(log)
        attempts = parse_bird_log(path)
        # First two at 5s gaps: 0.0, 5.0
        # Then cadence switches to 0.5s; the third attempt comes 0.5s after.
        self.assertEqual([a.t_offset_s for a in attempts],
                         [0.0, 5.0, 5.5, 6.0])

    def test_unusual_short_cadence(self):
        # Real logs contain `watch -n 5` (5ms — operator stress-test runs).
        log = (
            'Executing "drun ping " each 5 ms - press <enter> to stop\n'
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
        )
        path = _write_log(log)
        attempts = parse_bird_log(path)
        self.assertEqual([a.t_offset_s for a in attempts], [0.0, 0.005])


class ParseBirdLogTests(unittest.TestCase):
    """Outcome-driven parsing: each Ping line is one attempt, regardless of
    whether `Executing:` headers appear (operators use `watch` which fires
    the command repeatedly but only prints the header once)."""

    def test_counts_pings_without_executing_headers(self):
        # Three "No reply" lines, no [drun] Executing headers — simulates
        # what `watch -n 5000 drun ping` produces after the first iteration.
        log = (
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1267 [ms]\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
        )
        path = _write_log(log)
        attempts = parse_bird_log(path, interval_s=5.0, command_filter="ping")
        self.assertEqual(len(attempts), 3)
        self.assertEqual([a.outcome for a in attempts], ["FAIL", "OK", "FAIL"])
        self.assertEqual(attempts[1].rtt_ms, 1267)
        # Timestamps are spaced by interval_s.
        self.assertEqual([a.t_offset_s for a in attempts], [0.0, 5.0, 10.0])

    def test_attaches_range_rate_and_doppler(self):
        log = (
            "[drun] Range rate: -5.918 km/s, Doppler@100M: +1974.1 Hz\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1269 [ms]\n"
        )
        path = _write_log(log)
        attempts = parse_bird_log(path)
        self.assertEqual(len(attempts), 1)
        self.assertAlmostEqual(attempts[0].range_rate_kms, -5.918)
        self.assertAlmostEqual(attempts[0].doppler_hz, 1974.1)

    def test_hk_command_outcomes_gated_by_last_executing(self):
        # "No response" appears in many contexts. The parser must only
        # attribute it to hk if hk was the most recently executed command.
        log = (
            "[drun] Executing: hk retrieve -o\n"
            "Timestamp 1234\n"
            "[drun] Executing: hk retrieve -o\n"
            "No response\n"
            "[drun] Executing: ping\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            # This bare "No response" should NOT be counted as hk since
            # the active command is now ping.
            "No response\n"
        )
        path = _write_log(log)
        hk_attempts = parse_bird_log(path, command_filter="hk")
        self.assertEqual(len(hk_attempts), 2)
        self.assertEqual([a.outcome for a in hk_attempts], ["OK", "FAIL"])

    def test_command_filter_ping_ignores_hk(self):
        log = (
            "[drun] Executing: hk retrieve -o\n"
            "Timestamp 1234\n"
            "No response\n"
            "[drun] Executing: ping\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1265 [ms]\n"
        )
        path = _write_log(log)
        ping_attempts = parse_bird_log(path, command_filter="ping")
        self.assertEqual(len(ping_attempts), 1)
        self.assertEqual(ping_attempts[0].outcome, "OK")


class AttemptsToStatsTests(unittest.TestCase):
    def _build(self, outcomes, rtts=None):
        """Create a synthetic attempt list from an outcome sequence."""
        from parse_pass_log import Attempt
        attempts = []
        rtt_iter = iter(rtts or [])
        for i, o in enumerate(outcomes):
            r = next(rtt_iter, None) if o == "OK" else None
            attempts.append(Attempt(
                idx=i, t_offset_s=i * 5.0,
                range_rate_kms=None, doppler_hz=None,
                command="ping", outcome=o, rtt_ms=r,
            ))
        return attempts

    def test_empty_input(self):
        stats = attempts_to_stats([])
        self.assertEqual(stats["attempts"], 0)
        self.assertEqual(stats["successes"], 0)
        self.assertEqual(stats["overall_success_rate"], 0.0)

    def test_all_fail_classified_blackout(self):
        attempts = self._build(["FAIL"] * 20)
        stats = attempts_to_stats(attempts)
        self.assertEqual(stats["successes"], 0)
        self.assertEqual(stats["quality"], "blackout")
        self.assertEqual(stats["max_fail_run"], 20)

    def test_comm_window_first_and_last_ok(self):
        # Pattern: F F F O F O F F  → window from idx 3 to idx 5
        attempts = self._build(
            ["FAIL", "FAIL", "FAIL", "OK", "FAIL", "OK", "FAIL", "FAIL"],
            rtts=[1267, 1265],
        )
        stats = attempts_to_stats(attempts)
        self.assertEqual(stats["comm_window"]["first_ok_idx"], 3)
        self.assertEqual(stats["comm_window"]["last_ok_idx"], 5)
        self.assertEqual(stats["comm_window"]["duration_s"], 10.0)
        # Inside window has 3 attempts (3,4,5), 2 OK.
        self.assertEqual(stats["comm_window"]["attempts_inside"], 3)
        self.assertEqual(stats["comm_window"]["successes_inside"], 2)

    def test_markov_transition_counts(self):
        # F F O F O O  → pairs: FF, FO, OF, FO, OO
        # P(F|F) = 1/2, P(O|F) = 2/3, P(F|O) = 1/3, P(O|O) = 2/3? wait, let me retrace.
        # Pairs: (F,F), (F,O), (O,F), (F,O), (O,O)
        # From F: next was F once, O twice → P(O|F)=2/3, P(F|F)=1/3
        # From O: next was F once, O once → P(F|O)=1/2, P(O|O)=1/2
        attempts = self._build(["FAIL", "FAIL", "OK", "FAIL", "OK", "OK"],
                               rtts=[1265, 1267, 1264])
        stats = attempts_to_stats(attempts)
        m = stats["markov"]
        self.assertAlmostEqual(m["P_F_given_F"], 1/3, places=3)
        self.assertAlmostEqual(m["P_O_given_F"], 2/3, places=3)
        self.assertAlmostEqual(m["P_F_given_O"], 1/2, places=3)
        self.assertAlmostEqual(m["P_O_given_O"], 1/2, places=3)

    def test_fail_run_histogram(self):
        # F F F O F F O F → runs of: 3, 2, 1
        attempts = self._build(
            ["FAIL", "FAIL", "FAIL", "OK", "FAIL", "FAIL", "OK", "FAIL"],
            rtts=[1265, 1267],
        )
        stats = attempts_to_stats(attempts)
        self.assertEqual(stats["fail_runs"], {1: 1, 2: 1, 3: 1})
        self.assertEqual(stats["max_fail_run"], 3)

    def test_three_zone_markov_separates_aos_from_window(self):
        # 10 leading FAILs (AOS) then O F F O F O then 5 trailing FAILs (LOS).
        # Total: F*10 + O + F + F + O + F + O + F*5
        outcomes = (["FAIL"] * 10
                    + ["OK", "FAIL", "FAIL", "OK", "FAIL", "OK"]
                    + ["FAIL"] * 5)
        attempts = self._build(outcomes, rtts=[1265, 1267, 1264])
        stats = attempts_to_stats(attempts)

        # In-window slice = first_ok (idx 10) to last_ok (idx 15) inclusive:
        # O F F O F O — six attempts, three OKs, max in-window fail run = 2.
        # Pairs inside window: OF, FF, FO, OF, FO.
        #   From F (positions 11, 12, 14):
        #     11→F (FF), 12→O (FO), 14→O (FO) → P(F|F)=1/3, P(O|F)=2/3
        #   From O (positions 10, 13):
        #     10→F, 13→F → P(F|O)=1.0, P(O|O)=0
        self.assertEqual(stats["max_in_window_fail_run"], 2)
        self.assertEqual(stats["in_window_fail_runs"], {1: 1, 2: 1})

        mw = stats["markov_in_window"]
        self.assertAlmostEqual(mw["P_F_given_F"], 1/3, places=3)
        self.assertAlmostEqual(mw["P_O_given_F"], 2/3, places=3)
        self.assertAlmostEqual(mw["P_F_given_O"], 1.0, places=3)

        # Dead-zone Markov: 10 F's at AOS + 5 F's at LOS → 9 FF pairs at AOS,
        # 4 FF pairs at LOS. Total: 13 FF pairs, no O transitions inside.
        # P(F|F) should be 1.0 — dead-zone is pure F→F.
        ml = stats["markov_aos_los"]
        self.assertAlmostEqual(ml["P_F_given_F"], 1.0, places=3)

        # Overall Markov is dominated by the dead zones and will be much
        # higher than the in-window value. That's the whole point of
        # splitting them.
        mo = stats["markov_overall"]
        self.assertGreater(mo["P_F_given_F"], mw["P_F_given_F"])

    def test_no_window_skips_in_window_stats(self):
        # All FAIL → no comm window → in_window stats should be empty/zero.
        attempts = self._build(["FAIL"] * 10)
        stats = attempts_to_stats(attempts)
        self.assertEqual(stats["max_in_window_fail_run"], 0)
        self.assertEqual(stats["in_window_fail_runs"], {})
        self.assertEqual(stats["markov_in_window"], {})

    def test_markov_top_level_matches_overall(self):
        # Backwards compatibility: `markov` key == `markov_overall`.
        attempts = self._build(["FAIL", "FAIL", "OK", "FAIL", "OK"],
                               rtts=[1265, 1267])
        stats = attempts_to_stats(attempts)
        self.assertEqual(stats["markov"], stats["markov_overall"])


class AttemptsToPatternTests(unittest.TestCase):
    def _attempts(self, outcomes):
        from parse_pass_log import Attempt
        return [
            Attempt(idx=i, t_offset_s=i * 5.0,
                    range_rate_kms=None, doppler_hz=None,
                    command="ping", outcome=o,
                    rtt_ms=1265 if o == "OK" else None)
            for i, o in enumerate(outcomes)
        ]

    def test_window_only_starts_at_zero(self):
        attempts = self._attempts(["FAIL", "FAIL", "OK", "FAIL", "OK", "FAIL"])
        stats = attempts_to_stats(attempts)
        pat = attempts_to_pattern(attempts, stats, "test.log", window_only=True)
        # First event is up at t=0.
        self.assertEqual(pat.events[0].action, "up")
        self.assertAlmostEqual(pat.events[0].t_offset_s, 0.0)
        # Pattern metadata is preserved.
        self.assertEqual(pat.metadata["scope"], "window_only")

    def test_full_pass_starts_with_down(self):
        attempts = self._attempts(["FAIL", "FAIL", "OK", "FAIL"])
        stats = attempts_to_stats(attempts)
        pat = attempts_to_pattern(attempts, stats, "test.log", window_only=False)
        self.assertEqual(pat.events[0].action, "down")
        self.assertAlmostEqual(pat.events[0].t_offset_s, 0.0)
        # Eventually flips to up when first OK arrives.
        ups = [e for e in pat.events if e.action == "up"]
        self.assertGreaterEqual(len(ups), 1)

    def test_no_successes_emits_permanent_down(self):
        attempts = self._attempts(["FAIL"] * 5)
        stats = attempts_to_stats(attempts)
        pat = attempts_to_pattern(attempts, stats, "test.log")
        self.assertEqual(len(pat.events), 1)
        self.assertEqual(pat.events[0].action, "down")


class RoundTripTests(unittest.TestCase):
    """End-to-end: parse log → pattern → load → validate."""

    def test_log_to_valid_pattern_file(self):
        log = (
            "[drun] Range rate: -5.918 km/s, Doppler@100M: +1974.1 Hz\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1267 [ms]\n"
            "Ping node 5386 size 0 timeout 5000: Reply in 1265 [ms]\n"
            "Ping node 5386 size 0 timeout 5000: No reply\n"
        )
        log_path = _write_log(log)
        pat, stats = _parse_real_log(log_path, command_filter="ping")
        # Write, load, validate.
        out_path = log_path.with_suffix(".pattern")
        out_path.write_text(pat.render())
        loaded = load_pattern(out_path)
        errors = validate(loaded)
        self.assertEqual(errors, [],
                         msg=f"Round-tripped pattern failed validation: {errors}")
        # JSON-serializable stats.
        json.dumps(stats)


if __name__ == "__main__":
    unittest.main(verbosity=2)
