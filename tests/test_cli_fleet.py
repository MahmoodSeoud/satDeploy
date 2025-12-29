"""Tests for the satdeploy fleet commands."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS


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

    def test_fleet_status_fails_without_config(self, tmp_path):
        """Fleet status should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["fleet", "status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_fleet_status_shows_modules(self, mock_ssh_class, tmp_path):
        """Fleet status should show configured modules."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "modules": {
                        "som1": {
                            "host": "192.168.1.10",
                            "user": "root",
                            "csp_addr": 5421,
                        },
                        "som2": {
                            "host": "192.168.1.11",
                            "user": "root",
                            "csp_addr": 5475,
                        },
                    },
                    "appsys": {
                        "netmask": 8,
                        "interface": 0,
                        "baudrate": 100000,
                        "vmem_path": "/home/root/a53vmem",
                    },
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {},
                }
            )
        )

        # Mock SSH to always succeed
        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        result = runner.invoke(
            main,
            ["fleet", "status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "som1" in result.output
        assert "som2" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_fleet_status_shows_online_offline(self, mock_ssh_class, tmp_path):
        """Fleet status should show online/offline status for modules."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "modules": {
                        "som1": {
                            "host": "192.168.1.10",
                            "user": "root",
                            "csp_addr": 5421,
                        },
                        "som2": {
                            "host": "192.168.1.11",
                            "user": "root",
                            "csp_addr": 5475,
                        },
                    },
                    "appsys": {
                        "netmask": 8,
                        "interface": 0,
                        "baudrate": 100000,
                        "vmem_path": "/home/root/a53vmem",
                    },
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {},
                }
            )
        )

        # Make som1 online and som2 offline
        def ssh_side_effect(host, user, timeout=None):
            mock_ssh = MagicMock()
            if host == "192.168.1.10":
                mock_ssh.__enter__ = Mock(return_value=mock_ssh)
                mock_ssh.__exit__ = Mock(return_value=False)
                return mock_ssh
            else:
                from satdeploy.ssh import SSHError
                raise SSHError("Connection refused")

        mock_ssh_class.side_effect = ssh_side_effect

        result = runner.invoke(
            main,
            ["fleet", "status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "online" in result.output.lower() or SYMBOLS["check"] in result.output
        assert "offline" in result.output.lower() or SYMBOLS["cross"] in result.output
