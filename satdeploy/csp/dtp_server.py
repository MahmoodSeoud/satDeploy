"""DTP (Data Transfer Protocol) server for serving binaries.

Implements the DTP protocol over CSP with proper RDP (Reliable Datagram
Protocol) support for the metadata exchange, matching libcsp/libdtp's
wire format.

Protocol flow:
  1. Client opens RDP connection to server on CSP port 7
  2. Client sends dtp_meta_req_t (metadata request)
  3. Server responds with dtp_meta_resp_t (metadata response)
  4. Server closes RDP connection
  5. Server sends data packets connectionlessly on CSP port 8

All packets flow through zmqproxy as raw ZMQ messages containing
CSP v2 headers (6 bytes).

CSP v2 header (48 bits, big-endian):
  bits 47-46: priority (2)
  bits 45-32: destination (14)
  bits 31-18: source (14)
  bits 17-12: dest_port (6)
  bits 11-6:  src_port (6)
  bits 5-0:   flags (6)

CSP flags:
  bit 1: FRDP (RDP protocol)

RDP header (5 bytes, appended to CSP payload):
  byte 0:   flags (SYN=0x08, ACK=0x04, EAK=0x02, RST=0x01)
  bytes 1-2: seq_nr (big-endian uint16)
  bytes 3-4: ack_nr (big-endian uint16)
"""

import os
import random
import struct
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import zmq


# DTP protocol constants
DTP_PORT = 7            # CSP port for DTP metadata (RDP connection)
DTP_DATA_PORT = 8       # CSP port for DTP data packets (connectionless)
CSP_HEADER_SIZE = 6     # CSP v2 header size

# CSP flags
CSP_FRDP = 0x02         # Use RDP protocol

# RDP control flags
RDP_SYN = 0x08
RDP_ACK = 0x04
RDP_RST = 0x01
RDP_HEADER_SIZE = 5

# Default zmqproxy ports (9600/9601 to avoid macOS AirPlay conflict on 7000)
ZMQ_PROXY_SUB_PORT = 9600
ZMQ_PROXY_PUB_PORT = 9601

# RDP default options (sent in SYN-ACK)
RDP_WINDOW_SIZE = 5
RDP_CONN_TIMEOUT_MS = 30000
RDP_PACKET_TIMEOUT_MS = 5000
RDP_DELAYED_ACKS = 1
RDP_ACK_TIMEOUT = 2000
RDP_ACK_DELAY_COUNT = 4


def _parse_zmq_host(zmq_endpoint: str) -> str:
    """Extract hostname from a ZMQ endpoint string."""
    if "://" in zmq_endpoint:
        parsed = urlparse(zmq_endpoint)
        return parsed.hostname or "localhost"
    return zmq_endpoint


