"""Tests for the satdeploy rollback command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main


class TestRollbackCommand:
    """Test the rollback command."""

    def test_rollback_command_exists(self):
        """The rollback command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["rollback", "--help"])
        assert result.exit_code == 0
        assert "app" in result.output.lower()

    def test_rollback_requires_app_name(self):
        """Rollback should require an app name argument."""
        runner = CliRunner()
        result = runner.invoke(main, ["rollback"])
        assert result.exit_code != 0
        assert "app" in result.output.lower() or "missing" in result.output.lower()

    def test_rollback_fails_without_config(self, tmp_path):
        """Rollback should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_rollback_fails_for_unknown_app(self, tmp_path):
        """Rollback should fail if app is not in config."""
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
            ["rollback", "unknown_app", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "unknown_app" in result.output or "not found" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_restores_latest_backup(self, mock_ssh_class, tmp_path):
        """Rollback should restore the most recent backup."""
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
            ["rollback", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "rolled back" in result.output.lower() or "restored" in result.output.lower()
        assert "20240115-143022" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_accepts_version_argument(self, mock_ssh_class, tmp_path):
        """Rollback should accept optional version argument."""
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
            ["rollback", "controller", "20240114-091500", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "20240114-091500" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_fails_when_no_backups(self, mock_ssh_class, tmp_path):
        """Rollback should fail if no backups exist."""
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
            ["rollback", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "no backup" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_fails_when_version_not_found(self, mock_ssh_class, tmp_path):
        """Rollback should fail if specified version doesn't exist."""
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
            ["rollback", "controller", "20240101-000000", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_shows_health_check_status(self, mock_ssh_class, tmp_path):
        """Rollback should show health check status."""
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
            ["rollback", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "health check" in result.output.lower()


class TestRollbackWithDependencies:
    """Test rollback with dependency-aware service management."""

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_stops_dependents_first(self, mock_ssh_class, tmp_path):
        """Rollback should stop dependent services before the target."""
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
                            "depends_on": ["csp_server"],
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
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "csp_server", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        # Should mention stopping controller (the dependent)
        assert "controller" in result.output.lower()


class TestRollbackHistoryLogging:
    """Tests for deployment history logging on rollback."""

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_logs_successful_operation(self, mock_ssh_class, tmp_path):
        """Successful rollback should be recorded in history database."""
        from satdeploy.history import History

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
            ["rollback", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0

        # Check history was recorded
        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].app == "controller"
        assert records[0].action == "rollback"
        assert records[0].success is True
        assert records[0].remote_path == "/opt/disco/bin/controller"

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_logs_backup_path(self, mock_ssh_class, tmp_path):
        """Rollback should record the backup path used."""
        from satdeploy.history import History

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
            ["rollback", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert "20240115-143022" in records[0].backup_path

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_logs_failed_operation(self, mock_ssh_class, tmp_path):
        """Failed rollback should be recorded in history with error message."""
        from satdeploy.history import History
        from satdeploy.ssh import SSHError

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
        # First call returns backups, second call fails
        mock_ssh.run.side_effect = [
            Mock(stdout="20240115-143022.bak\n", exit_code=0),
            SSHError("Permission denied"),
        ]

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].success is False
        assert "Permission denied" in records[0].error_message
