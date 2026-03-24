"""Tests for the satdeploy push command."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.history import History
from satdeploy.output import SYMBOLS
from satdeploy.transport.base import DeployResult


def make_config(apps: dict, backup_dir: str = "/opt/satdeploy/backups") -> dict:
    """Create a flat config for testing."""
    return {
        "name": "som1",
        "transport": "ssh",
        "host": "192.168.1.50",
        "user": "root",
        "csp_addr": 5421,
        "backup_dir": backup_dir,
        "apps": apps,
    }


def make_mock_transport(deploy_result=None):
    """Create a mock transport that returns the given deploy result."""
    transport = MagicMock()
    if deploy_result is None:
        deploy_result = DeployResult(
            success=True, file_hash="abcd1234", backup_path="/backups/test.bak"
        )
    transport.deploy.return_value = deploy_result
    transport.get_status.return_value = {}
    return transport


class TestPushCommand:
    """Test the push command."""

    def test_push_command_exists(self):
        """The push command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["push", "--help"])
        assert result.exit_code == 0
        assert "app" in result.output.lower()

    def test_push_fails_without_config(self, tmp_path):
        """Push should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_push_fails_for_unknown_app(self, tmp_path):
        """Push should fail if app is not in config."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        result = runner.invoke(
            main,
            ["push", "unknown_app", "--config", str(config_dir / "config.yaml")],
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
                make_config({
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
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "exist" in result.output.lower()

    @patch("satdeploy.cli.get_transport")
    def test_push_connects_to_target(self, mock_get_transport, tmp_path):
        """Push should connect to the configured target via transport."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        transport.connect.assert_called_once()
        transport.disconnect.assert_called_once()

    @patch("satdeploy.cli.get_transport")
    def test_push_shows_success_message(self, mock_get_transport, tmp_path):
        """Push should show success message on completion."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "deployed" in result.output.lower() or "success" in result.output.lower()

    @patch("satdeploy.cli.get_transport")
    def test_push_with_local_override(self, mock_get_transport, tmp_path):
        """Push should allow overriding local path with --local."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "custom_controller"
        binary.write_bytes(b"custom binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": "/default/path/controller",
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            [
                "push",
                "controller",
                "--local",
                str(binary),
                "--config",
                str(config_dir / "config.yaml"),
            ],
        )

        assert result.exit_code == 0
        transport.deploy.assert_called_once()
        assert str(binary) in str(transport.deploy.call_args)

    @patch("satdeploy.cli.get_transport")
    def test_push_expands_tilde_in_local_path(self, mock_get_transport, tmp_path, monkeypatch):
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
                make_config({
                    "controller": {
                        "local": "~/build/controller",  # Uses tilde
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0, f"Failed with: {result.output}"
        transport.deploy.assert_called_once()
        # The expanded path should be used
        assert str(fake_home) in str(transport.deploy.call_args)


class TestPushWithDependencies:
    """Test push with dependency-aware service management."""

    @patch("satdeploy.cli.get_transport")
    def test_push_passes_services_to_transport(self, mock_get_transport, tmp_path):
        """Push should pass dependency-resolved services to transport.deploy()."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "csp_server"
        binary.write_bytes(b"binary content")

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
                        "local": str(binary),
                        "remote": "/usr/bin/csp_server",
                        "service": "csp_server.service",
                    },
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "csp_server", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        # transport.deploy should be called with services list
        deploy_call = transport.deploy.call_args
        services = deploy_call.kwargs.get("services") or deploy_call[1].get("services")
        assert services is not None
        # Services should include controller (dependent of csp_server)
        service_names = [s[1] for s in services]
        assert "controller.service" in service_names
        assert "csp_server.service" in service_names

    @patch("satdeploy.cli.get_transport")
    def test_push_library_passes_restart_services(self, mock_get_transport, tmp_path):
        """Push for library should pass restart list services to transport."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        lib = tmp_path / "libparam.so"
        lib.write_bytes(b"library content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
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

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "libparam", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        deploy_call = transport.deploy.call_args
        services = deploy_call.kwargs.get("services") or deploy_call[1].get("services")
        assert services is not None
        service_names = [s[1] for s in services]
        assert "csp_server.service" in service_names
        assert "controller.service" in service_names

    @patch("satdeploy.cli.get_transport")
    def test_push_handles_transport_error_gracefully(self, mock_get_transport, tmp_path):
        """Push should show clean error message on transport failure."""
        from satdeploy.transport.base import TransportError

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": None,
                    }
                })
            )
        )

        transport = MagicMock()
        transport.connect.side_effect = TransportError("Connection refused")
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        # Should fail with clean error, not traceback
        assert result.exit_code != 0
        assert "connection refused" in result.output.lower()
        assert "Traceback" not in result.output

    @patch("satdeploy.cli.get_transport")
    def test_push_handles_deploy_failure(self, mock_get_transport, tmp_path):
        """Push should handle deploy returning failure."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": None,
                    }
                })
            )
        )

        transport = make_mock_transport(
            DeployResult(success=False, error_message="Permission denied")
        )
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0

    @patch("satdeploy.cli.get_transport")
    def test_push_shows_skipped_message(self, mock_get_transport, tmp_path):
        """Push should show skip message when binary already deployed."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport(
            DeployResult(success=True, file_hash="abcd1234", skipped=True)
        )
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "already deployed" in result.output.lower()

    @patch("satdeploy.cli.get_transport")
    def test_push_shows_restored_message(self, mock_get_transport, tmp_path):
        """Push should show restore message when restoring from backup."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport(
            DeployResult(
                success=True, file_hash="abcd1234",
                restored=True, backup_path="/backups/test.bak",
            )
        )
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "restored from backup" in result.output.lower()


