"""Tests for the satdeploy status command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.services import ServiceStatus


class TestStatusCommand:
    """Test the status command."""

    def test_status_command_exists(self):
        """The status command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_status_fails_without_config(self, tmp_path):
        """Status should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_status_shows_target_info(self, mock_ssh_class, tmp_path):
        """Status should show target host information."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {},
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        result = runner.invoke(
            main,
            ["status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "192.168.1.50" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_status_shows_app_list(self, mock_ssh_class, tmp_path):
        """Status should show all configured apps."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "controller": {
                            "local": "./build/controller",
                            "remote": "/opt/disco/bin/controller",
                            "service": "controller.service",
                        },
                        "csp_server": {
                            "local": "./build/csp_server",
                            "remote": "/usr/bin/csp_server",
                            "service": "csp_server.service",
                        },
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "controller" in result.output
        assert "csp_server" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_status_shows_service_state(self, mock_ssh_class, tmp_path):
        """Status should show service state for each app."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "controller": {
                            "local": "./build/controller",
                            "remote": "/opt/disco/bin/controller",
                            "service": "controller.service",
                        },
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "running" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_status_shows_stopped_service(self, mock_ssh_class, tmp_path):
        """Status should show stopped state for inactive services."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "controller": {
                            "local": "./build/controller",
                            "remote": "/opt/disco/bin/controller",
                            "service": "controller.service",
                        },
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="inactive\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_status_handles_library_without_service(self, mock_ssh_class, tmp_path):
        """Status should handle apps without a service (libraries)."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "libparam": {
                            "local": "./build/libparam.so",
                            "remote": "/usr/lib/libparam.so",
                            "service": None,
                        },
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = True

        result = runner.invoke(
            main,
            ["status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "libparam" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_status_shows_empty_message_when_no_apps(self, mock_ssh_class, tmp_path):
        """Status should show message when no apps are configured."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {},
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        result = runner.invoke(
            main,
            ["status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "no apps" in result.output.lower()
