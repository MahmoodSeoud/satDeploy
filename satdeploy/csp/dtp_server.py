"""DTP (Data Transfer Protocol) server using libcsp Python bindings.

Serves binaries to satellite agents via the DTP protocol over CSP.
Uses libcsp for all CSP operations (socket, bind, accept, send) —
RDP handshake is handled internally by libcsp.

Protocol flow:
  1. Client opens RDP connection to server on CSP port 7
  2. Client sends dtp_meta_req_t (metadata request)
  3. Server responds with dtp_meta_resp_t (metadata response)
  4. Server sends data packets connectionlessly on CSP port 8
"""

import os
import struct
import threading
import time
from typing import Optional

try:
    import libcsp_py3 as libcsp
except ImportError:
    libcsp = None


# DTP protocol constants
DTP_PORT = 7            # CSP port for DTP metadata (RDP connection)
DTP_DATA_PORT = 8       # CSP port for DTP data packets (connectionless)
DTP_READ_TIMEOUT_MS = 10000  # Timeout for reading metadata request after accept


class DTPServer:
    """DTP server using libcsp for CSP/RDP transport.

    Binds a libcsp socket on DTP_PORT with RDP enabled. When a client
    connects, parses the DTP metadata request, sends a metadata response,
    then streams data packets connectionlessly on DTP_DATA_PORT.
    """

    def __init__(
        self,
        local_path: str,
        payload_id: int,
        node_address: int,
        mtu: int = 256,
        on_progress: Optional[callable] = None,
    ):
        self.local_path = local_path
        self.payload_id = payload_id
        self.node_address = node_address
        self.mtu = mtu
        self.on_progress = on_progress

        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._file_data: Optional[bytes] = None
        self._file_size: int = 0

    def _load_file(self) -> None:
        """Load the file into memory."""
        with open(self.local_path, "rb") as f:
            self._file_data = f.read()
        self._file_size = len(self._file_data)

    def _build_dtp_meta_response(self, transfer_size: int) -> bytes:
        """Build dtp_meta_resp_t (8 bytes).

        Format: uint32_t size_in_bytes + uint32_t total_payload_size
        Sent in little-endian (native byte order on ARM target).
        """
        return struct.pack("<II", transfer_size, self._file_size)

    def _send_data_packets(self, client_node: int, session_id: int, mtu: int) -> None:
        """Send all data packets connectionlessly on port 8.

        Data packet format:
          uint32_t bytes_sent (offset into file)
          uint32_t session_id
          payload data (mtu - 8 bytes)
        """
        if not self._file_data:
            return

        effective_payload = mtu - 8  # 2 x uint32_t header
        offset = 0

        while offset < self._file_size and self._running:
            end = min(offset + effective_payload, self._file_size)
            chunk = self._file_data[offset:end]

            data_header = struct.pack("<II", offset, session_id)
            packet_data = data_header + chunk

            packet = None
            try:
                packet = libcsp.buffer_get(0)
                libcsp.packet_set_data(packet, packet_data)
                libcsp.sendto(
                    libcsp.CSP_PRIO_NORM, client_node,
                    DTP_DATA_PORT, 0, libcsp.CSP_O_NONE, packet,
                )
                packet = None  # sendto took ownership
            except libcsp.Error:
                if packet is not None:
                    libcsp.buffer_free(packet)
                time.sleep(0.001)
                continue

            offset = end
            if self.on_progress:
                self.on_progress(offset, self._file_size)
            # Pace the transmission
            time.sleep(0.001)

    def _server_loop(self) -> None:
        """Main server loop: accept RDP connections, handle DTP requests."""
        self._load_file()

        # Create socket with RDP required
        sock = libcsp.socket(libcsp.CSP_SO_RDPREQ)
        libcsp.bind(sock, DTP_PORT)
        libcsp.listen(sock, 5)

        try:
            while self._running:
                try:
                    # Accept with 100ms timeout so we can check _running
                    conn = libcsp.accept(sock, 100)
                    if conn is None:
                        continue

                    # Read the metadata request packet
                    packet = libcsp.read(conn, DTP_READ_TIMEOUT_MS)
                    if packet is None:
                        libcsp.close(conn)
                        continue

                    req_data = libcsp.packet_get_data(packet)
                    client_node = libcsp.conn_src(conn)

                    self._handle_dtp_request(req_data, client_node, conn, packet)
                    libcsp.close(conn)
                except Exception:
                    # Don't let a single request kill the server thread
                    continue
        finally:
            libcsp.close_socket(sock)

    def _handle_dtp_request(
        self, req_data: bytes, client_node: int,
        conn, request_packet,
    ) -> None:
        """Parse DTP metadata request and respond + send data.

        dtp_meta_req_t layout (80 bytes):
          uint32_t throughput
          uint8_t  nof_intervals
          uint8_t  payload_id
          uint8_t  reserved1
          uint8_t  reserved2
          uint32_t session_id
          uint16_t mtu
          uint16_t keep_alive_interval (big-endian converted)
          interval_t intervals[8]  (8 x {uint32_t start, uint32_t end})
        """
        if len(req_data) < 16:
            return

        # Parse metadata request (little-endian from ARM client)
        throughput, nof_intervals, payload_id, _, _ = struct.unpack_from(
            "<IBBBB", req_data, 0,
        )
        session_id = struct.unpack_from("<I", req_data, 8)[0]
        mtu = struct.unpack_from("<H", req_data, 12)[0]

        effective_mtu = mtu if mtu > 0 else self.mtu

        # Build and send metadata response
        meta_resp = self._build_dtp_meta_response(self._file_size)
        reply = libcsp.buffer_get(0)
        try:
            libcsp.packet_set_data(reply, meta_resp)
            libcsp.sendto_reply(request_packet, reply, libcsp.CSP_O_NONE)
        except libcsp.Error:
            libcsp.buffer_free(reply)
            return

        # Brief delay for client to set up connectionless receive
        time.sleep(0.1)

        # Send data packets connectionlessly on port 8
        self._send_data_packets(client_node, session_id, effective_mtu)

    def start(self) -> None:
        """Start the DTP server in a background thread.

        Requires libcsp to be already initialized (by CSPTransport.connect()).
        """
        if self._running:
            return

        self._running = True
        self._server_thread = threading.Thread(target=self._server_loop, daemon=True)
        self._server_thread.start()

    def stop(self) -> None:
        """Stop the DTP server."""
        self._running = False

        if self._server_thread:
            self._server_thread.join(timeout=2.0)
            self._server_thread = None

        self._file_data = None
