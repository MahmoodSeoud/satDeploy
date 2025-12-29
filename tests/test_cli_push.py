"""Tests for the satdeploy push command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS


def make_module_config(apps: dict, backup_dir: str = "/opt/satdeploy/backups") -> dict:
    """Create a module-based config for testing."""
    return {
        "modules": {
            "som1": {
                "host": "192.168.1.50",
                "user": "root",
                "csp_addr": 5421,
            }
        },
        "appsys": {},
        "backup_dir": backup_dir,
        "apps": apps,
    }


class TestPushCommand:
    """Test the push command."""

    def test_push_command_exists(self):
        """The push command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["push", "--help"])
        assert result.exit_code == 0
        assert "app" in result.output.lower()

    def test_push_requires_module(self):
        """Push should require --module option."""
        runner = CliRunner()
        result = runner.invoke(main, ["push", "controller"])
        assert result.exit_code != 0
        assert "module" in result.output.lower()

    def test_push_fails_without_config(self, tmp_path):
        """Push should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_push_fails_for_unknown_app(self, tmp_path):
        """Push should fail if app is not in config."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_module_config({})))

        result = runner.invoke(
            main,
            ["push", "unknown_app", "-m", "som1", "--config-dir", str(config_dir)],
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
                make_module_config({
                    "controller": {
                        "local": "/nonexistent/path/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "exist" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_push_connects_to_target(self, mock_ssh_class, tmp_path):
        """Push should connect to the configured module."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
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
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
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
                make_module_config({
                    "controller": {
                        "local": "/default/path/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            [
                "push",
                "controller",
                "-m", "som1",
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
                make_module_config({
                    "controller": {
                        "local": "~/build/controller",  # Uses tilde
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0, f"Failed with: {result.output}"
        mock_ssh.upload.assert_called_once()
        # The expanded path should be used
        assert str(fake_home) in str(mock_ssh.upload.call_args)


class TestPushWithDependencies:
    """Test push with dependency-aware service management."""

    @patch("satdeploy.cli.SSHClient")
    def test_push_stops_dependents_first(self, mock_ssh_class, tmp_path):
        """Push should stop dependent services before the target."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "csp_server"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                        "depends_on": ["csp_server"],
                    },
                    "csp_server": {
                        "local": str(binary),
                        "remote": "/usr/bin/csp_server",
                        "service": "csp_server.service",
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "csp_server", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        # Should mention stopping controller (the dependent)
        assert "controller" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_push_starts_in_correct_order(self, mock_ssh_class, tmp_path):
        """Push should start services in correct dependency order."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "csp_server"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": "./build/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                        "depends_on": ["csp_server"],
                    },
                    "csp_server": {
                        "local": str(binary),
                        "remote": "/usr/bin/csp_server",
                        "service": "csp_server.service",
                    },
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "csp_server", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        # Output should show stopping controller, then csp_server
        # And starting csp_server, then controller
        output_lower = result.output.lower()
        stop_controller = output_lower.find("stopping controller")
        stop_csp = output_lower.find("stopping csp_server")
        start_csp = output_lower.find("starting csp_server")
        start_controller = output_lower.find("starting controller")

        # Stop order: controller first (dependent), then csp_server
        if stop_controller != -1 and stop_csp != -1:
            assert stop_controller < stop_csp, "controller should stop before csp_server"
        # Start order: csp_server first, then controller (dependent)
        if start_csp != -1 and start_controller != -1:
            assert start_csp < start_controller, "csp_server should start before controller"

    @patch("satdeploy.cli.SSHClient")
    def test_push_library_restarts_dependent_services(self, mock_ssh_class, tmp_path):
        """Push for library should restart services in restart list."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        lib = tmp_path / "libparam.so"
        lib.write_bytes(b"library content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "libparam": {
                        "local": str(lib),
                        "remote": "/usr/lib/libparam.so",
                        "service": None,
                        "restart": ["csp_server", "controller"],
                    },
                    "csp_server": {
                        "local": "./build/csp_server",
                        "remote": "/usr/bin/csp_server",
                        "service": "csp_server.service",
                    },
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
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "libparam", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        # Should restart both services
        output_lower = result.output.lower()
        assert "csp_server" in output_lower
        assert "controller" in output_lower

    @patch("satdeploy.cli.SSHClient")
    def test_push_handles_ssh_error_gracefully(self, mock_ssh_class, tmp_path):
        """Push should show clean error message on SSH failure, not traceback."""
        from satdeploy.ssh import SSHError

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": None,
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        # Simulate permission denied error
        mock_ssh.run.side_effect = SSHError("mkdir: cannot create directory '/opt/satdeploy': Permission denied")

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
        )

        # Should fail with clean error, not traceback
        assert result.exit_code != 0
        assert "permission denied" in result.output.lower()
        assert "Traceback" not in result.output


class TestPushHistoryLogging:
    """Tests for deployment history logging on push."""

    @patch("satdeploy.cli.SSHClient")
    def test_push_logs_successful_deployment(self, mock_ssh_class, tmp_path):
        """Successful push should be recorded in history database."""
        from satdeploy.history import History

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0

        # Check history was recorded
        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].app == "controller"
        assert records[0].action == "push"
        assert records[0].success is True
        assert records[0].remote_path == "/opt/disco/bin/controller"
        assert records[0].module == "som1"

    @patch("satdeploy.cli.SSHClient")
    def test_push_logs_binary_hash(self, mock_ssh_class, tmp_path):
        """Push should record the binary hash in history."""
        from satdeploy.history import History

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert records[0].binary_hash is not None
        assert len(records[0].binary_hash) == 8  # First 8 chars of SHA256

    @patch("satdeploy.cli.SSHClient")
    def test_push_logs_failed_deployment(self, mock_ssh_class, tmp_path):
        """Failed push should be recorded in history with error message."""
        from satdeploy.history import History
        from satdeploy.ssh import SSHError

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": None,
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.side_effect = SSHError("Connection refused")

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].success is False
        assert "Connection refused" in records[0].error_message


class TestPushPolishedOutput:
    """Tests for polished CLI output formatting."""

    @patch("satdeploy.cli.SSHClient")
    def test_push_shows_step_counters(self, mock_ssh_class, tmp_path):
        """Push should show step counters like [1/5]."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
            color=True,
        )

        assert result.exit_code == 0
        # Should have step counters in output
        assert "[1/" in result.output
        assert "[2/" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_push_success_shows_checkmark(self, mock_ssh_class, tmp_path):
        """Successful push should show checkmark symbol."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["check"] in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_push_shows_arrow_for_upload(self, mock_ssh_class, tmp_path):
        """Push should show arrow symbol for file transfer."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_module_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(stdout="active\n", stderr="", exit_code=0)

        result = runner.invoke(
            main,
            ["push", "controller", "-m", "som1", "--config-dir", str(config_dir)],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["arrow"] in result.output
