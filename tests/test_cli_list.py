"""Tests for the satdeploy list command."""

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
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_list_fails_for_unknown_app(self, tmp_path):
        """List should fail if app is not in config."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        result = runner.invoke(
            main,
            ["list", "unknown_app", "--config", str(config_dir / "config.yaml")],
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
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        # Check for human-readable timestamps (we no longer show raw version format)
        assert "2024-01-15 14:30:22" in result.output
        assert "2024-01-14 09:15:00" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_timestamps(self, mock_ssh_class, tmp_path):
        """List should display human-readable timestamps."""
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
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "2024-01-15" in result.output
        assert "14:30:22" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_message_when_no_versions(self, mock_ssh_class, tmp_path):
        """List should show message when no versions exist."""
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
        mock_ssh.run.return_value = Mock(stdout="", exit_code=0)

        result = runner.invoke(
            main,
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "no versions" in result.output.lower()


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
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
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
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
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
                make_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        # Add a deployment to history with matching version
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            module="som1",
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
        # Return backups including the current one (with hash in filename)
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-a3f2c9b1.bak\n20240114-091500-b7e1d2a4.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        # Should show arrow symbol for the currently deployed version
        assert SYMBOLS["arrow"] in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_list_sorts_by_timestamp_newest_first(self, mock_ssh_class, tmp_path):
        """List should sort versions by timestamp, newest first."""
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

        # Add a deployment to history with a hash not in backups (newest)
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="newest11",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        # Return older backups
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-older111.bak\n20240114-091500-oldest11.bak\n",
            exit_code=0,
        )

        result = runner.invoke(
            main,
            ["list", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        # Find positions of timestamps in output
        output = result.output
        pos_newest = output.find("newest11")
        pos_older = output.find("older111")
        pos_oldest = output.find("oldest11")

        # All three should be present and in order (newest first)
        assert pos_newest != -1, "newest11 not found in output"
        assert pos_older != -1, "older111 not found in output"
        assert pos_oldest != -1, "oldest11 not found in output"
        assert pos_newest < pos_older < pos_oldest, \
            f"Versions not in timestamp order: newest={pos_newest}, older={pos_older}, oldest={pos_oldest}"
