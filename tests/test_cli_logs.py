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
name: som1
transport: ssh
host: 192.168.1.50
user: root
csp_addr: 5421
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
        return config_file

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
        result = runner.invoke(main, ["logs", "controller", "--config", str(config_dir / "config.yaml")])
        assert result.exit_code != 0
        assert "config" in result.output.lower() or "not found" in result.output.lower()

    def test_logs_fails_for_unknown_app(self, runner, config_with_app):
        """Logs command fails for unknown app."""
        result = runner.invoke(main, ["logs", "unknown", "--config", str(config_with_app)])
        assert result.exit_code != 0
        assert "unknown" in result.output.lower() or "not found" in result.output.lower()

    def test_logs_fails_for_app_without_service(self, runner, config_with_app, mocker):
        """Logs command fails for apps without a service (libraries)."""
        result = runner.invoke(main, ["logs", "libparam", "--config", str(config_with_app)])
        assert result.exit_code != 0
        assert "service" in result.output.lower() or "library" in result.output.lower()

    def test_logs_shows_journalctl_output(self, runner, config_with_app, mocker):
        """Logs command shows output from journalctl."""
        mock_transport = mocker.MagicMock()
        mock_transport.get_logs.return_value = "Dec 26 10:00:00 flatsat controller[1234]: Starting up\nDec 26 10:00:01 flatsat controller[1234]: Ready"

        mocker.patch("satdeploy.cli.get_transport", return_value=mock_transport)

        result = runner.invoke(main, ["logs", "controller", "--config", str(config_with_app)])
        assert result.exit_code == 0
        assert "Starting up" in result.output
        assert "Ready" in result.output

    def test_logs_accepts_lines_option(self, runner, config_with_app, mocker):
        """Logs command accepts --lines option."""
        mock_transport = mocker.MagicMock()
        mock_transport.get_logs.return_value = "log line"

        mocker.patch("satdeploy.cli.get_transport", return_value=mock_transport)

        result = runner.invoke(main, ["logs", "controller", "--lines", "50", "--config", str(config_with_app)])
        assert result.exit_code == 0
        mock_transport.get_logs.assert_called_once_with("controller", "controller.service", lines=50)

    def test_logs_default_lines_is_100(self, runner, config_with_app, mocker):
        """Logs command defaults to 100 lines."""
        mock_transport = mocker.MagicMock()
        mock_transport.get_logs.return_value = "log line"

        mocker.patch("satdeploy.cli.get_transport", return_value=mock_transport)

        result = runner.invoke(main, ["logs", "controller", "--config", str(config_with_app)])
        assert result.exit_code == 0
        mock_transport.get_logs.assert_called_once_with("controller", "controller.service", lines=100)


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
name: som1
transport: ssh
host: 192.168.1.50
user: root
csp_addr: 5421
backup_dir: /opt/satdeploy/backups
max_backups: 10
apps:
  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
""")
        return config_file

    def test_logs_shows_header(self, runner, config_with_app, mocker):
        """Logs command should show a header with app name."""
        mock_transport = mocker.MagicMock()
        mock_transport.get_logs.return_value = "Dec 26 10:00:00 flatsat controller[1234]: Starting up"

        mocker.patch("satdeploy.cli.get_transport", return_value=mock_transport)

        result = runner.invoke(
            main,
            ["logs", "controller", "--config", str(config_with_app)],
            color=True,
        )

        assert result.exit_code == 0
        assert "controller" in result.output
