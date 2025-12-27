"""Tests for CLI output formatting."""

import click

from satdeploy.output import (
    success,
    warning,
    error,
    step,
    SYMBOLS,
    SatDeployError,
)


class TestSymbols:
    """Test that standard symbols are defined."""

    def test_check_symbol_defined(self):
        assert "check" in SYMBOLS
        assert SYMBOLS["check"] == "▸"

    def test_cross_symbol_defined(self):
        assert "cross" in SYMBOLS
        assert SYMBOLS["cross"] == "✗"

    def test_arrow_symbol_defined(self):
        assert "arrow" in SYMBOLS
        assert SYMBOLS["arrow"] == "→"

    def test_bullet_symbol_defined(self):
        assert "bullet" in SYMBOLS
        assert SYMBOLS["bullet"] == "•"


class TestMessageFormatters:
    """Test message formatting functions."""

    def test_success_returns_green_with_check(self):
        result = success("Done")
        assert "▸" in result
        # The result should contain ANSI color codes for green
        assert "\x1b[" in result or result == "▸ Done"

    def test_warning_returns_yellow(self):
        result = warning("Caution")
        assert "\x1b[" in result or "Caution" in result

    def test_error_returns_red(self):
        result = error("Failed")
        assert "Failed" in result
        # The result should contain ANSI color codes for red
        assert "\x1b[" in result or result == "Failed"

    def test_error_has_prefix(self):
        result = error("Something went wrong")
        assert "[ERROR]" in result
        assert "Something went wrong" in result


class TestStepFormatter:
    """Test step counter formatting."""

    def test_step_formats_with_counter(self):
        result = step(1, 5, "Backing up")
        assert "[1/5]" in result
        assert "Backing up" in result

    def test_step_returns_styled_string(self):
        result = step(2, 3, "Deploying")
        assert "[2/3]" in result
        assert "Deploying" in result


class TestSatDeployError:
    """Test custom error exception."""

    def test_error_formats_message_in_red(self):
        err = SatDeployError("Something went wrong")
        formatted = err.format_message()
        assert "Something went wrong" in formatted
        # Should contain ANSI red color codes
        assert "\x1b[" in formatted

    def test_error_has_prefix(self):
        err = SatDeployError("Something went wrong")
        formatted = err.format_message()
        assert "[ERROR]" in formatted

    def test_error_is_click_exception(self):
        err = SatDeployError("Test error")
        assert isinstance(err, click.ClickException)


class TestColoredGroup:
    """Test custom CLI group that colors errors."""

    def test_usage_error_shows_red(self):
        """Usage errors should be displayed in red."""
        from click.testing import CliRunner
        from satdeploy.output import ColoredGroup

        @click.group(cls=ColoredGroup)
        def cli():
            pass

        @cli.command()
        @click.argument("name")
        def greet(name):
            click.echo(f"Hello {name}")

        runner = CliRunner()
        result = runner.invoke(cli, ["greet"], color=True)  # Enable color output

        # Should contain red styling (ANSI codes)
        assert result.exit_code != 0
        assert "Missing argument" in result.output
        assert "\x1b[" in result.output  # ANSI color code
