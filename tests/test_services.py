"""Tests for services module."""

import pytest
from unittest.mock import Mock, MagicMock

from satdeploy.services import ServiceManager, ServiceStatus


class TestServiceStatus:
    """Test getting service status."""

    def test_get_status_running(self):
        """Should return running for active service."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        status = manager.get_status("controller.service")

        assert status == ServiceStatus.RUNNING
        mock_ssh.run.assert_called_once()
        assert "is-active" in mock_ssh.run.call_args[0][0]

    def test_get_status_stopped(self):
        """Should return stopped for inactive service."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="inactive\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        status = manager.get_status("controller.service")

        assert status == ServiceStatus.STOPPED

    def test_get_status_failed(self):
        """Should return failed for failed service."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="failed\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        status = manager.get_status("controller.service")

        assert status == ServiceStatus.FAILED

    def test_get_status_unknown(self):
        """Should return unknown for unknown status."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="some-weird-status\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        status = manager.get_status("controller.service")

        assert status == ServiceStatus.UNKNOWN


class TestServiceControl:
    """Test starting and stopping services."""

    def test_stop_service(self):
        """Should stop a service using systemctl."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="", stderr="")

        manager = ServiceManager(mock_ssh)
        result = manager.stop("controller.service")

        assert result is True
        assert "systemctl stop" in mock_ssh.run.call_args[0][0]
        assert "controller.service" in mock_ssh.run.call_args[0][0]

    def test_start_service(self):
        """Should start a service using systemctl."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="", stderr="")

        manager = ServiceManager(mock_ssh)
        result = manager.start("controller.service")

        assert result is True
        assert "systemctl start" in mock_ssh.run.call_args[0][0]
        assert "controller.service" in mock_ssh.run.call_args[0][0]

    def test_restart_service(self):
        """Should restart a service using systemctl."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="", stderr="")

        manager = ServiceManager(mock_ssh)
        manager.restart("controller.service")

        assert "systemctl restart" in mock_ssh.run.call_args[0][0]
        assert "controller.service" in mock_ssh.run.call_args[0][0]


class TestHealthCheck:
    """Test service health checks."""

    def test_is_healthy_returns_true_for_active(self):
        """Should return True if service is active."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        assert manager.is_healthy("controller.service") is True

    def test_is_healthy_returns_false_for_inactive(self):
        """Should return False if service is not active."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="inactive\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        assert manager.is_healthy("controller.service") is False

    def test_is_healthy_returns_false_for_failed(self):
        """Should return False if service failed."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="failed\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        assert manager.is_healthy("controller.service") is False


class TestServiceLogs:
    """Test getting service logs."""

    def test_get_logs_uses_journalctl(self):
        """Should use journalctl to get logs."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="log line 1\nlog line 2\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        logs = manager.get_logs("controller.service", lines=50)

        assert "journalctl" in mock_ssh.run.call_args[0][0]
        assert "controller.service" in mock_ssh.run.call_args[0][0]
        assert "-n 50" in mock_ssh.run.call_args[0][0]

    def test_get_logs_returns_output(self):
        """Should return the log output."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="log line 1\nlog line 2\n", exit_code=0)

        manager = ServiceManager(mock_ssh)
        logs = manager.get_logs("controller.service")

        assert logs == "log line 1\nlog line 2\n"


class TestDaemonReload:
    """Test systemd daemon reload."""

    def test_daemon_reload_runs_systemctl(self):
        """Should run systemctl daemon-reload."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="", stderr="", exit_code=0)

        manager = ServiceManager(mock_ssh)
        manager.daemon_reload()

        mock_ssh.run.assert_called_once()
        cmd = mock_ssh.run.call_args[0][0]
        assert "systemctl daemon-reload" in cmd
        assert "sudo" in cmd


class TestServiceEnable:
    """Test enabling services."""

    def test_enable_runs_systemctl_enable(self):
        """Should run systemctl enable."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="", stderr="", exit_code=0)

        manager = ServiceManager(mock_ssh)
        manager.enable("controller.service")

        mock_ssh.run.assert_called_once()
        cmd = mock_ssh.run.call_args[0][0]
        assert "systemctl enable" in cmd
        assert "controller.service" in cmd
        assert "sudo" in cmd
