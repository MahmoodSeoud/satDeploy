"""CSP transport implementation using libcsp Python bindings.

Uses libcsp_py3 (the official C extension) for CSP framing, routing, and
ZMQ interface management. This replaces the previous raw-ZMQ implementation
that reimplemented CSP v2 header packing and packet filtering by hand.
"""

import os
import stat
from typing import Optional

try:
    import libcsp_py3 as libcsp
except ImportError:
    libcsp = None

from satdeploy.csp.proto import (
    DeployCommand,
    DeployRequest,
    DeployResponse,
)
from satdeploy.csp.dtp_server import DTPServer
from satdeploy.hash import compute_file_hash
from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)


CSP_DEPLOY_PORT = 20  # Port for deploy commands

# Module-level guard: libcsp.init() can only be called once per process.
_csp_initialized = False


class CSPTransport(Transport):
    """Transport implementation using CSP via libcsp Python bindings.

    Uses libcsp_py3 for all CSP operations: ZMQ interface setup, routing,
    and request/response transactions. The DTP server runs in a separate
    thread using libcsp's socket API.
    """

    def __init__(
        self,
        zmq_endpoint: str,
        agent_node: int,
        ground_node: int,
        backup_dir: str,
        dtp_port: int = 7,
        timeout_ms: int = 30000,
        dtp_mtu: int = 1024,
        dtp_throughput: int = 10000000,
        dtp_timeout: int = 60,
    ):
        self.zmq_endpoint = zmq_endpoint
        self.zmq_host = _parse_zmq_host(zmq_endpoint)
        self.agent_node = agent_node
        self.ground_node = ground_node
        self.backup_dir = backup_dir
        self.dtp_port = dtp_port
        self.timeout_ms = timeout_ms
        self.dtp_mtu = dtp_mtu
        self.dtp_throughput = dtp_throughput
        self.dtp_timeout = dtp_timeout

        self._connected = False
        self._dtp_server: Optional[DTPServer] = None
        self._payload_counter = 0

    def connect(self) -> None:
        """Initialize libcsp and ZMQ interface.

        Calls libcsp.init() once per process, then sets up the ZMQ
        interface and routing table. Subsequent calls skip init.

        Raises:
            TransportError: If initialization fails.
        """
        global _csp_initialized
        if libcsp is None:
            raise TransportError(
                "CSP transport requires libcsp_py3. "
                "Build and install from libcsp-py3/: pip install ./libcsp-py3"
            )
        try:
            if not _csp_initialized:
                libcsp.init("satdeploy", "ground", "0.1")
                # init() is once-per-process — mark done before ZMQ setup
                # so a retry after zmqhub failure won't re-call init()
                _csp_initialized = True
                libcsp.zmqhub_init(self.ground_node, self.zmq_host, True)
                libcsp.rtable_load("0/0 ZMQHUB")
                libcsp.route_start_task()
            self._connected = True
        except libcsp.Error as e:
            raise TransportError(
                f"Failed to initialize libcsp on {self.zmq_host}: {e}"
            )

    def disconnect(self) -> None:
        """Stop DTP server and reset transport state.

        Does NOT tear down libcsp (global state, can't un-init).
        """
        if self._dtp_server:
            self._dtp_server.stop()
            self._dtp_server = None
        self._connected = False

    def _send_request(
        self, request: DeployRequest, retries: int = 1,
    ) -> DeployResponse:
        """Send a deploy request via csp_transaction() and parse the response.

        Args:
            request: The protobuf request message.
            retries: Number of retry attempts on timeout (default 1).

        Returns:
            The protobuf response message.

        Raises:
            TransportError: If transaction fails after all retries.
        """
        if not self._connected:
            raise TransportError("Not connected")

        outbuf = bytearray(request.SerializeToString())
        inbuf = bytearray(4096)

        last_error = None
        for attempt in range(1 + retries):
            try:
                resp_len = libcsp.transaction(
                    libcsp.CSP_PRIO_NORM, self.agent_node,
                    CSP_DEPLOY_PORT, self.timeout_ms, outbuf, inbuf,
                )
                response = DeployResponse()
                response.ParseFromString(bytes(inbuf[:resp_len]))
                return response
            except libcsp.Error as e:
                last_error = TransportError(f"CSP transaction failed: {e}")

        raise last_error

    def _compute_checksum(self, file_path: str) -> str:
        """Compute SHA256 checksum of a file (first 8 hex chars)."""
        return compute_file_hash(file_path)

    def _get_file_size(self, file_path: str) -> int:
        """Get the size of a file in bytes."""
        return os.path.getsize(file_path)

    def _next_payload_id(self) -> int:
        """Generate the next unique payload ID."""
        self._payload_counter += 1
        return self._payload_counter

    def deploy(
        self,
        app_name: str,
        local_path: str,
        remote_path: str,
        param_name: Optional[str] = None,
        appsys_node: Optional[int] = None,
        run_node: Optional[int] = None,
        expected_checksum: Optional[str] = None,
        services: Optional[list[tuple[str, str]]] = None,
        force: bool = False,
        on_progress: Optional[callable] = None,
    ) -> DeployResult:
        """Deploy a file via CSP/DTP.

        The satellite agent will:
        1. Stop the app (via libparam)
        2. Backup the current file
        3. Download the new file via DTP
        4. Verify the checksum
        5. Install and start the app
        """
        if not self._connected:
            return DeployResult(success=False, error_message="Not connected")

        checksum = expected_checksum or self._compute_checksum(local_path)
        file_size = self._get_file_size(local_path)
        payload_id = self._next_payload_id()

        # Start DTP server to serve the binary
        self._dtp_server = DTPServer(
            local_path=local_path,
            payload_id=payload_id,
            node_address=self.ground_node,
            mtu=self.dtp_mtu,
            on_progress=on_progress,
        )
        self._dtp_server.start()

        try:
            request = DeployRequest()
            request.command = DeployCommand.CMD_DEPLOY
            request.app_name = app_name
            request.remote_path = remote_path
            request.expected_checksum = checksum
            request.expected_size = file_size
            request.payload_id = payload_id
            request.dtp_server_node = self.ground_node
            request.dtp_server_port = self.dtp_port
            request.dtp_mtu = self.dtp_mtu
            request.dtp_throughput = self.dtp_throughput
            request.dtp_timeout = self.dtp_timeout

            file_stat = os.stat(local_path)
            request.file_mode = stat.S_IMODE(file_stat.st_mode)

            if param_name:
                request.param_name = param_name
            if appsys_node:
                request.appsys_node = appsys_node
            if run_node:
                request.run_node = run_node

            response = self._send_request(request)

            return DeployResult(
                success=response.success,
                backup_path=response.backup_path if response.backup_path else None,
                error_code=response.error_code if not response.success else None,
                error_message=response.error_message if not response.success else None,
                file_hash=checksum[:8] if response.success else None,
            )

        finally:
            if self._dtp_server:
                self._dtp_server.stop()
                self._dtp_server = None

    def rollback(
        self,
        app_name: str,
        backup_hash: Optional[str] = None,
        remote_path: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> DeployResult:
        """Rollback to a previous version via CSP."""
        if not self._connected:
            return DeployResult(success=False, error_message="Not connected")

        request = DeployRequest()
        request.command = DeployCommand.CMD_ROLLBACK
        request.app_name = app_name
        if backup_hash:
            request.rollback_hash = backup_hash

        try:
            response = self._send_request(request)
            return DeployResult(
                success=response.success,
                error_code=response.error_code if not response.success else None,
                error_message=response.error_message if not response.success else None,
                backup_path=response.backup_path if response.backup_path else None,
            )
        except TransportError as e:
            return DeployResult(success=False, error_message=str(e))

    def get_status(self) -> dict[str, AppStatus]:
        """Get status of deployed applications via CSP."""
        if not self._connected:
            return {}

        request = DeployRequest()
        request.command = DeployCommand.CMD_STATUS

        try:
            response = self._send_request(request)
            result = {}
            for app in response.apps:
                result[app.app_name] = AppStatus(
                    app_name=app.app_name,
                    running=app.running,
                    file_hash=app.file_hash if app.file_hash else None,
                    remote_path=app.remote_path,
                )
            return result
        except TransportError:
            return {}

    def list_backups(self, app_name: str) -> list[BackupInfo]:
        """List available backups via CSP."""
        if not self._connected:
            return []

        request = DeployRequest()
        request.command = DeployCommand.CMD_LIST_VERSIONS
        request.app_name = app_name

        try:
            response = self._send_request(request)
            return [
                BackupInfo(
                    version=b.version,
                    timestamp=b.timestamp,
                    file_hash=b.hash if b.hash else None,
                    path=b.path,
                )
                for b in response.backups
            ]
        except TransportError:
            return []

    def get_logs(self, app_name: str, service: str, lines: int = 100) -> Optional[str]:
        """Fetch service logs via CSP."""
        if not self._connected:
            return None

        request = DeployRequest()
        request.command = DeployCommand.CMD_LOGS
        request.app_name = app_name
        request.log_lines = lines

        try:
            response = self._send_request(request)
            if response.success and response.log_output:
                return response.log_output
            return None
        except TransportError:
            return None


def _parse_zmq_host(zmq_endpoint: str) -> str:
    """Extract hostname from a ZMQ endpoint string.

    Handles formats like "tcp://localhost:4040", "tcp://192.168.1.1:6000",
    or just "localhost".
    """
    if "://" in zmq_endpoint:
        from urllib.parse import urlparse
        parsed = urlparse(zmq_endpoint)
        return parsed.hostname or "localhost"
    return zmq_endpoint
