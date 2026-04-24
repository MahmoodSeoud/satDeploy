"""Tests for CLI output formatting."""

import os
import stat

import click
import pytest
from click.testing import CliRunner

from satdeploy.output import (
    SYMBOLS,
    ColoredGroup,
    SatDeployError,
    error,
    format_relative_time,
    normalize_timestamp,
    shadow_binary_hint,
    step,
    success,
    warning,
)


class TestSymbols:
    """Standard symbols used across the CLI."""

    def test_check_symbol_defined(self):
        assert "check" in SYMBOLS
        assert SYMBOLS["check"] == "●"

    def test_cross_symbol_defined(self):
        assert "cross" in SYMBOLS
        assert SYMBOLS["cross"] == "✗"

    def test_arrow_symbol_defined(self):
        assert "arrow" in SYMBOLS
        assert SYMBOLS["arrow"] == "→"

    def test_bullet_symbol_defined(self):
        assert "bullet" in SYMBOLS
        assert SYMBOLS["bullet"] == "·"


class TestMessageFormatters:
    """Formatters for success/warning/error lines."""

    def test_success_contains_check_symbol(self):
        result = success("Done")
        assert SYMBOLS["check"] in result
        assert "Done" in result

    def test_warning_contains_message(self):
        result = warning("Caution")
        assert "Caution" in result
        # Warnings are yellow — must carry ANSI coloring
        assert "\x1b[" in result

    def test_error_contains_message_and_cross(self):
        result = error("Failed")
        assert "Failed" in result
        assert SYMBOLS["cross"] in result
        assert "\x1b[" in result


class TestStepFormatter:
    """Step counter formatting — the [N/M] shape is load-bearing."""

    def test_step_formats_with_counter(self):
        result = step(1, 5, "Backing up")
        assert "[1/5]" in result
        assert "Backing up" in result

    def test_step_keeps_counter_shape(self):
        result = step(2, 3, "Deploying")
        assert "[2/3]" in result
        assert "Deploying" in result


class TestSatDeployError:
    """Custom error exception."""

    def test_error_formats_message_in_red(self):
        err = SatDeployError("Something went wrong")
        formatted = err.format_message()
        assert "Something went wrong" in formatted
        assert "\x1b[" in formatted  # ANSI red

    def test_error_uses_cross_symbol(self):
        err = SatDeployError("Something went wrong")
        formatted = err.format_message()
        assert SYMBOLS["cross"] in formatted

    def test_error_is_click_exception(self):
        err = SatDeployError("Test error")
        assert isinstance(err, click.ClickException)


class TestColoredGroup:
    """Custom CLI group that colors errors."""

    def test_usage_error_shows_red(self):
        from click.testing import CliRunner

        @click.group(cls=ColoredGroup)
        def cli():
            pass

        @cli.command()
        @click.argument("name")
        def greet(name):
            click.echo(f"Hello {name}")

        runner = CliRunner()
        result = runner.invoke(cli, ["greet"], color=True)

        assert result.exit_code != 0
        assert "Missing argument" in result.output
        assert "\x1b[" in result.output  # ANSI code


class TestNormalizeTimestamp:
    def test_iso_with_t(self):
        assert normalize_timestamp("2024-01-15T14:30:22") == "2024-01-15 14:30:22"

    def test_iso_with_space(self):
        assert normalize_timestamp("2024-01-15 14:30:22") == "2024-01-15 14:30:22"

    def test_iso_with_z(self):
        assert normalize_timestamp("2024-01-15T14:30:22Z") == "2024-01-15 14:30:22"

    def test_empty_returns_dash(self):
        assert normalize_timestamp(None) == "-"
        assert normalize_timestamp("") == "-"

    def test_bogus_passes_through(self):
        assert normalize_timestamp("not a date") == "not a date"


class TestFormatRelativeTime:
    def test_none_returns_dash(self):
        assert format_relative_time(None) == "-"

    def test_future_returns_just_now(self):
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(seconds=60)).isoformat()
        assert format_relative_time(future) == "just now"

    def test_minutes_ago(self):
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        assert format_relative_time(past) == "5m ago"

    def test_hours_ago(self):
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(hours=3)).isoformat()
        assert format_relative_time(past) == "3h ago"


# ---------------------------------------------------------------------------
# Shadow-binary hint (DX review 2026-04-23, decision #1)
# ---------------------------------------------------------------------------
#
# When a user runs a newer `satdeploy` command that an older system-wide
# install is shadowing via PATH, Click's default "No such command" error
# leaves the user stuck. ColoredGroup now appends a hint listing every
# `satdeploy` binary on PATH so the user can spot the shadow themselves.


def _make_fake_binary(path):
    """Write a minimal executable file at path and make it exec+r."""
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)


class TestShadowBinaryHint:
    def test_returns_none_when_no_satdeploy_on_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert shadow_binary_hint() is None

    def test_returns_none_with_single_binary(self, tmp_path, monkeypatch):
        _make_fake_binary(tmp_path / "satdeploy")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert shadow_binary_hint() is None

    def test_fires_when_two_distinct_binaries_on_path(self, tmp_path, monkeypatch):
        venv = tmp_path / "venv" / "bin"
        system = tmp_path / "usr" / "local" / "bin"
        venv.mkdir(parents=True)
        system.mkdir(parents=True)
        _make_fake_binary(venv / "satdeploy")
        _make_fake_binary(system / "satdeploy")
        monkeypatch.setenv("PATH", f"{venv}{os.pathsep}{system}")
        hint = shadow_binary_hint()
        assert hint is not None
        assert str(venv / "satdeploy") in hint
        assert str(system / "satdeploy") in hint
        assert "PATH is resolving to the wrong one" in hint

    def test_dedupes_same_realpath(self, tmp_path, monkeypatch):
        """Two PATH entries pointing at the same file (via symlink) are
        still one install — don't falsely hint."""
        target_dir = tmp_path / "real"
        target_dir.mkdir()
        _make_fake_binary(target_dir / "satdeploy")
        link_dir = tmp_path / "symlinked"
        link_dir.symlink_to(target_dir)
        monkeypatch.setenv("PATH", f"{target_dir}{os.pathsep}{link_dir}")
        # Two PATH entries, one real binary. Hint should NOT fire.
        assert shadow_binary_hint() is None


class TestColoredGroupNoSuchCommand:
    def test_usage_error_includes_shadow_hint_when_multiple_binaries(
        self, tmp_path, monkeypatch
    ):
        venv = tmp_path / "venv" / "bin"
        system = tmp_path / "usr" / "local" / "bin"
        venv.mkdir(parents=True)
        system.mkdir(parents=True)
        _make_fake_binary(venv / "satdeploy")
        _make_fake_binary(system / "satdeploy")
        monkeypatch.setenv("PATH", f"{venv}{os.pathsep}{system}")

        @click.group(cls=ColoredGroup)
        def cli():
            pass

        @cli.command()
        def known():  # pragma: no cover - registered but not invoked
            pass

        runner = CliRunner()
        result = runner.invoke(cli, ["nonexistent"])
        assert result.exit_code != 0
        # Click's standard "No such command" preserved...
        assert "No such command" in result.output
        # ...and the hint is appended.
        assert "different `satdeploy` binaries" in result.output

    def test_usage_error_clean_when_no_shadow(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))  # empty

        @click.group(cls=ColoredGroup)
        def cli():
            pass

        runner = CliRunner()
        result = runner.invoke(cli, ["nonexistent"])
        assert result.exit_code != 0
        assert "No such command" in result.output
        # No hint when there's nothing to hint about.
        assert "different `satdeploy` binaries" not in result.output
