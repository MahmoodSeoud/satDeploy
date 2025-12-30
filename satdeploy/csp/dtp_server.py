"""DTP (Data Transfer Protocol) server for serving binaries."""

import struct
import threading
from typing import Optional

import zmq


# DTP protocol constants
DTP_PORT = 7  # CSP port for DTP
DTP_DATA_PORT = 8  # CSP port for DTP data packets
DTP_MTU = 200  # Default MTU size
CSP_HEADER_SIZE = 4


class DTPServer:
    """DTP server that serves files for satellite download.

    This server runs on the ground station and serves binary files
    to the satellite's DTP client. The satellite connects via CSP,
    sends a metadata request, and then receives data packets.

    Protocol flow:
    1. Client connects to port 7 (DTP_PORT) and sends metadata request
    2. Server responds with metadata (file size, MTU, etc.)
    3. Server sends data packets to client on port 8 (DTP_DATA_PORT)
    4. Client acknowledges completion

    This is a simplified implementation for ground-station use.
    The actual satellite DTP client handles the receiving side.
    """

    def __init__(
        self,
        local_path: str,
        payload_id: int,
        zmq_endpoint: str,
        node_address: int,
        mtu: int = DTP_MTU,
    ):
        """Initialize DTP server.

        Args:
            local_path: Path to the file to serve.
            payload_id: Unique identifier for this payload.
            zmq_endpoint: ZMQ endpoint to bind to.
            node_address: CSP node address of this server.
            mtu: Maximum transmission unit size.
        """
        self.local_path = local_path
        self.payload_id = payload_id
        self.zmq_endpoint = zmq_endpoint
        self.node_address = node_address
        self.mtu = mtu

        self._context: Optional[zmq.Context] = None
        self._socket: Optional[zmq.Socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._file_data: Optional[bytes] = None
        self._file_size: int = 0

    def _load_file(self) -> None:
        """Load the file into memory."""
        with open(self.local_path, "rb") as f:
            self._file_data = f.read()
        self._file_size = len(self._file_data)

    def _build_csp_header(self, dest: int, dest_port: int, src_port: int = DTP_PORT) -> bytes:
        """Build a CSP header.

        Args:
            dest: Destination node address.
            dest_port: Destination port.
            src_port: Source port.

        Returns:
            4-byte CSP header.
        """
        priority = 2  # Normal priority
        header = (
            (priority & 0x3) << 30 |
            (self.node_address & 0x1F) << 25 |
            (dest & 0x1F) << 20 |
            (dest_port & 0x3F) << 14 |
            (src_port & 0x3F) << 8
        )
        return struct.pack(">I", header)

    def _parse_csp_header(self, header: bytes) -> tuple[int, int, int, int]:
        """Parse a CSP header.

        Args:
            header: 4-byte CSP header.

        Returns:
            Tuple of (source, dest, dest_port, src_port).
        """
        value = struct.unpack(">I", header)[0]
        src = (value >> 25) & 0x1F
        dest = (value >> 20) & 0x1F
        dest_port = (value >> 14) & 0x3F
        src_port = (value >> 8) & 0x3F
        return src, dest, dest_port, src_port

    def _handle_metadata_request(self, client_node: int, request_data: bytes) -> bytes:
        """Handle a DTP metadata request.

        Args:
            client_node: The requesting client's CSP node address.
            request_data: The metadata request payload.

        Returns:
            Metadata response payload.
        """
        # DTP metadata response format:
        # - throughput (4 bytes): max server throughput in KB/sec
        # - nof_intervals (1 byte): number of segments
        # - payload_id (2 bytes): payload identifier
        # - mtu (2 bytes): MTU size
        # - intervals: (start, stop) pairs for transfer segments

        # Calculate number of packets needed
        data_per_packet = self.mtu - 4  # 4 bytes for sequence number
        num_packets = (self._file_size + data_per_packet - 1) // data_per_packet

        # Build response
        throughput = 100  # 100 KB/sec (conservative for radio link)
        nof_intervals = 1
        interval_start = 0
        interval_stop = self._file_size

        response = struct.pack(
            ">IBHHII",
            throughput,
            nof_intervals,
            self.payload_id,
            self.mtu,
            interval_start,
            interval_stop,
        )
        return response

    def _send_data_packets(self, client_node: int) -> None:
        """Send all data packets to the client.

        Args:
            client_node: The client's CSP node address.
        """
        if not self._socket or not self._file_data:
            return

        data_per_packet = self.mtu - 4  # 4 bytes for sequence number
        offset = 0
        seq = 0

        while offset < self._file_size:
            # Get chunk of data
            end = min(offset + data_per_packet, self._file_size)
            chunk = self._file_data[offset:end]

            # Build packet with sequence number
            seq_bytes = struct.pack(">I", offset)
            packet_data = seq_bytes + chunk

            # Build CSP packet
            header = self._build_csp_header(client_node, DTP_DATA_PORT, DTP_DATA_PORT)
            packet = header + packet_data

            try:
                self._socket.send(packet, zmq.NOBLOCK)
            except zmq.Again:
                # Socket buffer full, wait a bit
                import time
                time.sleep(0.001)
                continue

            offset = end
            seq += 1

    def _server_loop(self) -> None:
        """Main server loop."""
        self._load_file()

        while self._running:
            try:
                # Wait for incoming packets
                if self._socket.poll(100):  # 100ms timeout
                    data = self._socket.recv()

                    if len(data) < CSP_HEADER_SIZE:
                        continue

                    # Parse CSP header
                    header = data[:CSP_HEADER_SIZE]
                    payload = data[CSP_HEADER_SIZE:]
                    src, dest, dest_port, src_port = self._parse_csp_header(header)

                    # Handle metadata request on DTP port
                    if dest_port == DTP_PORT:
                        response = self._handle_metadata_request(src, payload)
                        response_header = self._build_csp_header(src, src_port, DTP_PORT)
                        self._socket.send(response_header + response)

                        # Send data packets
                        self._send_data_packets(src)

            except zmq.ZMQError:
                if self._running:
                    continue
                break

    def start(self) -> None:
        """Start the DTP server."""
        if self._running:
            return

        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.DEALER)
        self._socket.connect(self.zmq_endpoint)

        self._running = True
        self._server_thread = threading.Thread(target=self._server_loop, daemon=True)
        self._server_thread.start()

    def stop(self) -> None:
        """Stop the DTP server."""
        self._running = False

        if self._server_thread:
            self._server_thread.join(timeout=1.0)
            self._server_thread = None

        if self._socket:
            self._socket.close()
            self._socket = None

        if self._context:
            self._context.term()
            self._context = None

        self._file_data = None
