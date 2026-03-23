"""CSP transport implementation using ZMQ.

Uses PUB/SUB sockets through zmqproxy, matching libcsp's csp_zmqhub wire
format. The zmqproxy is an XSUB/XPUB forwarder on default ports 6000/7000.
"""

import struct
import time
from typing import Optional
from urllib.parse import urlparse

import zmq

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


# CSP v2 header: 48 bits (6 bytes), big-endian
# bits 47-46: priority (2)
# bits 45-32: destination (14)
# bits 31-18: source (14)
# bits 17-12: dest_port (6)
# bits 11-6:  src_port (6)
# bits 5-0:   flags (6)
CSP_HEADER_SIZE = 6
CSP_DEPLOY_PORT = 20  # Port for deploy commands

# Default zmqproxy ports — 9600/9601 avoids conflicts with macOS AirPlay (7000)
# and other common services. Override via config if zmqproxy uses different ports.
ZMQ_PROXY_SUB_PORT = 9600  # Clients publish (TX) to this port
ZMQ_PROXY_PUB_PORT = 9601  # Clients subscribe (RX) from this port


def _parse_zmq_host(zmq_endpoint: str) -> str:
    """Extract hostname from a ZMQ endpoint string.

    Handles formats like "tcp://localhost:4040", "tcp://192.168.1.1:6000",
    or just "localhost".
    """
    if "://" in zmq_endpoint:
        parsed = urlparse(zmq_endpoint)
        return parsed.hostname or "localhost"
    return zmq_endpoint