class TestPushHistoryLogging:
    """Tests for deployment history logging on push."""

    @patch("satdeploy.cli.get_transport")
    def test_push_logs_successful_deployment(self, mock_get_transport, tmp_path):
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
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
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

    @patch("satdeploy.cli.get_transport")
    def test_push_logs_file_hash(self, mock_get_transport, tmp_path):
        """Push should record the file hash in history."""
        from satdeploy.history import History

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert records[0].file_hash is not None
        assert len(records[0].file_hash) == 8  # First 8 chars of SHA256

    @patch("satdeploy.cli.get_transport")
    def test_push_logs_failed_deployment(self, mock_get_transport, tmp_path):
        """Failed push should be recorded in history with error message."""
        from satdeploy.history import History
        from satdeploy.transport.base import TransportError

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": None,
                    }
                })
            )
        )

        transport = MagicMock()
        transport.connect.side_effect = TransportError("Connection refused")
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].success is False
        assert "Connection refused" in records[0].error_message


class TestPushPolishedOutput:
    """Tests for polished CLI output formatting."""

    @patch("satdeploy.cli.get_transport")
    def test_push_success_shows_checkmark(self, mock_get_transport, tmp_path):
        """Successful push should show checkmark symbol."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["check"] in result.output


class TestPushDryRun:
    """Test --dry-run flag."""

    @patch("satdeploy.cli.get_transport")
    def test_dry_run_shows_plan_without_deploying(self, mock_get_transport, tmp_path):
        """--dry-run should show what would happen without actually deploying."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content for dry run test")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        result = runner.invoke(
            main,
            ["push", "controller", "--dry-run", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "Dry run" in result.output
        assert "controller" in result.output
        assert "/opt/disco/bin/controller" in result.output
        # Transport should never be created in dry-run mode
        mock_get_transport.assert_not_called()

    @patch("satdeploy.cli.get_transport")
    def test_dry_run_shows_file_size(self, mock_get_transport, tmp_path):
        """--dry-run should show the binary file size."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"x" * 2048)  # 2 KB

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        result = runner.invoke(
            main,
            ["push", "controller", "--dry-run", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "KB" in result.output


class TestPushConfirmation:
    """Test confirmation prompts."""

    @patch("satdeploy.cli.get_transport")
    def test_push_all_prompts_for_confirmation(self, mock_get_transport, tmp_path):
        """push --all should ask for confirmation when deploying multiple apps."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary1 = tmp_path / "app1"
        binary1.write_bytes(b"app1 binary")
        binary2 = tmp_path / "app2"
        binary2.write_bytes(b"app2 binary")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "app1": {
                        "local": str(binary1),
                        "remote": "/opt/bin/app1",
                        "service": "app1.service",
                    },
                    "app2": {
                        "local": str(binary2),
                        "remote": "/opt/bin/app2",
                        "service": "app2.service",
                    },
                })
            )
        )

        # Answer "no" to confirmation
        result = runner.invoke(
            main,
            ["push", "--all", "--config", str(config_dir / "config.yaml")],
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        mock_get_transport.assert_not_called()

    @patch("satdeploy.cli.get_transport")
    def test_push_all_with_yes_skips_confirmation(self, mock_get_transport, tmp_path):
        """push --all -y should skip confirmation."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary1 = tmp_path / "app1"
        binary1.write_bytes(b"app1 binary")
        binary2 = tmp_path / "app2"
        binary2.write_bytes(b"app2 binary")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "app1": {
                        "local": str(binary1),
                        "remote": "/opt/bin/app1",
                        "service": "app1.service",
                    },
                    "app2": {
                        "local": str(binary2),
                        "remote": "/opt/bin/app2",
                        "service": "app2.service",
                    },
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "--all", "-y", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "Aborted" not in result.output

    @patch("satdeploy.cli.get_transport")
    def test_push_single_app_no_confirmation(self, mock_get_transport, tmp_path):
        """push with a single app should not ask for confirmation."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "Continue?" not in result.output


class TestPushHealthCheck:
    """Test post-deploy health checks."""

    @patch("satdeploy.cli.get_transport")
    def test_push_ssh_runs_health_check(self, mock_get_transport, tmp_path):
        """SSH push should check service status after successful deploy."""
        from satdeploy.services import ServiceStatus

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        # Set up service_manager so the health check path is exercised
        mock_svc_mgr = MagicMock()
        mock_svc_mgr.get_status.return_value = ServiceStatus.RUNNING
        transport.service_manager = mock_svc_mgr
        transport.ssh = MagicMock()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "health check passed" in result.output.lower()
        mock_svc_mgr.get_status.assert_called_once_with("controller.service")

    @patch("satdeploy.cli.get_transport")
    def test_push_ssh_health_check_warns_on_failure(self, mock_get_transport, tmp_path):
        """SSH push should warn if service is in failed state after deploy."""
        from satdeploy.services import ServiceStatus

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_svc_mgr = MagicMock()
        mock_svc_mgr.get_status.return_value = ServiceStatus.FAILED
        transport.service_manager = mock_svc_mgr
        transport.ssh = MagicMock()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "failed state" in result.output.lower()


class TestPushProvenance:
    """Test git provenance tracking on push."""

    @patch("satdeploy.cli.resolve_provenance", return_value=("main@abc12345", "local"))
    @patch("satdeploy.cli.get_transport")
    def test_push_captures_provenance(self, mock_get_transport, mock_provenance, tmp_path):
        """Push should show provenance string and record git_hash in history."""
        from satdeploy.history import History

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "main@abc12345" in result.output

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].git_hash == "main@abc12345"

    @patch("satdeploy.cli.resolve_provenance", return_value=("main@abc12345-dirty", "local"))
    @patch("satdeploy.cli.get_transport")
    def test_push_require_clean_rejects_dirty(self, mock_get_transport, mock_provenance, tmp_path):
        """Push with --require-clean should reject dirty git tree."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        result = runner.invoke(
            main,
            ["push", "controller", "--require-clean", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code != 0
        assert "dirty" in result.output.lower()
        mock_get_transport.assert_not_called()

    @patch("satdeploy.cli.resolve_provenance", return_value=("main@abc12345", "local"))
    @patch("satdeploy.cli.get_transport")
    def test_push_require_clean_allows_clean(self, mock_get_transport, mock_provenance, tmp_path):
        """Push with --require-clean should succeed when tree is clean."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--require-clean", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "deployed" in result.output.lower() or "success" in result.output.lower()

    @patch("satdeploy.cli.resolve_provenance", return_value=("main@abc12345-dirty", "local"))
    @patch("satdeploy.cli.get_transport")
    def test_push_warns_on_dirty_tree(self, mock_get_transport, mock_provenance, tmp_path):
        """Push without --require-clean should warn on dirty tree but succeed."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        assert "uncommitted" in result.output.lower() or "dirty" in result.output.lower()
        # Deploy should still succeed
        assert "deployed" in result.output.lower() or "success" in result.output.lower()

    @patch("satdeploy.cli.resolve_provenance", return_value=(None, "local"))
    @patch("satdeploy.cli.get_transport")
    def test_push_no_provenance_when_not_git_repo(self, mock_get_transport, mock_provenance, tmp_path):
        """Push should succeed without provenance when not in a git repo."""
        from satdeploy.history import History

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            ["push", "controller", "--config", str(config_dir / "config.yaml")],
        )

        assert result.exit_code == 0
        # No provenance string should appear in parentheses after the hash
        # The output should have the hash but not a provenance tag
        assert "main@" not in result.output

        history = History(config_dir / "history.db")
        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].git_hash is None

    # Regression: ISSUE-001 — --require-clean silently passes when binary is outside git repo
    # Found by /qa on 2026-03-24
    @patch("satdeploy.cli.resolve_provenance", return_value=(None, "local"))
    @patch("satdeploy.cli.get_transport")
    def test_require_clean_rejects_when_no_provenance_and_cwd_dirty(
        self, mock_get_transport, mock_provenance, tmp_path
    ):
        """--require-clean should check CWD git status when binary has no provenance."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        # Mock subprocess.run to simulate dirty CWD git status
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = Mock(returncode=1)  # dirty

            result = runner.invoke(
                main,
                ["push", "controller", "--require-clean", "--config", str(config_dir / "config.yaml")],
            )

        assert result.exit_code != 0
        assert "dirty" in result.output.lower()
        mock_get_transport.assert_not_called()

    # Regression: ISSUE-001 — companion test for clean CWD
    @patch("satdeploy.cli.resolve_provenance", return_value=(None, "local"))
    @patch("satdeploy.cli.get_transport")
    def test_require_clean_allows_when_no_provenance_and_cwd_clean(
        self, mock_get_transport, mock_provenance, tmp_path
    ):
        """--require-clean should allow deploy when binary has no provenance but CWD is clean."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                make_config({
                    "controller": {
                        "local": str(binary),
                        "remote": "/opt/disco/bin/controller",
                        "service": "controller.service",
                    }
                })
            )
        )

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        # Mock subprocess.run to simulate clean CWD git status
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = Mock(returncode=0)  # clean

            result = runner.invoke(
                main,
                ["push", "controller", "--require-clean", "--config", str(config_dir / "config.yaml")],
            )

        assert result.exit_code == 0


class TestAdhocPush:
    """Tests for ad-hoc push mode (--local + --remote without app name)."""

    @patch("satdeploy.cli.get_transport")
    def test_adhoc_push_succeeds(self, mock_get_transport, tmp_path):
        """Ad-hoc push with --local and --remote should deploy without config entry."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        payload = tmp_path / "test-payload.txt"
        payload.write_text("hello satellite")

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            [
                "push",
                "--local", str(payload),
                "--remote", "/opt/test/payload.txt",
                "--config", str(config_file),
                "-y",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Ad-hoc mode" in result.output
        transport.deploy.assert_called_once()
        deploy_call = transport.deploy.call_args
        assert deploy_call.kwargs["remote_path"] == "/opt/test/payload.txt"
        assert deploy_call.kwargs["app_name"] == "payload"

    @patch("satdeploy.cli.get_transport")
    def test_adhoc_push_derives_app_name_from_remote(self, mock_get_transport, tmp_path):
        """App name should be derived from remote path basename without extension."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        payload = tmp_path / "myfile.bin"
        payload.write_bytes(b"\x00" * 32)

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            [
                "push",
                "--local", str(payload),
                "--remote", "/opt/disco/bin/controller",
                "--config", str(config_file),
                "-y",
            ],
        )

        assert result.exit_code == 0, result.output
        deploy_call = transport.deploy.call_args
        assert deploy_call.kwargs["app_name"] == "controller"

    @patch("satdeploy.cli.get_transport")
    def test_adhoc_push_avoids_name_collision(self, mock_get_transport, tmp_path):
        """Ad-hoc push should prefix with adhoc- if name collides with configured app."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        # Config has a "controller" app
        existing_binary = tmp_path / "controller"
        existing_binary.write_bytes(b"existing")
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({
            "controller": {
                "local": str(existing_binary),
                "remote": "/opt/disco/bin/controller",
                "service": "controller.service",
            }
        })))

        payload = tmp_path / "new-controller.bin"
        payload.write_bytes(b"new content")

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            [
                "push",
                "--local", str(payload),
                "--remote", "/opt/disco/bin/controller",
                "--config", str(config_file),
                "-y",
            ],
        )

        assert result.exit_code == 0, result.output
        deploy_call = transport.deploy.call_args
        assert deploy_call.kwargs["app_name"] == "adhoc-controller"

    def test_adhoc_push_fails_without_remote(self, tmp_path):
        """--local without --remote and no app name should fail."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        payload = tmp_path / "test.txt"
        payload.write_text("test")

        result = runner.invoke(
            main,
            [
                "push",
                "--local", str(payload),
                "--config", str(config_file),
            ],
        )

        assert result.exit_code != 0

    def test_adhoc_push_fails_with_remote_and_app_name(self, tmp_path):
        """--remote with an app name should fail."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        payload = tmp_path / "test.txt"
        payload.write_text("test")

        result = runner.invoke(
            main,
            [
                "push", "myapp",
                "--local", str(payload),
                "--remote", "/opt/test.txt",
                "--config", str(config_file),
            ],
        )

        assert result.exit_code != 0
        assert "Cannot specify app name" in result.output

    def test_adhoc_push_fails_remote_without_local(self, tmp_path):
        """--remote without --local should fail."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        result = runner.invoke(
            main,
            [
                "push",
                "--remote", "/opt/test.txt",
                "--config", str(config_file),
            ],
        )

        assert result.exit_code != 0
        assert "--remote requires --local" in result.output

    @patch("satdeploy.cli.get_transport")
    def test_adhoc_push_records_history(self, mock_get_transport, tmp_path):
        """Ad-hoc push should record deployment in history.db."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        payload = tmp_path / "test-payload.txt"
        payload.write_text("hello satellite")

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            [
                "push",
                "--local", str(payload),
                "--remote", "/opt/test/payload.txt",
                "--config", str(config_file),
                "-y",
            ],
        )

        assert result.exit_code == 0, result.output

        history = History(config_dir / "history.db")
        records = history.get_history("payload")
        assert len(records) == 1
        assert records[0].remote_path == "/opt/test/payload.txt"
        assert records[0].success is True

    @patch("satdeploy.cli.get_transport")
    def test_adhoc_push_shows_warning_and_prompts(self, mock_get_transport, tmp_path):
        """Ad-hoc push without -y should show warning and prompt."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        payload = tmp_path / "test.txt"
        payload.write_text("test")

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        # Answer 'n' to the confirmation prompt
        result = runner.invoke(
            main,
            [
                "push",
                "--local", str(payload),
                "--remote", "/opt/test.txt",
                "--config", str(config_file),
            ],
            input="n\n",
        )

        assert "Ad-hoc mode" in result.output
        assert "Continue?" in result.output
        transport.deploy.assert_not_called()

    @patch("satdeploy.cli.get_transport")
    def test_adhoc_push_no_service_restart(self, mock_get_transport, tmp_path):
        """Ad-hoc push should not pass services to transport.deploy."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump(make_config({})))

        payload = tmp_path / "test.txt"
        payload.write_text("test")

        transport = make_mock_transport()
        mock_get_transport.return_value = transport

        result = runner.invoke(
            main,
            [
                "push",
                "--local", str(payload),
                "--remote", "/opt/test.txt",
                "--config", str(config_file),
                "-y",
            ],
        )

        assert result.exit_code == 0, result.output
        deploy_call = transport.deploy.call_args
        services = deploy_call.kwargs.get("services")
        assert services is None or services == []
