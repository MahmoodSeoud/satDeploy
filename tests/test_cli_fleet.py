"""Tests for the satdeploy fleet commands."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main


class TestFleetCommandGroup:
    """Test the fleet command group exists."""

    def test_fleet_command_exists(self):
        """The fleet command group should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["fleet", "--help"])
        assert result.exit_code == 0
        assert "fleet" in result.output.lower()


class TestFleetStatusCommand:
    """Test the fleet status command."""

    def test_fleet_status_command_exists(self):
        """The fleet status command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["fleet", "status", "--help"])
        assert result.exit_code == 0
