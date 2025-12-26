"""Tests for the satdeploy push command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main


class TestPushCommand:
    """Test the push command."""

    def test_push_command_exists(self):
        """The push command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["push", "--help"])
        assert result.exit_code == 0
        assert "app" in result.output.lower()

    def test_push_requires_app_name(self):
        """Push should require an app name argument."""
        runner = CliRunner()
        result = runner.invoke(main, ["push"])
        assert result.exit_code != 0
        assert "app" in result.output.lower() or "missing" in result.output.lower()

    def test_push_fails_without_config(self, tmp_path):
        """Push should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["push", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_push_fails_for_unknown_app(self, tmp_path):
        """Push should fail if app is not in config."""
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
            ["push", "unknown_app", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "unknown_app" in result.output or "not found" in result.output.lower()

    def test_push_fails_if_local_file_missing(self, tmp_path):
        """Push should fail if local binary doesn't exist."""
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
                            "local": "/nonexistent/path/controller",
                            "remote": "/opt/disco/bin/controller",
                            "service": "controller.service",
                        }
                    },
                }
            )
        )

        result = runner.invoke(
            main,
            ["push", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "exist" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_push_connects_to_target(self, mock_ssh_class, tmp_path):
        """Push should connect to the configured target."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "controller": {
                            "local": str(binary),
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
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "--config-dir", str(config_dir)],
        )

        mock_ssh_class.assert_called_once_with(host="192.168.1.50", user="root")

    @patch("satdeploy.cli.SSHClient")
    def test_push_shows_success_message(self, mock_ssh_class, tmp_path):
        """Push should show success message on completion."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "controller": {
                            "local": str(binary),
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
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "deployed" in result.output.lower() or "success" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_push_with_local_override(self, mock_ssh_class, tmp_path):
        """Push should allow overriding local path with --local."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "custom_controller"
        binary.write_bytes(b"custom binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "controller": {
                            "local": "/default/path/controller",
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
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            [
                "push",
                "controller",
                "--local",
                str(binary),
                "--config-dir",
                str(config_dir),
            ],
        )

        assert result.exit_code == 0
        mock_ssh.upload.assert_called_once()
        assert str(binary) in str(mock_ssh.upload.call_args)

    @patch("satdeploy.cli.SSHClient")
    def test_push_expands_tilde_in_local_path(self, mock_ssh_class, tmp_path, monkeypatch):
        """Push should expand ~ in local path from config."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        # Create binary in a fake home directory
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        binary = fake_home / "build" / "controller"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"binary content")

        # Make ~ expand to our fake home
        monkeypatch.setenv("HOME", str(fake_home))

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "controller": {
                            "local": "~/build/controller",  # Uses tilde
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
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0, f"Failed with: {result.output}"
        mock_ssh.upload.assert_called_once()
        # The expanded path should be used
        assert str(fake_home) in str(mock_ssh.upload.call_args)
