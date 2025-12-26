"""Tests for the satdeploy list command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS


class TestListCommand:
    """Test the list command."""

    def test_list_command_exists(self):
        """The list command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["list", "--help"])
        assert result.exit_code == 0
        assert "app" in result.output.lower()

    def test_list_requires_app_name(self):
        """List should require an app name argument."""
        runner = CliRunner()
        result = runner.invoke(main, ["list"])
        assert result.exit_code != 0
        assert "app" in result.output.lower() or "missing" in result.output.lower()

    def test_list_fails_without_config(self, tmp_path):
        """List should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["list", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_list_fails_for_unknown_app(self, tmp_path):
        """List should fail if app is not in config."""
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

        result = runner.invoke(
            main,
            ["list", "unknown_app", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "unknown_app" in result.output or "not found" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_backups(self, mock_ssh_class, tmp_path):
        """List should display available backups."""
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
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n20240114-091500.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "20240115-143022" in result.output
        assert "20240114-091500" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_timestamps(self, mock_ssh_class, tmp_path):
        """List should display human-readable timestamps."""
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
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "2024-01-15" in result.output
        assert "14:30:22" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_message_when_no_backups(self, mock_ssh_class, tmp_path):
        """List should show message when no backups exist."""
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
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="", exit_code=0)

        result = runner.invoke(
            main,
            ["list", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "no backup" in result.output.lower()


class TestListPolishedOutput:
    """Tests for polished CLI output formatting."""

    @patch("satdeploy.cli.SSHClient")
    def test_list_uses_bullet_for_entries(self, mock_ssh_class, tmp_path):
        """List should show bullet for each backup entry."""
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
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n20240114-091500.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config-dir", str(config_dir)],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["bullet"] in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_header_line(self, mock_ssh_class, tmp_path):
        """List should show a header line with backups for app."""
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
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config-dir", str(config_dir)],
            color=True,
        )

        assert result.exit_code == 0
        # Should have a styled header
        assert "controller" in result.output.lower()
        assert "backup" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_current_version_marker(self, mock_ssh_class, tmp_path):
        """List should show marker for currently deployed version."""
        from satdeploy.history import History, DeploymentRecord

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
                        }
                    },
                }
            )
        )

        # Add a deployment to history with matching version
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            app="controller",
            binary_hash="a3f2c9b1",
            remote_path="/opt/disco/bin/controller",
            backup_path="/opt/satdeploy/backups/controller/20240115-143022.bak",
            action="push",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        # Return backups including the current one
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n20240114-091500.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config-dir", str(config_dir)],
            color=True,
        )

        assert result.exit_code == 0
        # Should show "current" marker for the deployed version
        assert "current" in result.output.lower()
