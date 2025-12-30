"""CSP transport implementation using ZMQ."""

import hashlib
import struct
from typing import Optional

import zmq

from satdeploy.csp.proto import (
    DeployCommand,
    DeployRequest,
    DeployResponse,
)
from satdeploy.csp.dtp_server import DTPServer
from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)


# CSP header format for ZMQ packets
# CSP header is 4 bytes: priority(2) | source(5) | dest(5) | dest_port(6) | src_port(6) | reserved(8)
CSP_HEADER_SIZE = 4
CSP_DEPLOY_PORT = 20  # Port for deploy commands


class CSPTransport(Transport):
    """Transport implementation using CSP over ZMQ.

    This transport communicates with a satdeploy-agent running on the
    satellite via the Cubesat Space Protocol (CSP) over ZMQ. The agent
    handles all deployment operations locally, pulling binaries via DTP.

    Architecture:
    - Ground (this code) sends deploy commands via CSP
    - Ground runs a DTP server to serve binaries
    - Satellite agent receives commands, pulls binaries via DTP,
      and manages local installation/backup/rollback
    """

    def __init__(
        self,
        zmq_endpoint: str,
        agent_node: int,
        ground_node: int,
        backup_dir: str,
        dtp_port: int = 7,
        timeout_ms: int = 30000,
    ):
        """Initialize CSP transport.

        Args:
            zmq_endpoint: ZMQ endpoint to connect to (e.g., "tcp://localhost:4040").
            agent_node: CSP node address of the satdeploy-agent.
            ground_node: CSP node address of this ground station.
            backup_dir: Remote directory for backups (on satellite).
            dtp_port: Port for DTP server (default 7).
            timeout_ms: Timeout for CSP operations in milliseconds.
        """
        self.zmq_endpoint = zmq_endpoint
        self.agent_node = agent_node
        self.ground_node = ground_node
        self.backup_dir = backup_dir
        self.dtp_port = dtp_port
        self.timeout_ms = timeout_ms

        self._context: Optional[zmq.Context] = None
        self._socket: Optional[zmq.Socket] = None
        self._dtp_server: Optional[DTPServer] = None
        self._payload_counter = 0

    def connect(self) -> None:
        """Establish ZMQ connection.

        Raises:
            TransportError: If connection fails.
        """
        try:
            self._context = zmq.Context()
            # Use DEALER socket for async request/reply
            self._socket = self._context.socket(zmq.DEALER)
            self._socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            self._socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
            self._socket.connect(self.zmq_endpoint)
        except zmq.ZMQError as e:
            raise TransportError(f"Failed to connect to {self.zmq_endpoint}: {e}")

    def disconnect(self) -> None:
        """Close ZMQ connection."""
        if self._dtp_server:
            self._dtp_server.stop()
            self._dtp_server = None
        if self._socket:
            self._socket.close()
            self._socket = None
        if self._context:
            self._context.term()
            self._context = None

    def _build_csp_header(self, dest: int, dest_port: int, src_port: int = 0) -> bytes:
        """Build a CSP header.

        Args:
            dest: Destination node address.
            dest_port: Destination port.
            src_port: Source port.

        Returns:
            4-byte CSP header.
        """
        # CSP header format (32 bits):
        # bits 31-30: priority (2 bits)
        # bits 29-25: source address (5 bits)
        # bits 24-20: destination address (5 bits)
        # bits 19-14: destination port (6 bits)
        # bits 13-8: source port (6 bits)
        # bits 7-0: reserved/flags (8 bits)
        priority = 2  # Normal priority
        header = (
            (priority & 0x3) << 30 |
            (self.ground_node & 0x1F) << 25 |
            (dest & 0x1F) << 20 |
            (dest_port & 0x3F) << 14 |
            (src_port & 0x3F) << 8
        )
        return struct.pack(">I", header)

    def _send_request(self, request: DeployRequest) -> DeployResponse:
        """Send a deploy request and wait for response.

        Args:
            request: The protobuf request message.

        Returns:
            The protobuf response message.

        Raises:
            TransportError: If send/receive fails or times out.
        """
        if not self._socket:
            raise TransportError("Not connected")

        # Build packet: CSP header + protobuf payload
        header = self._build_csp_header(self.agent_node, CSP_DEPLOY_PORT)
        payload = request.SerializeToString()
        packet = header + payload

        try:
            self._socket.send(packet)
            response_data = self._socket.recv()

            # Skip CSP header in response
            if len(response_data) > CSP_HEADER_SIZE:
                response_payload = response_data[CSP_HEADER_SIZE:]
            else:
                response_payload = response_data

            response = DeployResponse()
            response.ParseFromString(response_payload)
            return response

        except zmq.Again:
            raise TransportError("Request timed out")
        except zmq.ZMQError as e:
            raise TransportError(f"ZMQ error: {e}")

    def _compute_checksum(self, file_path: str) -> str:
        """Compute SHA256 checksum of a file.

        Args:
            file_path: Path to the file.

        Returns:
            First 8 characters of the hex digest.
        """
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()[:8]

    def _get_file_size(self, file_path: str) -> int:
        """Get the size of a file in bytes."""
        import os
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
        service_name: Optional[str] = None,
    ) -> DeployResult:
        """Deploy a binary via CSP/DTP.

        The satellite agent will:
        1. Stop the app (via libparam)
        2. Backup the current binary
        3. Download the new binary via DTP
        4. Verify the checksum
        5. Install the binary
        6. Start the app (via libparam)

        Args:
            app_name: Name of the application.
            local_path: Path to the local binary.
            remote_path: Path on the satellite where binary should be installed.
            param_name: The libparam parameter name (e.g., "mng_dipp").
            appsys_node: The app-sys-manager CSP node address.
            run_node: The CSP node address where app runs.
            expected_checksum: Expected SHA256 checksum (first 8 chars).
            service_name: Ignored for CSP transport (uses param_name instead).

        Returns:
            DeployResult indicating success/failure and backup path.
        """
        if not self._socket:
            return DeployResult(success=False, error_message="Not connected")

        # Compute checksum and size
        checksum = expected_checksum or self._compute_checksum(local_path)
        file_size = self._get_file_size(local_path)
        payload_id = self._next_payload_id()

        # Start DTP server to serve the binary
        self._dtp_server = DTPServer(
            local_path=local_path,
            payload_id=payload_id,
            zmq_endpoint=self.zmq_endpoint,
            node_address=self.ground_node,
        )
        self._dtp_server.start()

        try:
            # Build deploy request
            request = DeployRequest()
            request.command = DeployCommand.CMD_DEPLOY
            request.app_name = app_name
            request.remote_path = remote_path
            request.expected_checksum = checksum
            request.expected_size = file_size
            request.payload_id = payload_id
            request.dtp_server_node = self.ground_node
            request.dtp_server_port = self.dtp_port

            if param_name:
                request.param_name = param_name
            if appsys_node:
                request.appsys_node = appsys_node
            if run_node:
                request.run_node = run_node

            # Send request and wait for response
            response = self._send_request(request)

            return DeployResult(
                success=response.success,
                backup_path=response.backup_path if response.backup_path else None,
                error_code=response.error_code if not response.success else None,
                error_message=response.error_message if not response.success else None,
            )

        finally:
            # Stop DTP server
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
        """Rollback to a previous version via CSP.

        Args:
            app_name: Name of the application.
            backup_hash: Specific backup hash to restore, or None for latest.
            remote_path: Ignored for CSP (agent knows the path).
            service_name: Ignored for CSP (uses libparam).

        Returns:
            DeployResult indicating success/failure.
        """
        if not self._socket:
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
            )
        except TransportError as e:
            return DeployResult(success=False, error_message=str(e))

    def get_status(self, apps: Optional[dict] = None) -> dict[str, AppStatus]:
        """Get status of deployed applications via CSP.

        Args:
            apps: Ignored for CSP (agent reports all apps).

        Returns:
            Dictionary mapping app names to their status.
        """
        if not self._socket:
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
                    binary_hash=app.binary_hash if app.binary_hash else None,
                    remote_path=app.remote_path,
                )
            return result
        except TransportError:
            return {}

    def list_backups(self, app_name: str) -> list[BackupInfo]:
        """List available backups via CSP.

        Args:
            app_name: Name of the application.

        Returns:
            List of BackupInfo, sorted newest first.
        """
        if not self._socket:
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
                    binary_hash=b.hash if b.hash else None,
                    path=b.path,
                )
                for b in response.backups
            ]
        except TransportError:
            return []

    def verify(self, app_name: str, remote_path: str) -> Optional[str]:
        """Verify the installed binary checksum via CSP.

        Args:
            app_name: Name of the application.
            remote_path: Path to the binary on target.

        Returns:
            The checksum (first 8 chars of SHA256), or None if not found.
        """
        if not self._socket:
            return None

        request = DeployRequest()
        request.command = DeployCommand.CMD_VERIFY
        request.app_name = app_name
        request.remote_path = remote_path

        try:
            response = self._send_request(request)
            if response.success and response.actual_checksum:
                return response.actual_checksum
            return None
        except TransportError:
            return None
