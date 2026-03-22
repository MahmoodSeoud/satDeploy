"""Tests for the CSP transport implementation."""

from unittest.mock import MagicMock, patch, call
import struct
import pytest
import zmq as real_zmq

from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)
from satdeploy.transport.csp import CSPTransport, CSP_HEADER_SIZE
from satdeploy.csp.proto import DeployCommand, DeployRequest, DeployResponse


@pytest.fixture
def mock_zmq():
    """Create ZMQ mock that preserves exception classes."""
    with patch("satdeploy.transport.csp.zmq") as mock:
        # Preserve real exception classes and constants
        mock.Again = real_zmq.Again
        mock.ZMQError = real_zmq.ZMQError
        mock.PUB = real_zmq.PUB
        mock.SUB = real_zmq.SUB
        mock.SUBSCRIBE = real_zmq.SUBSCRIBE
        mock.LINGER = real_zmq.LINGER
        mock.RCVTIMEO = real_zmq.RCVTIMEO
        mock.SNDTIMEO = real_zmq.SNDTIMEO
        mock.NOBLOCK = real_zmq.NOBLOCK

        # Setup default context with separate pub/sub sockets
        mock_context = MagicMock()
        mock_pub = MagicMock()
        mock_sub = MagicMock()

        def socket_factory(socket_type):
            if socket_type == real_zmq.PUB:
                return mock_pub
            elif socket_type == real_zmq.SUB:
                return mock_sub
            return MagicMock()

        mock.Context.return_value = mock_context
        mock_context.socket.side_effect = socket_factory

        # Store references for tests
        mock._context = mock_context
        mock._pub = mock_pub
        mock._sub = mock_sub

        yield mock


def make_csp_response(response: DeployResponse) -> bytes:
    """Wrap a protobuf response with a CSP v2 header for testing.

    Builds a realistic header with sport=20 (CSP_DEPLOY_PORT) so the
    CSPTransport's port filter accepts it.
    """
    import struct
    # pri=2, dst=40, src=5425, dport=0, sport=20 (deploy port)
    id2 = (
        (2 & 0x3) << 46 |
        (40 & 0x3FFF) << 32 |
        (5425 & 0x3FFF) << 18 |
        (0 & 0x3F) << 12 |
        (20 & 0x3F) << 6
    )
    header = struct.pack(">Q", id2 << 16)[:6]
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
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
            backup_dir="/opt/satdeploy/backups",
        )
        assert transport.zmq_endpoint == "tcp://localhost:6000"
        assert transport.zmq_host == "localhost"
        assert transport.agent_node == 5424
        assert transport.ground_node == 40

    def test_zmq_host_parsing(self):
        """Host is extracted from various zmq_endpoint formats."""
        t1 = CSPTransport(zmq_endpoint="tcp://192.168.1.5:6000",
                          agent_node=1, ground_node=2, backup_dir="/b")
        assert t1.zmq_host == "192.168.1.5"

        t2 = CSPTransport(zmq_endpoint="tcp://satcom:4040",
                          agent_node=1, ground_node=2, backup_dir="/b")
        assert t2.zmq_host == "satcom"

        t3 = CSPTransport(zmq_endpoint="localhost",
                          agent_node=1, ground_node=2, backup_dir="/b")
        assert t3.zmq_host == "localhost"