class DTPServer:
    """DTP server with CSP RDP support for serving binaries.

    Implements the full DTP protocol:
    - RDP handshake (SYN/SYN-ACK/ACK) on port 7
    - Metadata request/response within the RDP connection
    - Connectionless data transfer on port 8
    """

    def __init__(
        self,
        local_path: str,
        payload_id: int,
        zmq_endpoint: str,
        node_address: int,
        mtu: int = 256,
        zmq_pub_port: int = ZMQ_PROXY_SUB_PORT,
        zmq_sub_port: int = ZMQ_PROXY_PUB_PORT,
        on_progress: Optional[callable] = None,
    ):
        self.local_path = local_path
        self.payload_id = payload_id
        self.zmq_endpoint = zmq_endpoint
        self.zmq_host = _parse_zmq_host(zmq_endpoint)
        self.node_address = node_address
        self.mtu = mtu
        self.zmq_pub_port = zmq_pub_port
        self.zmq_sub_port = zmq_sub_port
        self.on_progress = on_progress

        self._context: Optional[zmq.Context] = None
        self._pub: Optional[zmq.Socket] = None
        self._sub: Optional[zmq.Socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._file_data: Optional[bytes] = None
        self._file_size: int = 0

    def _load_file(self) -> None:
        """Load the file into memory."""
        with open(self.local_path, "rb") as f:
            self._file_data = f.read()
        self._file_size = len(self._file_data)

    def _build_csp_header(
        self, dest: int, dest_port: int, src_port: int = DTP_PORT,
        priority: int = 2, flags: int = 0,
    ) -> bytes:
        """Build a CSP v2 header (6 bytes).

        Args:
            dest: Destination node address (14 bits).
            dest_port: Destination port (6 bits).
            src_port: Source port (6 bits).
            priority: Packet priority (0-3).
            flags: CSP flags (e.g., CSP_FRDP for RDP packets).
        """
        id2 = (
            (priority & 0x3) << 46 |
            (dest & 0x3FFF) << 32 |
            (self.node_address & 0x3FFF) << 18 |
            (dest_port & 0x3F) << 12 |
            (src_port & 0x3F) << 6 |
            (flags & 0x3F)
        )
        raw = struct.pack(">Q", id2 << 16)
        return raw[:6]

    def _parse_csp_header(self, data: bytes) -> dict:
        """Parse a CSP v2 header from 6 bytes."""
        raw = data[:6] + b'\x00\x00'
        id2 = struct.unpack(">Q", raw)[0] >> 16
        return {
            "pri": (id2 >> 46) & 0x3,
            "dst": (id2 >> 32) & 0x3FFF,
            "src": (id2 >> 18) & 0x3FFF,
            "dport": (id2 >> 12) & 0x3F,
            "sport": (id2 >> 6) & 0x3F,
            "flags": id2 & 0x3F,
        }

    def _build_rdp_header(self, flags: int, seq_nr: int, ack_nr: int) -> bytes:
        """Build a 5-byte RDP header (appended to packet data, not prepended)."""
        return struct.pack(">BHH", flags, seq_nr, ack_nr)

    def _parse_rdp_from_end(self, payload: bytes) -> tuple[dict, bytes]:
        """Parse RDP header from the END of the payload.

        libcsp appends the RDP header after the application data.
        Returns (rdp_dict, application_data).
        """
        if len(payload) < RDP_HEADER_SIZE:
            return {"flags": 0, "seq_nr": 0, "ack_nr": 0}, b""
        rdp_bytes = payload[-RDP_HEADER_SIZE:]
        app_data = payload[:-RDP_HEADER_SIZE]
        flags, seq_nr, ack_nr = struct.unpack(">BHH", rdp_bytes)
        # Lower 4 bits are RDP flags, upper 4 bits are ephemeral counter
        return {"flags": flags & 0x0F, "seq_nr": seq_nr, "ack_nr": ack_nr}, app_data

    def _build_rdp_syn_ack(
        self, dest: int, dest_port: int, src_port: int,
        my_iss: int, client_iss: int,
    ) -> bytes:
        """Build a complete SYN-ACK packet.

        libcsp wire format: CSP header + data + RDP header (appended).
        For SYN-ACK, data = 6 × uint32_t options, then RDP header at end.
        """
        csp_hdr = self._build_csp_header(
            dest, dest_port, src_port, priority=1, flags=CSP_FRDP,
        )
        # SYN-ACK options: 6 × uint32_t (same format as SYN)
        options = struct.pack(
            ">IIIIII",
            RDP_WINDOW_SIZE,
            RDP_CONN_TIMEOUT_MS,
            RDP_PACKET_TIMEOUT_MS,
            RDP_DELAYED_ACKS,
            RDP_ACK_TIMEOUT,
            RDP_ACK_DELAY_COUNT,
        )
        # RDP header is APPENDED after the data
        rdp_hdr = self._build_rdp_header(RDP_SYN | RDP_ACK, my_iss, client_iss)
        return csp_hdr + options + rdp_hdr

    def _build_rdp_data_response(
        self, dest: int, dest_port: int, src_port: int,
        seq_nr: int, ack_nr: int, payload: bytes,
    ) -> bytes:
        """Build an RDP data packet.

        Wire format: CSP header + payload data + RDP header (appended).
        """
        csp_hdr = self._build_csp_header(
            dest, dest_port, src_port, priority=1, flags=CSP_FRDP,
        )
        rdp_hdr = self._build_rdp_header(RDP_ACK, seq_nr, ack_nr)
        return csp_hdr + payload + rdp_hdr

    def _build_dtp_meta_response(self, transfer_size: int) -> bytes:
        """Build dtp_meta_resp_t (8 bytes).

        Format: uint32_t size_in_bytes + uint32_t total_payload_size
        Sent in native byte order (little-endian on ARM target).
        """
        return struct.pack("<II", transfer_size, self._file_size)

    def _send_data_packets(self, client_node: int, session_id: int, mtu: int) -> None:
        """Send all data packets connectionlessly on port 8.

        Data packet format:
          uint32_t bytes_sent (offset into file)
          uint32_t session_id
          payload data (mtu - 8 bytes)
        """
        if not self._pub or not self._file_data:
            return

        effective_payload = mtu - 8  # 2 × uint32_t header
        offset = 0

        while offset < self._file_size and self._running:
            end = min(offset + effective_payload, self._file_size)
            chunk = self._file_data[offset:end]

            # Build data packet: CSP header (no RDP) + data header + payload
            csp_hdr = self._build_csp_header(
                client_node, DTP_DATA_PORT, 0,
                priority=2, flags=0,  # No RDP flag for connectionless
            )
            data_header = struct.pack("<II", offset, session_id)
            packet = csp_hdr + data_header + chunk

            try:
                self._pub.send(packet, zmq.NOBLOCK)
            except zmq.Again:
                time.sleep(0.001)
                continue

            offset = end
            if self.on_progress:
                self.on_progress(offset, self._file_size)
            # Pace the transmission
            time.sleep(0.001)

    def _server_loop(self) -> None:
        """Main server loop implementing CSP RDP + DTP protocol."""
        self._load_file()

        # Generate our Initial Send Sequence number
        my_iss = random.randint(0, 0xFFFF)

        # RDP connection state
        STATE_CLOSED = 0
        STATE_SYN_RCVD = 1
        STATE_OPEN = 2

        RDP_SYN_RCVD_TIMEOUT = 10.0  # seconds

        state = STATE_CLOSED
        syn_rcvd_time = 0.0
        client_node = 0
        client_sport = 0
        client_iss = 0
        client_seq = 0
        session_id = 0
        client_mtu = self.mtu

        while self._running:
            try:
                # Timeout SYN_RCVD if no ACK arrives — prevents stuck state
                # when the client crashes after sending SYN.
                if (state == STATE_SYN_RCVD
                        and time.time() - syn_rcvd_time > RDP_SYN_RCVD_TIMEOUT):
                    state = STATE_CLOSED

                if not self._sub.poll(100):  # 100ms timeout
                    continue

                data = self._sub.recv()
                if len(data) < CSP_HEADER_SIZE:
                    continue

                csp = self._parse_csp_header(data[:CSP_HEADER_SIZE])
                payload = data[CSP_HEADER_SIZE:]

                # Only handle packets to our DTP port
                if csp["dport"] != DTP_PORT:
                    continue

                is_rdp = csp["flags"] & CSP_FRDP

                if not is_rdp or len(payload) < RDP_HEADER_SIZE:
                    continue

                rdp, rdp_payload = self._parse_rdp_from_end(payload)

                # STATE: CLOSED — waiting for SYN
                if state == STATE_CLOSED and (rdp["flags"] & RDP_SYN):
                    client_node = csp["src"]
                    client_sport = csp["sport"]
                    client_iss = rdp["seq_nr"]
                    client_seq = client_iss

                    # Parse SYN options (6 × uint32_t) if present
                    # We don't need them but acknowledge them

                    # Send SYN-ACK
                    syn_ack = self._build_rdp_syn_ack(
                        client_node, client_sport, DTP_PORT,
                        my_iss, client_iss,
                    )
                    self._pub.send(syn_ack)
                    state = STATE_SYN_RCVD
                    syn_rcvd_time = time.time()

                # STATE: SYN_RCVD — waiting for ACK
                elif state == STATE_SYN_RCVD and (rdp["flags"] & RDP_ACK):
                    if csp["src"] == client_node:
                        state = STATE_OPEN
                        # The ACK might carry data (metadata request)
                        # or the metadata request comes in the next packet

                        if len(rdp_payload) > 0:
                            # Metadata request is in this packet
                            self._handle_dtp_request(
                                rdp_payload, client_node, client_sport,
                                rdp, my_iss, client_mtu,
                            )
                            state = STATE_CLOSED
                            my_iss = random.randint(0, 0xFFFF)

                # STATE: OPEN — waiting for metadata request
                elif state == STATE_OPEN:
                    if csp["src"] == client_node and len(rdp_payload) > 0:
                        self._handle_dtp_request(
                            rdp_payload, client_node, client_sport,
                            rdp, my_iss, client_mtu,
                        )
                        state = STATE_CLOSED
                        my_iss = random.randint(0, 0xFFFF)

            except zmq.ZMQError:
                if self._running:
                    continue
                break

    def _handle_dtp_request(
        self, req_data: bytes, client_node: int, client_sport: int,
        rdp: dict, my_iss: int, default_mtu: int,
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
          interval_t intervals[8]  (8 × {uint32_t start, uint32_t end})
        """
        if len(req_data) < 16:  # Minimum: header fields before intervals
            return

        # Parse metadata request (dtp_meta_req_t)
        # The struct is sent as a raw memory copy from the client (ARM little-endian).
        # Only keep_alive_interval is explicitly converted to big-endian by the sender.
        throughput, nof_intervals, payload_id, _, _ = struct.unpack_from(
            "<IBBBB", req_data, 0,
        )
        session_id = struct.unpack_from("<I", req_data, 8)[0]
        mtu = struct.unpack_from("<H", req_data, 12)[0]
        keep_alive = struct.unpack_from(">H", req_data, 14)[0]  # BE (htobe32'd by sender)

        # Use client's MTU or default
        if mtu > 0:
            effective_mtu = mtu
        else:
            effective_mtu = default_mtu

        # Compute transfer size (simplified — send entire file)
        transfer_size = self._file_size

        # Build metadata response
        meta_resp = self._build_dtp_meta_response(transfer_size)

        # Send response within RDP connection
        response = self._build_rdp_data_response(
            client_node, client_sport, DTP_PORT,
            seq_nr=my_iss + 1,
            ack_nr=rdp["seq_nr"],
            payload=meta_resp,
        )
        self._pub.send(response)

        # Small delay to allow the client to process metadata
        # and set up its connectionless receive socket on port 8
        time.sleep(0.1)

        # Send data packets connectionlessly on port 8
        self._send_data_packets(client_node, session_id, effective_mtu)

    def start(self) -> None:
        """Start the DTP server with PUB/SUB sockets through zmqproxy."""
        if self._running:
            return

        pub_endpoint = f"tcp://{self.zmq_host}:{self.zmq_pub_port}"
        sub_endpoint = f"tcp://{self.zmq_host}:{self.zmq_sub_port}"

        self._context = zmq.Context()

        self._pub = self._context.socket(zmq.PUB)
        self._pub.setsockopt(zmq.LINGER, 0)
        self._pub.connect(pub_endpoint)

        self._sub = self._context.socket(zmq.SUB)
        self._sub.setsockopt(zmq.LINGER, 0)
        # Subscribe to packets addressed to our node, all priorities
        for priority in range(4):
            filt = struct.pack(">H", (priority << 14) | self.node_address)
            self._sub.setsockopt(zmq.SUBSCRIBE, filt)
        self._sub.connect(sub_endpoint)

        self._running = True
        self._server_thread = threading.Thread(target=self._server_loop, daemon=True)
        self._server_thread.start()

        # ZMQ slow-joiner: wait for subscriptions to propagate
        time.sleep(0.2)

    def stop(self) -> None:
        """Stop the DTP server."""
        self._running = False

        if self._server_thread:
            self._server_thread.join(timeout=2.0)
            self._server_thread = None

        if self._pub:
            self._pub.close(linger=0)
            self._pub = None

        if self._sub:
            self._sub.close(linger=0)
            self._sub = None

        if self._context:
            self._context.term()
            self._context = None

        self._file_data = None