class CSPTransport(Transport):
    """Transport implementation using CSP over ZMQ.

    Uses PUB/SUB sockets through zmqproxy, matching the libcsp zmqhub wire
    format. PUB connects to zmqproxy's subscribe port (6000), SUB connects
    to zmqproxy's publish port (7000).

    Architecture:
    - Ground (this code) sends deploy commands via CSP over ZMQ PUB
    - Agent responses arrive via ZMQ SUB
    - Ground runs a DTP server to serve binaries for deploy
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
        zmq_pub_port: int = ZMQ_PROXY_SUB_PORT,
        zmq_sub_port: int = ZMQ_PROXY_PUB_PORT,
    ):
        """Initialize CSP transport.

        Args:
            zmq_endpoint: ZMQ host or endpoint (e.g., "tcp://localhost:6000"
                or "localhost"). The host is extracted.
            agent_node: CSP node address of the satdeploy-agent.
            ground_node: CSP node address of this ground station.
            backup_dir: Remote directory for backups (on satellite).
            dtp_port: Port for DTP server (default 7).
            timeout_ms: Timeout for CSP operations in milliseconds.
            zmq_pub_port: zmqproxy subscribe port (TX). Default 6000.
            zmq_sub_port: zmqproxy publish port (RX). Default 7000.
        """
        self.zmq_endpoint = zmq_endpoint
        self.zmq_host = _parse_zmq_host(zmq_endpoint)
        self.agent_node = agent_node
        self.ground_node = ground_node
        self.backup_dir = backup_dir
        self.dtp_port = dtp_port
        self.timeout_ms = timeout_ms
        self.zmq_pub_port = zmq_pub_port
        self.zmq_sub_port = zmq_sub_port

        self._context: Optional[zmq.Context] = None
        self._pub: Optional[zmq.Socket] = None  # TX: send CSP packets
        self._sub: Optional[zmq.Socket] = None  # RX: receive CSP packets
        self._dtp_server: Optional[DTPServer] = None
        self._payload_counter = 0

    def connect(self) -> None:
        """Establish ZMQ PUB/SUB connections through zmqproxy.

        Raises:
            TransportError: If connection fails.
        """
        pub_endpoint = f"tcp://{self.zmq_host}:{self.zmq_pub_port}"
        sub_endpoint = f"tcp://{self.zmq_host}:{self.zmq_sub_port}"
        try:
            self._context = zmq.Context()

            # PUB socket for sending CSP packets (connects to zmqproxy's
            # subscribe/XSUB port — our published messages get forwarded)
            self._pub = self._context.socket(zmq.PUB)
            self._pub.setsockopt(zmq.LINGER, 0)
            self._pub.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
            self._pub.connect(pub_endpoint)

            # SUB socket for receiving CSP packets (connects to zmqproxy's
            # publish/XPUB port — we receive forwarded messages)
            self._sub = self._context.socket(zmq.SUB)
            self._sub.setsockopt(zmq.LINGER, 0)
            self._sub.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            # Subscribe to packets addressed to our ground node, all priorities
            for priority in range(4):
                filt = struct.pack(">H", (priority << 14) | self.ground_node)
                self._sub.setsockopt(zmq.SUBSCRIBE, filt)
            self._sub.connect(sub_endpoint)

        except zmq.ZMQError as e:
            raise TransportError(
                f"Failed to connect to zmqproxy at {self.zmq_host}: {e}"
            )

        # ZMQ PUB/SUB "slow joiner" — subscriptions need time to propagate
        # through zmqproxy before messages can be received. 0.3s handles
        # rapid reconnects (e.g., push immediately followed by list).
        time.sleep(0.3)

    def disconnect(self) -> None:
        """Close ZMQ connections."""
        if self._dtp_server:
            self._dtp_server.stop()
            self._dtp_server = None
        if self._pub:
            self._pub.close(linger=0)
            self._pub = None
        if self._sub:
            self._sub.close(linger=0)
            self._sub = None
        if self._context:
            self._context.term()
            self._context = None

    def _build_csp_header(self, dest: int, dest_port: int, src_port: int = 0) -> bytes:
        """Build a CSP v2 header (6 bytes).

        Args:
            dest: Destination node address (14 bits).
            dest_port: Destination port (6 bits).
            src_port: Source port (6 bits).

        Returns:
            6-byte CSP v2 header.
        """
        priority = 2  # Normal priority
        id2 = (
            (priority & 0x3) << 46 |
            (dest & 0x3FFF) << 32 |
            (self.ground_node & 0x3FFF) << 18 |
            (dest_port & 0x3F) << 12 |
            (src_port & 0x3F) << 6
        )
        # Pack as big-endian: shift left 16 in a uint64, take first 6 bytes
        raw = struct.pack(">Q", id2 << 16)
        return raw[:6]

    @staticmethod
    def _parse_csp_header(data: bytes) -> dict:
        """Parse a CSP v2 header from 6 bytes.

        Returns:
            Dict with pri, dst, src, dport, sport, flags.
        """
        raw = data[:6] + b'\x00\x00'  # Pad to 8 bytes for uint64
        id2 = struct.unpack(">Q", raw)[0] >> 16
        return {
            "pri": (id2 >> 46) & 0x3,
            "dst": (id2 >> 32) & 0x3FFF,
            "src": (id2 >> 18) & 0x3FFF,
            "dport": (id2 >> 12) & 0x3F,
            "sport": (id2 >> 6) & 0x3F,
            "flags": id2 & 0x3F,
        }

    def _send_request(
        self, request: DeployRequest, retries: int = 1,
    ) -> DeployResponse:
        """Send a deploy request and wait for response.

        Retries on timeout to handle ZMQ slow-joiner races where the SUB
        subscription hasn't propagated through zmqproxy yet.

        Args:
            request: The protobuf request message.
            retries: Number of retry attempts on timeout (default 1).

        Returns:
            The protobuf response message.

        Raises:
            TransportError: If send/receive fails after all retries.
        """
        if not self._pub or not self._sub:
            raise TransportError("Not connected")

        # Build packet: CSP v2 header + protobuf payload
        header = self._build_csp_header(self.agent_node, CSP_DEPLOY_PORT)
        payload = request.SerializeToString()
        packet = header + payload

        last_error = None
        for attempt in range(1 + retries):
            try:
                self._pub.send(packet)

                # Loop to skip non-deploy packets (DTP RDP packets on ports 7/8
                # may arrive while the DTP server is running concurrently)
                deadline = time.time() + (self.timeout_ms / 1000)
                while time.time() < deadline:
                    remaining_ms = int((deadline - time.time()) * 1000)
                    if remaining_ms <= 0:
                        break

                    self._sub.setsockopt(zmq.RCVTIMEO, remaining_ms)
                    response_data = self._sub.recv()

                    if len(response_data) < CSP_HEADER_SIZE:
                        continue

                    # Parse CSP header to check the source port
                    resp_hdr = self._parse_csp_header(response_data[:CSP_HEADER_SIZE])
                    # Only accept packets from the deploy port (port 20)
                    if resp_hdr["sport"] != CSP_DEPLOY_PORT:
                        continue

                    response_payload = response_data[CSP_HEADER_SIZE:]
                    response = DeployResponse()
                    response.ParseFromString(response_payload)
                    return response

                last_error = TransportError("Request timed out")
                if attempt < retries:
                    time.sleep(0.3)  # Brief pause before retry

            except zmq.Again:
                last_error = TransportError("Request timed out")
                if attempt < retries:
                    time.sleep(0.3)
            except zmq.ZMQError as e:
                raise TransportError(f"ZMQ error: {e}")

        raise last_error

    def _compute_checksum(self, file_path: str) -> str:
        """Compute SHA256 checksum of a file.

        Args:
            file_path: Path to the file.

        Returns:
            First 8 characters of the hex digest.
        """
        return compute_file_hash(file_path)

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
        services: Optional[list[tuple[str, str]]] = None,
        on_progress: Optional[callable] = None,
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
            services: Ignored for CSP transport (agent manages services).

        Returns:
            DeployResult indicating success/failure and backup path.
        """
        if not self._pub:
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
            zmq_pub_port=self.zmq_pub_port,
            zmq_sub_port=self.zmq_sub_port,
            on_progress=on_progress,
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
        if not self._pub:
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

    def get_status(self) -> dict[str, AppStatus]:
        """Get status of deployed applications via CSP.

        Returns:
            Dictionary mapping app names to their status.
        """
        if not self._pub:
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
        if not self._pub:
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

    def get_logs(self, app_name: str, service: str, lines: int = 100) -> Optional[str]:
        """Fetch service logs via CSP.

        Args:
            app_name: Name of the application.
            service: Service name (e.g., controller.service).
            lines: Number of log lines to fetch.

        Returns:
            Log output string, or None on failure.
        """
        if not self._pub:
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
