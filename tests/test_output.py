"""Tests for CLI output formatting."""

import click

from satdeploy.output import (
    SYMBOLS,
    ColoredGroup,
    SatDeployError,
    error,
    format_relative_time,
    normalize_timestamp,
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
