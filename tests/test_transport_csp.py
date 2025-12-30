"""Tests for the CSP transport implementation."""

from unittest.mock import MagicMock, patch
import pytest
import zmq as real_zmq

from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)
from satdeploy.transport.csp import CSPTransport
from satdeploy.csp.proto import DeployCommand, DeployRequest, DeployResponse


@pytest.fixture
def mock_zmq():
    """Create ZMQ mock that preserves exception classes."""
    with patch("satdeploy.transport.csp.zmq") as mock:
        # Preserve real exception classes for proper exception handling
        mock.Again = real_zmq.Again
        mock.ZMQError = real_zmq.ZMQError
        mock.DEALER = real_zmq.DEALER
        mock.RCVTIMEO = real_zmq.RCVTIMEO
        mock.SNDTIMEO = real_zmq.SNDTIMEO
        mock.NOBLOCK = real_zmq.NOBLOCK

        # Setup default context/socket
        mock_context = MagicMock()
        mock_socket = MagicMock()
        mock.Context.return_value = mock_context
        mock_context.socket.return_value = mock_socket

        # Store references for tests
        mock._context = mock_context
        mock._socket = mock_socket

        yield mock


def make_csp_response(response: DeployResponse) -> bytes:
    """Wrap a protobuf response with a CSP header for testing."""
    # 4-byte CSP header (dummy values for testing)
    header = b'\x00\x00\x00\x00'
    return header + response.SerializeToString()


@pytest.fixture
def mock_dtp():
    """Create DTPServer mock."""
    with patch("satdeploy.transport.csp.DTPServer") as mock:
        yield mock


class TestCSPTransportInterface:
    """Test that CSPTransport implements Transport interface."""

    def test_csp_transport_is_transport(self):
        """CSPTransport is a Transport subclass."""
        assert issubclass(CSPTransport, Transport)

    def test_csp_transport_instantiation(self):
        """CSPTransport can be instantiated with required params."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/opt/satdeploy/backups",
        )
        assert transport.zmq_endpoint == "tcp://localhost:4040"
        assert transport.agent_node == 5424
        assert transport.ground_node == 4040


class TestCSPTransportConnection:
    """Test CSP connection handling."""

    def test_connect_creates_zmq_socket(self, mock_zmq):
        """connect() establishes ZMQ connection."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()

        mock_zmq.Context.assert_called_once()
        mock_zmq._context.socket.assert_called()

    def test_disconnect_closes_zmq_socket(self, mock_zmq):
        """disconnect() closes ZMQ connection."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()
        transport.disconnect()

        mock_zmq._socket.close.assert_called()

    def test_context_manager(self, mock_zmq):
        """CSPTransport works as context manager."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )

        with transport:
            pass

        mock_zmq._socket.close.assert_called()


class TestCSPTransportDeploy:
    """Test CSP deployment operations."""

    def test_deploy_sends_command(self, mock_zmq, mock_dtp, tmp_path):
        """deploy() sends CMD_DEPLOY to agent."""
        # Create a test binary
        binary = tmp_path / "test_app"
        binary.write_bytes(b"test binary content")

        # Mock successful response
        response = DeployResponse()
        response.success = True
        response.backup_path = "/backups/app/123.bak"
        mock_zmq._socket.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()

        result = transport.deploy(
            app_name="dipp",
            local_path=str(binary),
            remote_path="/usr/bin/dipp",
            param_name="mng_dipp",
            appsys_node=5421,
            run_node=5423,
        )

        assert result.success is True
        assert result.backup_path == "/backups/app/123.bak"
        # Verify DTP server was started and stopped
        mock_dtp.return_value.start.assert_called_once()
        mock_dtp.return_value.stop.assert_called_once()

    def test_deploy_handles_failure(self, mock_zmq, mock_dtp, tmp_path):
        """deploy() handles agent failure response."""
        binary = tmp_path / "test_app"
        binary.write_bytes(b"test binary content")

        # Mock failure response
        response = DeployResponse()
        response.success = False
        response.error_code = 6  # ERR_CHECKSUM_MISMATCH
        response.error_message = "Checksum verification failed"
        mock_zmq._socket.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()

        result = transport.deploy(
            app_name="dipp",
            local_path=str(binary),
            remote_path="/usr/bin/dipp",
        )

        assert result.success is False
        assert result.error_code == 6
        assert "Checksum" in result.error_message


class TestCSPTransportRollback:
    """Test CSP rollback operations."""

    def test_rollback_sends_command(self, mock_zmq):
        """rollback() sends CMD_ROLLBACK to agent."""
        response = DeployResponse()
        response.success = True
        mock_zmq._socket.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()

        result = transport.rollback(app_name="dipp")

        assert result.success is True
        # Verify command was sent
        mock_zmq._socket.send.assert_called_once()


class TestCSPTransportStatus:
    """Test CSP status queries."""

    def test_get_status_queries_agent(self, mock_zmq):
        """get_status() sends CMD_STATUS and parses response."""
        response = DeployResponse()
        response.success = True
        app_status = response.apps.add()
        app_status.app_name = "dipp"
        app_status.running = True
        app_status.binary_hash = "abc12345"
        app_status.remote_path = "/usr/bin/dipp"
        mock_zmq._socket.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()

        status = transport.get_status()

        assert "dipp" in status
        assert status["dipp"].running is True
        assert status["dipp"].binary_hash == "abc12345"


class TestCSPTransportListBackups:
    """Test CSP backup listing."""

    def test_list_backups_queries_agent(self, mock_zmq):
        """list_backups() sends CMD_LIST_VERSIONS and parses response."""
        response = DeployResponse()
        response.success = True
        backup = response.backups.add()
        backup.version = "20250115-143022-abc12345"
        backup.timestamp = "2025-01-15 14:30:22"
        backup.hash = "abc12345"
        backup.path = "/backups/dipp/20250115-143022-abc12345.bak"
        mock_zmq._socket.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()

        backups = transport.list_backups("dipp")

        assert len(backups) == 1
        assert isinstance(backups[0], BackupInfo)
        assert backups[0].binary_hash == "abc12345"


class TestCSPTransportVerify:
    """Test CSP verification."""

    def test_verify_queries_agent(self, mock_zmq):
        """verify() sends CMD_VERIFY and returns checksum."""
        response = DeployResponse()
        response.success = True
        response.actual_checksum = "abc12345"
        mock_zmq._socket.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
            backup_dir="/backups",
        )
        transport.connect()

        checksum = transport.verify("dipp", "/usr/bin/dipp")

        assert checksum == "abc12345"
