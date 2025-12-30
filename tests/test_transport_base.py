"""Tests for the Transport base class and interface."""

from abc import ABC
import pytest

from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)


class TestTransportInterface:
    """Test that Transport is a proper abstract base class."""

    def test_transport_is_abstract_class(self):
        """Transport cannot be instantiated directly."""
        with pytest.raises(TypeError):
            Transport()

    def test_transport_requires_connect_method(self):
        """Transport subclass must implement connect."""
        class IncompleteTransport(Transport):
            pass

        with pytest.raises(TypeError):
            IncompleteTransport()

    def test_transport_context_manager(self):
        """Transport supports context manager protocol."""
        class DummyTransport(Transport):
            def __init__(self):
                self.connected = False
                self.disconnected = False

            def connect(self):
                self.connected = True

            def disconnect(self):
                self.disconnected = True

            def deploy(self, app_name, local_path, remote_path, param_name=None,
                      appsys_node=None, run_node=None, expected_checksum=None):
                return DeployResult(success=True)

            def rollback(self, app_name, backup_hash=None):
                return DeployResult(success=True)

            def get_status(self):
                return {}

            def list_backups(self, app_name):
                return []

            def verify(self, app_name, remote_path):
                return None

        transport = DummyTransport()
        with transport:
            assert transport.connected
        assert transport.disconnected


class TestDeployResult:
    """Tests for DeployResult dataclass."""

    def test_deploy_result_success(self):
        """DeployResult can represent a successful deployment."""
        result = DeployResult(success=True, backup_path="/backups/app/123.bak")
        assert result.success is True
        assert result.backup_path == "/backups/app/123.bak"
        assert result.error_message is None

    def test_deploy_result_failure(self):
        """DeployResult can represent a failed deployment."""
        result = DeployResult(
            success=False,
            error_code=5,
            error_message="Checksum mismatch"
        )
        assert result.success is False
        assert result.error_code == 5
        assert result.error_message == "Checksum mismatch"


class TestAppStatus:
    """Tests for AppStatus dataclass."""

    def test_app_status_with_all_fields(self):
        """AppStatus can hold all app state information."""
        status = AppStatus(
            app_name="dipp",
            running=True,
            binary_hash="a1b2c3d4",
            remote_path="/usr/bin/dipp"
        )
        assert status.app_name == "dipp"
        assert status.running is True
        assert status.binary_hash == "a1b2c3d4"
        assert status.remote_path == "/usr/bin/dipp"

    def test_app_status_not_running(self):
        """AppStatus handles apps that aren't running."""
        status = AppStatus(
            app_name="camera-control",
            running=False,
            binary_hash=None,
            remote_path="/usr/bin/DiscoCameraController"
        )
        assert status.running is False
        assert status.binary_hash is None


class TestBackupInfo:
    """Tests for BackupInfo dataclass."""

    def test_backup_info_full(self):
        """BackupInfo holds backup metadata."""
        info = BackupInfo(
            version="20250115-143022-a1b2c3d4",
            timestamp="2025-01-15 14:30:22",
            binary_hash="a1b2c3d4",
            path="/opt/satdeploy/backups/dipp/20250115-143022-a1b2c3d4.bak"
        )
        assert info.version == "20250115-143022-a1b2c3d4"
        assert info.timestamp == "2025-01-15 14:30:22"
        assert info.binary_hash == "a1b2c3d4"
        assert info.path.endswith(".bak")


class TestTransportError:
    """Tests for TransportError exception."""

    def test_transport_error_basic(self):
        """TransportError is an exception with a message."""
        error = TransportError("Connection failed")
        assert str(error) == "Connection failed"

    def test_transport_error_inheritance(self):
        """TransportError inherits from Exception."""
        assert issubclass(TransportError, Exception)
