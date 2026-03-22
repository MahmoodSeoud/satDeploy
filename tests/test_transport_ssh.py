"""Tests for the SSH transport implementation."""

from unittest.mock import MagicMock, patch
import pytest

from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)
from satdeploy.transport.ssh import SSHTransport


class TestSSHTransportInterface:
    """Test that SSHTransport implements Transport interface."""

    def test_ssh_transport_is_transport(self):
        """SSHTransport is a Transport subclass."""
        assert issubclass(SSHTransport, Transport)

    def test_ssh_transport_instantiation(self):
        """SSHTransport can be instantiated with host and user."""
        transport = SSHTransport(
            host="192.168.1.50",
            user="root",
            backup_dir="/opt/satdeploy/backups",
        )
        assert transport.host == "192.168.1.50"
        assert transport.user == "root"
        assert transport.backup_dir == "/opt/satdeploy/backups"


class TestSSHTransportConnection:
    """Test SSH connection handling."""

    @patch("satdeploy.transport.ssh.SSHClient")
    def test_connect_creates_ssh_client(self, mock_ssh_class):
        """connect() establishes SSH connection."""
        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        mock_ssh_class.assert_called_once_with("192.168.1.50", "root", 22)
        mock_ssh_class.return_value.connect.assert_called_once()

    @patch("satdeploy.transport.ssh.SSHClient")
    def test_disconnect_closes_ssh_client(self, mock_ssh_class):
        """disconnect() closes SSH connection."""
        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()
        transport.disconnect()

        mock_ssh_class.return_value.disconnect.assert_called_once()

    @patch("satdeploy.transport.ssh.SSHClient")
    def test_context_manager(self, mock_ssh_class):
        """SSHTransport works as context manager."""
        transport = SSHTransport("192.168.1.50", "root", "/backups")

        with transport:
            pass

        mock_ssh_class.return_value.connect.assert_called_once()
        mock_ssh_class.return_value.disconnect.assert_called_once()

    @patch("satdeploy.transport.ssh.SSHClient")
    def test_connect_failure_raises_transport_error(self, mock_ssh_class):
        """connect() raises TransportError on SSH failure."""
        from satdeploy.ssh import SSHError
        mock_ssh_class.return_value.connect.side_effect = SSHError("Connection refused")

        transport = SSHTransport("192.168.1.50", "root", "/backups")

        with pytest.raises(TransportError) as exc_info:
            transport.connect()
        assert "Connection refused" in str(exc_info.value)


class TestSSHTransportDeploy:
    """Test SSH deployment operations."""

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_deploy_success(self, mock_svc_class, mock_deployer_class, mock_ssh_class, tmp_path):
        """deploy() successfully deploys a binary."""
        # Create a test binary
        binary = tmp_path / "test_app"
        binary.write_bytes(b"test binary content")

        # Mock deployer behavior
        mock_deployer = mock_deployer_class.return_value
        mock_deployer.compute_hash.return_value = "abc12345"
        mock_deployer.backup.return_value = "/backups/app/20250115-abc12345.bak"

        # Mock service manager
        mock_svc = mock_svc_class.return_value

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        result = transport.deploy(
            app_name="test_app",
            local_path=str(binary),
            remote_path="/usr/bin/test_app",
        )

        assert result.success is True
        assert result.backup_path == "/backups/app/20250115-abc12345.bak"
        mock_deployer.backup.assert_called_once()
        mock_deployer.deploy.assert_called_once()

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_deploy_with_service(self, mock_svc_class, mock_deployer_class, mock_ssh_class, tmp_path):
        """deploy() stops and starts services when services list provided."""
        binary = tmp_path / "test_app"
        binary.write_bytes(b"test binary content")

        mock_deployer = mock_deployer_class.return_value
        mock_deployer.compute_remote_hash.return_value = None
        mock_deployer.list_backups.return_value = []
        mock_deployer.backup.return_value = None

        mock_svc = mock_svc_class.return_value

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        result = transport.deploy(
            app_name="test_app",
            local_path=str(binary),
            remote_path="/usr/bin/test_app",
            services=[("test_app", "test_app.service")],
        )

        assert result.success is True
        mock_svc.stop.assert_called_once_with("test_app.service")
        mock_svc.start.assert_called_once_with("test_app.service")


