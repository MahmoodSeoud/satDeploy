"""Tests for the logs CLI command."""

import pytest
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS


class TestLogsCommand:
    """Tests for satdeploy logs."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def config_with_app(self, tmp_path):
        """Create a config file with an app that has a service."""
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text("""
target:
  host: 192.168.1.50
  user: root
backup_dir: /opt/satdeploy/backups
max_backups: 10
apps:
  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
  libparam:
    local: ./build/libparam.so
    remote: /usr/lib/libparam.so
    service: null
""")
        return config_dir

    def test_logs_command_exists(self, runner):
        """The logs command should exist."""
        result = runner.invoke(main, ["logs", "--help"])
        assert result.exit_code == 0
        assert "logs" in result.output.lower()

    def test_logs_requires_app_name(self, runner):
        """The logs command requires an app name."""
        result = runner.invoke(main, ["logs"])
        assert result.exit_code != 0
        assert "app" in result.output.lower() or "missing" in result.output.lower()

    def test_logs_fails_without_config(self, runner, tmp_path):
        """Logs command fails if no config exists."""
        config_dir = tmp_path / ".satdeploy"
        result = runner.invoke(main, ["logs", "controller", "--config-dir", str(config_dir)])
        assert result.exit_code != 0
        assert "config" in result.output.lower() or "not found" in result.output.lower()

    def test_logs_fails_for_unknown_app(self, runner, config_with_app):
        """Logs command fails for unknown app."""
        result = runner.invoke(main, ["logs", "unknown", "--config-dir", str(config_with_app)])
        assert result.exit_code != 0
        assert "unknown" in result.output.lower() or "not found" in result.output.lower()

    def test_logs_fails_for_app_without_service(self, runner, config_with_app, mocker):
        """Logs command fails for apps without a service (libraries)."""
        result = runner.invoke(main, ["logs", "libparam", "--config-dir", str(config_with_app)])
        assert result.exit_code != 0
        assert "service" in result.output.lower() or "library" in result.output.lower()

    def test_logs_shows_journalctl_output(self, runner, config_with_app, mocker):
        """Logs command shows output from journalctl."""
        mock_ssh = mocker.MagicMock()
        mock_ssh.__enter__ = mocker.MagicMock(return_value=mock_ssh)
        mock_ssh.__exit__ = mocker.MagicMock(return_value=False)
        mock_ssh.run.return_value.stdout = "Dec 26 10:00:00 flatsat controller[1234]: Starting up\nDec 26 10:00:01 flatsat controller[1234]: Ready"

        mocker.patch("satdeploy.cli.SSHClient", return_value=mock_ssh)

        result = runner.invoke(main, ["logs", "controller", "--config-dir", str(config_with_app)])
        assert result.exit_code == 0
        assert "Starting up" in result.output
        assert "Ready" in result.output

    def test_logs_accepts_lines_option(self, runner, config_with_app, mocker):
        """Logs command accepts --lines option."""
        mock_ssh = mocker.MagicMock()
        mock_ssh.__enter__ = mocker.MagicMock(return_value=mock_ssh)
        mock_ssh.__exit__ = mocker.MagicMock(return_value=False)
        mock_ssh.run.return_value.stdout = "log line"

        mocker.patch("satdeploy.cli.SSHClient", return_value=mock_ssh)

        result = runner.invoke(main, ["logs", "controller", "--lines", "50", "--config-dir", str(config_with_app)])
        assert result.exit_code == 0
        # Verify journalctl was called with correct number of lines
        mock_ssh.run.assert_called()
        call_args = str(mock_ssh.run.call_args)
        assert "-n 50" in call_args or "-n50" in call_args

    def test_logs_default_lines_is_100(self, runner, config_with_app, mocker):
        """Logs command defaults to 100 lines."""
        mock_ssh = mocker.MagicMock()
        mock_ssh.__enter__ = mocker.MagicMock(return_value=mock_ssh)
        mock_ssh.__exit__ = mocker.MagicMock(return_value=False)
        mock_ssh.run.return_value.stdout = "log line"

        mocker.patch("satdeploy.cli.SSHClient", return_value=mock_ssh)

        result = runner.invoke(main, ["logs", "controller", "--config-dir", str(config_with_app)])
        assert result.exit_code == 0
        # Verify journalctl was called with 100 lines
        mock_ssh.run.assert_called()
        call_args = str(mock_ssh.run.call_args)
        assert "-n 100" in call_args or "-n100" in call_args


class TestLogsPolishedOutput:
    """Tests for polished CLI output formatting."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def config_with_app(self, tmp_path):
        """Create a config file with an app that has a service."""
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text("""
target:
  host: 192.168.1.50
  user: root
backup_dir: /opt/satdeploy/backups
max_backups: 10
apps:
  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
""")
        return config_dir

    def test_logs_shows_header(self, runner, config_with_app, mocker):
        """Logs command should show a header with app name."""
        mock_ssh = mocker.MagicMock()
        mock_ssh.__enter__ = mocker.MagicMock(return_value=mock_ssh)
        mock_ssh.__exit__ = mocker.MagicMock(return_value=False)
        mock_ssh.run.return_value.stdout = "Dec 26 10:00:00 flatsat controller[1234]: Starting up"

        mocker.patch("satdeploy.cli.SSHClient", return_value=mock_ssh)

        result = runner.invoke(
            main,
            ["logs", "controller", "--config-dir", str(config_with_app)],
            color=True,
        )

        assert result.exit_code == 0
        assert "controller" in result.output