class TestCSPTransportConnection:
    """Test CSP connection handling."""

    def test_connect_creates_pub_sub_sockets(self, mock_zmq):
        """connect() creates PUB and SUB sockets through zmqproxy."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
            backup_dir="/backups",
        )
        transport.connect()

        mock_zmq.Context.assert_called_once()
        # Should create both PUB and SUB sockets
        assert mock_zmq._context.socket.call_count == 2
        mock_zmq._pub.connect.assert_called_with("tcp://localhost:9600")
        mock_zmq._sub.connect.assert_called_with("tcp://localhost:9601")

    def test_connect_sets_sub_filters(self, mock_zmq):
        """connect() subscribes to packets for our ground node."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
            backup_dir="/backups",
        )
        transport.connect()

        # Should subscribe for all 4 priority levels
        subscribe_calls = [
            c for c in mock_zmq._sub.setsockopt.call_args_list
            if c[0][0] == real_zmq.SUBSCRIBE
        ]
        assert len(subscribe_calls) == 4

        # Verify filter format: big-endian uint16 = (priority << 14) | ground_node
        for pri in range(4):
            expected_filter = struct.pack(">H", (pri << 14) | 40)
            assert call(real_zmq.SUBSCRIBE, expected_filter) in mock_zmq._sub.setsockopt.call_args_list

    def test_disconnect_closes_both_sockets(self, mock_zmq):
        """disconnect() closes PUB and SUB with linger=0."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
            backup_dir="/backups",
        )
        transport.connect()
        transport.disconnect()

        mock_zmq._pub.close.assert_called_with(linger=0)
        mock_zmq._sub.close.assert_called_with(linger=0)

    def test_context_manager(self, mock_zmq):
        """CSPTransport works as context manager."""
        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
            backup_dir="/backups",
        )

        with transport:
            pass

        mock_zmq._pub.close.assert_called_with(linger=0)
        mock_zmq._sub.close.assert_called_with(linger=0)


class TestCSPV2Header:
    """Test CSP v2 header encoding/decoding."""

    def test_header_is_6_bytes(self):
        """CSP v2 header is 6 bytes."""
        transport = CSPTransport(
            zmq_endpoint="localhost",
            agent_node=5425,
            ground_node=40,
            backup_dir="/backups",
        )
        header = transport._build_csp_header(dest=5425, dest_port=20)
        assert len(header) == 6

    def test_header_roundtrip(self):
        """Build and parse produces same values."""
        transport = CSPTransport(
            zmq_endpoint="localhost",
            agent_node=5425,
            ground_node=40,
            backup_dir="/backups",
        )
        header = transport._build_csp_header(dest=5425, dest_port=20, src_port=15)
        parsed = CSPTransport._parse_csp_header(header)

        assert parsed["dst"] == 5425
        assert parsed["src"] == 40
        assert parsed["dport"] == 20
        assert parsed["sport"] == 15
        assert parsed["pri"] == 2  # Normal priority

    def test_header_14bit_addresses(self):
        """CSP v2 supports 14-bit node addresses (up to 16383)."""
        transport = CSPTransport(
            zmq_endpoint="localhost",
            agent_node=10000,
            ground_node=8000,
            backup_dir="/backups",
        )
        header = transport._build_csp_header(dest=10000, dest_port=20)
        parsed = CSPTransport._parse_csp_header(header)

        assert parsed["dst"] == 10000
        assert parsed["src"] == 8000


class TestCSPTransportDeploy:
    """Test CSP deployment operations."""

    def test_deploy_sends_command(self, mock_zmq, mock_dtp, tmp_path):
        """deploy() sends CMD_DEPLOY to agent."""
        binary = tmp_path / "test_app"
        binary.write_bytes(b"test binary content")

        response = DeployResponse()
        response.success = True
        response.backup_path = "/backups/app/123.bak"
        mock_zmq._sub.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
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
        mock_zmq._pub.send.assert_called_once()
        mock_dtp.return_value.start.assert_called_once()
        mock_dtp.return_value.stop.assert_called_once()

    def test_deploy_handles_failure(self, mock_zmq, mock_dtp, tmp_path):
        """deploy() handles agent failure response."""
        binary = tmp_path / "test_app"
        binary.write_bytes(b"test binary content")

        response = DeployResponse()
        response.success = False
        response.error_code = 6
        response.error_message = "Checksum verification failed"
        mock_zmq._sub.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
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
        mock_zmq._sub.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
            backup_dir="/backups",
        )
        transport.connect()

        result = transport.rollback(app_name="dipp")

        assert result.success is True
        mock_zmq._pub.send.assert_called_once()


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
        mock_zmq._sub.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
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
        mock_zmq._sub.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
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
        mock_zmq._sub.recv.return_value = make_csp_response(response)

        transport = CSPTransport(
            zmq_endpoint="tcp://localhost:6000",
            agent_node=5424,
            ground_node=40,
            backup_dir="/backups",
        )
        transport.connect()

        checksum = transport.verify("dipp", "/usr/bin/dipp")

        assert checksum == "abc12345"
