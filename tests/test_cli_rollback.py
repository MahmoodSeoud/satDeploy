"""Tests for the satdeploy rollback command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS


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
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_rollback_fails_for_unknown_app(self, tmp_path):
        """Rollback should fail if app is not in config."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        result = runner.invoke(
            main,
            ["rollback", "unknown_app", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n20240114-091500-def67890.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "rolled back" in result.output.lower() or "restored" in result.output.lower()
        # Should show formatted timestamp
        assert "2024-01-15 14:30:22" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_accepts_hash_argument(self, mock_ssh_class, tmp_path):
        """Rollback should accept optional hash argument."""
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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n20240114-091500-def67890.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "def67890", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        # Should show formatted timestamp for the specific hash
        assert "2024-01-14 09:15:00" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_fails_when_no_backups(self, mock_ssh_class, tmp_path):
        """Rollback should fail if no backups exist."""
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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(stdout="", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0
        assert "no backup" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_fails_when_hash_not_found(self, mock_ssh_class, tmp_path):
        """Rollback should fail if specified hash doesn't exist."""
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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "zzzzzzzz", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
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
                make_config({
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
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "csp_server", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert "20240115-143022" in records[0].backup_path

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_stores_hash_not_version_string(self, mock_ssh_class, tmp_path):
        """Rollback should store just the hash in binary_hash, not the full version string."""
        from satdeploy.history import History

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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        # Use new format with hash in filename
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        # binary_hash should be just the hash, not the full version string
        assert records[0].binary_hash == "abc12345"
        assert records[0].binary_hash != "20240115-143022-abc12345"

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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        # First call returns backups, second call fails
        mock_ssh.run.side_effect = [
            Mock(stdout="20240115-143022-abc12345.bak\n", stderr="", exit_code=0),
            SSHError("Permission denied"),
        ]

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].success is False
        assert "Permission denied" in records[0].error_message


class TestRollbackPolishedOutput:
    """Tests for polished CLI output formatting."""

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_shows_step_counters(self, mock_ssh_class, tmp_path):
        """Rollback should show step counters like [1/4]."""
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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        assert "[1/" in result.output
        assert "[2/" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_success_shows_checkmark(self, mock_ssh_class, tmp_path):
        """Successful rollback should show checkmark symbol."""
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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["check"] in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_shows_formatted_hash_and_timestamp(self, mock_ssh_class, tmp_path):
        """Rollback should show formatted hash and timestamp, not raw version string."""
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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        # Should show formatted hash and timestamp
        assert "abc12345" in result.output
        assert "2024-01-15 14:30:22" in result.output
        # Should NOT show the raw version string format
        assert "20240115-143022-abc12345" not in result.output


class TestRollbackDialBehavior:
    """Tests for rollback dial behavior - each rollback goes one step back."""

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_goes_to_next_older_version_not_newest(self, mock_ssh_class, tmp_path):
        """Rollback should go to next older version, not ping-pong to newest.

        Given backups [C, B, A] (newest first) and B is currently deployed,
        rollback should go to A (older than B), NOT C (newer than B).
        """
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
                    }
                })
            )
        )

        # Set up history: B is currently deployed
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="bbbbbbbb",
            remote_path="/opt/disco/bin/controller",
            action="rollback",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        # Backups: C (newest), B (current), A (oldest)
        mock_ssh.run.return_value = Mock(
            stdout="20240117-120000-cccccccc.bak\n20240116-120000-bbbbbbbb.bak\n20240115-120000-aaaaaaaa.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        # Should rollback to A (next older than B), NOT C (newer than B)
        assert "aaaaaaaa" in result.output
        assert "cccccccc" not in result.output or "Rolled back" not in result.output.split("cccccccc")[0]

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_stops_at_oldest_version(self, mock_ssh_class, tmp_path):
        """Rollback should error when already at oldest version, not wrap."""
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
                    }
                })
            )
        )

        # Set up history: A (oldest) is currently deployed
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="aaaaaaaa",
            remote_path="/opt/disco/bin/controller",
            action="rollback",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        # Backups: C (newest), B, A (oldest/current)
        mock_ssh.run.return_value = Mock(
            stdout="20240117-120000-cccccccc.bak\n20240116-120000-bbbbbbbb.bak\n20240115-120000-aaaaaaaa.bak\n",
            stderr="",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["rollback", "controller", "--config", str(config_dir / "config.yaml")],
        )

        # Should warn (not error) because we're at the oldest version
        assert result.exit_code == 0
        assert "oldest" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_explicit_hash_ignores_dial(self, mock_ssh_class, tmp_path):
        """Explicit hash argument should work regardless of dial position."""
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
                    }
                })
            )
        )

        # Set up history: A (oldest) is currently deployed
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="aaaaaaaa",
            remote_path="/opt/disco/bin/controller",
            action="rollback",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240117-120000-cccccccc.bak\n20240116-120000-bbbbbbbb.bak\n20240115-120000-aaaaaaaa.bak\n",
            stderr="",
            exit_code=0,
        )

        # Explicitly request C by hash even though we're at A (oldest)
        result = runner.invoke(
            main,
            ["rollback", "controller", "cccccccc", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "cccccccc" in result.output


class TestRollbackByHash:
    """Tests for rollback by hash prefix."""

    @patch("satdeploy.cli.SSHClient")
    def test_rollback_by_hash_finds_correct_backup(self, mock_ssh_class, tmp_path):
        """Rollback by hash prefix should find the matching backup."""
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
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.run.return_value = Mock(
            stdout="20240117-120000-cccccccc.bak\n20240116-120000-bbbbbbbb.bak\n20240115-120000-aaaaaaaa.bak\n",
            stderr="",
            exit_code=0,
        )

        # Rollback by hash only (not full version string)
        result = runner.invoke(
            main,
            ["rollback", "controller", "bbbbbbbb", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "bbbbbbbb" in result.output
        assert "2024-01-16 12:00:00" in result.output
