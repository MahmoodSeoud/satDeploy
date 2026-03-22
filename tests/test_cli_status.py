"""Tests for the satdeploy status command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS
from satdeploy.services import ServiceStatus


def make_config(apps: dict) -> dict:
    """Create a flat config for testing."""
    return {
        "name": "som1",
        "transport": "ssh",
        "host": "192.168.1.50",
        "user": "root",
        "csp_addr": 5421,
        "backup_dir": "/opt/satdeploy/backups",
        "max_backups": 10,
        "apps": apps,
    }


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
            ["status", "--config", str(config_dir / "config.yaml")],
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
        config_file.write_text(yaml.dump(make_config({})))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
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
                make_config({
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
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="inactive\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "libparam": {
                        "local": "./build/libparam.so",
                        "remote": "/usr/lib/libparam.so",
                        "service": None,
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = True

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
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
        config_file.write_text(yaml.dump(make_config({})))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "no apps" in result.output.lower()


class TestStatusPolishedOutput:
    """Tests for polished CLI output formatting."""

    @patch("satdeploy.cli.SSHClient")
    def test_status_running_shows_checkmark(self, mock_ssh_class, tmp_path):
        """Status should show checkmark for running services."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["check"] in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_status_failed_shows_cross(self, mock_ssh_class, tmp_path):
        """Status should show cross for failed services."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="failed\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["cross"] in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_status_uses_bullet_for_library(self, mock_ssh_class, tmp_path):
        """Status should show bullet for libraries."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "libparam": {
                        "local": "./build/libparam.so",
                        "remote": "/usr/lib/libparam.so",
                        "service": None,
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = True

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["bullet"] in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_status_shows_version_from_history(self, mock_ssh_class, tmp_path):
        """Status should show version from deployment history."""
        from satdeploy.history import History, DeploymentRecord

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    },
                })
            )
        )

        # Add a deployment to history
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="a3f2c9b1",
            remote_path="/opt/disco/bin/controller",
            backup_path="/opt/satdeploy/backups/controller/20240115-143022-a3f2c9b1.bak",
            action="push",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["status", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        assert "a3f2c9b1" in result.output
