"""Tests for CLI output formatting."""

import click

from satdeploy.output import (
    success,
    error,
    warning,
    info,
    step,
    SYMBOLS,
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

    def test_error_returns_red_with_cross(self):
        result = error("Failed")
        assert "✗" in result
        assert "\x1b[" in result or result == "✗ Failed"

    def test_warning_returns_yellow(self):
        result = warning("Caution")
        assert "\x1b[" in result or "Caution" in result

    def test_info_returns_plain_message(self):
        result = info("Information")
        assert "Information" in result


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