class TestSSHTransportRollback:
    """Test SSH rollback operations."""

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_rollback_latest(self, mock_svc_class, mock_deployer_class, mock_ssh_class):
        """rollback() restores the most recent backup."""
        mock_deployer = mock_deployer_class.return_value
        mock_deployer.list_backups.return_value = [
            {"version": "20250115-143022-abc12345", "path": "/backups/app/20250115-143022-abc12345.bak", "hash": "abc12345", "timestamp": "2025-01-15 14:30:22"},
            {"version": "20250114-091500-def67890", "path": "/backups/app/20250114-091500-def67890.bak", "hash": "def67890", "timestamp": "2025-01-14 09:15:00"},
        ]

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        result = transport.rollback(
            app_name="test_app",
            remote_path="/usr/bin/test_app",
        )

        assert result.success is True
        mock_deployer.restore.assert_called_once()

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_rollback_specific_hash(self, mock_svc_class, mock_deployer_class, mock_ssh_class):
        """rollback() can restore a specific backup by hash."""
        mock_deployer = mock_deployer_class.return_value
        mock_deployer.list_backups.return_value = [
            {"version": "20250115-143022-abc12345", "path": "/backups/app/20250115-143022-abc12345.bak", "hash": "abc12345", "timestamp": "2025-01-15 14:30:22"},
            {"version": "20250114-091500-def67890", "path": "/backups/app/20250114-091500-def67890.bak", "hash": "def67890", "timestamp": "2025-01-14 09:15:00"},
        ]

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        result = transport.rollback(
            app_name="test_app",
            remote_path="/usr/bin/test_app",
            backup_hash="def67890",
        )

        assert result.success is True
        # Should restore the backup matching the hash
        mock_deployer.restore.assert_called_once_with(
            "/backups/app/20250114-091500-def67890.bak",
            "/usr/bin/test_app"
        )

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_rollback_no_backups(self, mock_svc_class, mock_deployer_class, mock_ssh_class):
        """rollback() fails when no backups exist."""
        mock_deployer = mock_deployer_class.return_value
        mock_deployer.list_backups.return_value = []

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        result = transport.rollback(
            app_name="test_app",
            remote_path="/usr/bin/test_app",
        )

        assert result.success is False
        assert "No backups" in result.error_message


class TestSSHTransportStatus:
    """Test SSH status queries."""

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_get_status(self, mock_svc_class, mock_deployer_class, mock_ssh_class):
        """get_status() returns status of deployed apps."""
        from satdeploy.services import ServiceStatus

        mock_ssh = mock_ssh_class.return_value
        mock_ssh.file_exists.return_value = True

        mock_deployer = mock_deployer_class.return_value
        mock_deployer.compute_remote_hash.return_value = "abc12345"

        mock_svc = mock_svc_class.return_value
        mock_svc.get_status.return_value = ServiceStatus.RUNNING

        transport = SSHTransport(
            "192.168.1.50", "root", "/backups",
            apps={"dipp": {"remote": "/usr/bin/dipp", "service": "dipp.service"}},
        )
        transport.connect()

        status = transport.get_status()

        assert "dipp" in status
        assert status["dipp"].running is True
        assert status["dipp"].binary_hash == "abc12345"


class TestSSHTransportListBackups:
    """Test SSH backup listing."""

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_list_backups(self, mock_svc_class, mock_deployer_class, mock_ssh_class):
        """list_backups() returns backup information."""
        mock_deployer = mock_deployer_class.return_value
        mock_deployer.list_backups.return_value = [
            {"version": "20250115-143022-abc12345", "path": "/backups/app/20250115-143022-abc12345.bak", "hash": "abc12345", "timestamp": "2025-01-15 14:30:22"},
        ]

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        backups = transport.list_backups("test_app")

        assert len(backups) == 1
        assert isinstance(backups[0], BackupInfo)
        assert backups[0].binary_hash == "abc12345"


class TestSSHTransportVerify:
    """Test SSH verification."""

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_verify_returns_checksum(self, mock_svc_class, mock_deployer_class, mock_ssh_class):
        """verify() returns the remote checksum."""
        mock_deployer = mock_deployer_class.return_value
        mock_deployer.compute_remote_hash.return_value = "abc12345"

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        checksum = transport.verify("test_app", "/usr/bin/test_app")

        assert checksum == "abc12345"

    @patch("satdeploy.transport.ssh.SSHClient")
    @patch("satdeploy.transport.ssh.Deployer")
    @patch("satdeploy.transport.ssh.ServiceManager")
    def test_verify_returns_none_when_file_missing(self, mock_svc_class, mock_deployer_class, mock_ssh_class):
        """verify() returns None when file doesn't exist."""
        mock_deployer = mock_deployer_class.return_value
        mock_deployer.compute_remote_hash.return_value = None

        transport = SSHTransport("192.168.1.50", "root", "/backups")
        transport.connect()

        checksum = transport.verify("test_app", "/usr/bin/test_app")

        assert checksum is None
