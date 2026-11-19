#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Pure-Python implementation of Mondo Robotics' UCP-over-UDP (:9007) transport and
`mdZenohOverUcp` framing layer, bridging Beni's UCP link to a local loopback TCP
endpoint (`tcp/127.0.0.1:<port>`) compatible with stock `eclipse-zenoh` /
`zenoh-pico` 1.9.0.

Wire protocol verified against `libmondo_native.so` and `md_videotrans`:
  1. UDP handshake on port 9007:
       Client -> Server: b"ucp:sync" (8 bytes)
       Server -> Client: b"ucp:ack"  (7 bytes)
       Disconnect:       b"ucp:leav" (8 bytes)
  2. UCP packet header (12 bytes, little-endian):
       uint16 flags  (0x0051 base: version=1 | magic=5<<4,
                      0x0100 confirm/reliable, 0x0200 more_fragments,
                      0x0c00 channel [0=msg, 1=video, 2=audio, 3=DDS/Zenoh],
                      0x1000 is_hb [carries 11-byte BAK trailer])
       uint16 una    (next expected reliable sn from peer)
       uint16 sn     (this packet's reliable sequence number)
       uint16 len    (total packet bytes including 12-byte hdr [+ 11-byte BAK])
       uint32 ts     (monotonic microseconds)
  3. `mdZenohOverUcp` frames carried over UCP channel 3 (DDS) + HB:
       0xa4 <uint32_be session_id>                             (OPEN)
       0xa5 <uint32_be session_id>                             (READY)
       0xa6 <uint32_be session_id>                             (RESET)
       0xa7 <uint16_be session_id> <uint16_le zlen> <zbytes>   (DATA)
"""

from __future__ import annotations

import logging
import select
import socket
import struct
import threading
import time
from typing import Callable

log = logging.getLogger("open_ineb.ucp")

UCP_DEFAULT_PORT = 9007
UCP_MTU = 0x05B4  # 1460 bytes
UCP_HDR_LEN = 12
UCP_BAK_LEN = 11
UCP_BASE_VER_MAGIC = 0x0051  # version=1, magic=5

UCP_FLAG_CONFIRM = 0x0100
UCP_FLAG_MORE_FRAG = 0x0200
UCP_FLAG_HB = 0x1000

UCP_CHAN_MSG = 0
UCP_CHAN_VIDEO = 1
UCP_CHAN_AUDIO = 2
UCP_CHAN_DDS = 3

ZENOH_FRAME_OPEN = 0xA4
ZENOH_FRAME_READY = 0xA5
ZENOH_FRAME_RESET = 0xA6
ZENOH_FRAME_DATA = 0xA7


def _u16_diff(a: int, b: int) -> int:
    """Signed 16-bit sequence number difference (matching `utils_u16_diff`)."""
    d = (a - b) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


def _now_us() -> int:
    return int(time.monotonic() * 1_000_000) & 0xFFFFFFFF


def _is_zenoh_keepalive_or_ping(zenoh_frame: bytes) -> bool:
    """Match `FUN_001e8494` (`mdZenohOverUcpProductionPort.send`) HB routing."""
    # zenoh_frame is [u16_le len][zenoh_payload...]
    # In the full 0xa7 frame, byte[5] is zenoh_frame[2] (first byte of zenoh msg).
    if len(zenoh_frame) < 3:
        return False
    hdr = zenoh_frame[2]
    if (hdr >> 5) & 1:
        return False
    msg_id = hdr & 0x1F
    return msg_id in (5, 6)


class UcpEndpoint:
    """Reliable UCP-over-UDP endpoint (supports both client and server roles)."""

    def __init__(
        self,
        host: str = "192.168.7.1",
        port: int = UCP_DEFAULT_PORT,
        *,
        role: str = "client",
        on_dds: Callable[[bytes], None] | None = None,
        on_hb: Callable[[bytes], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.role = role
        self.on_dds = on_dds
        self.on_hb = on_hb

        self._sock: socket.socket | None = None
        self._peer_addr: tuple[str, int] | None = None
        self._running = False
        self._connected = False

        self._lock = threading.Lock()
        self._snd_nxt = 0
        self._rcv_nxt = 0
        # sn -> (wire_bytes_without_una_ts, first_sent_time, last_sent_time, flags, payload)
        self._snd_buf: dict[int, tuple[int, bytes, float, float]] = {}
        self._rcv_buf: dict[int, tuple[int, bytes]] = {}

        self._rx_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self, timeout: float = 5.0) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.role == "client":
            self._peer_addr = (self.host, self.port)
            self._sock.connect(self._peer_addr)
            self._sock.settimeout(0.5)
            deadline = time.monotonic() + timeout
            acked = False
            while time.monotonic() < deadline:
                self._sock.sendall(b"ucp:sync")
                try:
                    data = self._sock.recv(64)
                    if data.startswith(b"ucp:ack"):
                        acked = True
                        break
                except socket.timeout:
                    continue
            if not acked:
                self._sock.close()
                self._sock = None
                raise TimeoutError(
                    f"UCP handshake timed out waiting for ucp:ack from {self.host}:{self.port}"
                )
            self._connected = True
        else:
            self._sock.bind((self.host, self.port))
            self.port = self._sock.getsockname()[1]
            self._sock.settimeout(0.2)

        self._running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, name="ucp-rx", daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_loop, name="ucp-tx", daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()

    def close(self) -> None:
        self._running = False
        if self._sock is not None:
            if self._connected:
                try:
                    self._raw_send(b"ucp:leav")
                except OSError:
                    pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected = False

    def _raw_send(self, data: bytes) -> None:
        if self._sock is None:
            return
        if self.role == "client":
            self._sock.sendall(data)
        elif self._peer_addr is not None:
            self._sock.sendto(data, self._peer_addr)

    def _make_bak_trailer(self) -> bytes:
        # Caller holds self._lock
        bm0 = 0
        bm1 = 0
        for sn in self._rcv_buf:
            off = _u16_diff(sn, self._rcv_nxt)
            if 1 <= off <= 32:
                bm0 |= 1 << (off - 1)
            elif 33 <= off <= 64:
                bm1 |= 1 << (off - 33)
        return struct.pack("<BHII", 5, self._rcv_nxt, bm0, bm1)

    def _build_pkt(self, flags: int, sn: int, payload: bytes, *, append_bak: bool = False) -> bytes:
        # Caller holds self._lock
        bak = self._make_bak_trailer() if append_bak else b""
        pkt_len = UCP_HDR_LEN + len(payload) + len(bak)
        hdr = struct.pack("<HHHHI", flags, self._rcv_nxt, sn, pkt_len, _now_us())
        return hdr + payload + bak

    def send_dds(self, data: bytes) -> None:
        """Send payload reliably on UCP channel 3 (DDS/Zenoh), fragmenting at MTU."""
        if not data:
            return
        chunks = [data[i : i + UCP_MTU] for i in range(0, len(data), UCP_MTU)]
        base_flags = UCP_BASE_VER_MAGIC | (UCP_CHAN_DDS << 10) | UCP_FLAG_CONFIRM
        now = time.monotonic()
        with self._lock:
            for idx, chunk in enumerate(chunks):
                flags = base_flags | (UCP_FLAG_MORE_FRAG if idx < len(chunks) - 1 else 0)
                sn = self._snd_nxt
                self._snd_nxt = (self._snd_nxt + 1) & 0xFFFF
                self._snd_buf[sn] = (flags, chunk, now, now)
                pkt = self._build_pkt(flags, sn, chunk)
                try:
                    self._raw_send(pkt)
                except OSError as exc:
                    log.debug("ucp send_dds error: %s", exc)

    def send_hb(self, payload: bytes = b"\x00") -> None:
        """Send a UCP heartbeat packet carrying optional payload + 11-byte BAK trailer."""
        flags = UCP_BASE_VER_MAGIC | UCP_FLAG_HB
        with self._lock:
            pkt = self._build_pkt(flags, 0, payload, append_bak=True)
            try:
                self._raw_send(pkt)
            except OSError as exc:
                log.debug("ucp send_hb error: %s", exc)

    def _tx_loop(self) -> None:
        last_hb = 0.0
        while self._running:
            time.sleep(0.05)
            if not self._connected:
                continue
            now = time.monotonic()
            # Retransmit unacked reliable packets after 200ms RTO
            to_resend: list[bytes] = []
            with self._lock:
                for sn, (flags, payload, first_ts, last_ts) in list(self._snd_buf.items()):
                    if now - last_ts >= 0.2:
                        self._snd_buf[sn] = (flags, payload, first_ts, now)
                        to_resend.append(self._build_pkt(flags, sn, payload))
            for pkt in to_resend:
                try:
                    self._raw_send(pkt)
                except OSError:
                    pass
            # Periodic heartbeat + BAK every 250ms (keeps peer's 3.5s UCP timeout alive)
            if now - last_hb >= 0.25:
                self.send_hb(b"\x00")
                last_hb = now

    def _rx_loop(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                if self.role == "client":
                    data = self._sock.recv(2048)
                else:
                    data, addr = self._sock.recvfrom(2048)
                    if data == b"ucp:sync":
                        self._peer_addr = addr
                        self._connected = True
                        self._sock.sendto(b"ucp:ack", addr)
                        continue
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue
            if data == b"ucp:leav":
                self._connected = False
                break
            if data.startswith(b"ucp:"):
                continue

            self._handle_datagram(data)

    def _handle_datagram(self, data: bytes) -> None:
        pos = 0
        end = len(data)
        deliveries: list[tuple[int, bytes, bool]] = []
        need_ack = False

        with self._lock:
            while pos + UCP_HDR_LEN <= end:
                flags, una, sn, pkt_len, _ts = struct.unpack_from("<HHHHI", data, pos)
                if pkt_len < UCP_HDR_LEN or pos + pkt_len > end:
                    break
                if (flags & 0xFF) != UCP_BASE_VER_MAGIC:
                    break

                # Process cumulative ACK (`una`)
                for s in list(self._snd_buf.keys()):
                    if _u16_diff(s, una) < 0:
                        del self._snd_buf[s]

                is_hb = bool(flags & UCP_FLAG_HB)
                is_confirm = bool(flags & UCP_FLAG_CONFIRM)
                chan = (flags >> 10) & 0x03

                if is_hb:
                    if pkt_len >= UCP_HDR_LEN + UCP_BAK_LEN:
                        bak_off = pos + pkt_len - UCP_BAK_LEN
                        bak_magic, bak_una, bm0, bm1 = struct.unpack_from("<BHII", data, bak_off)
                        if bak_magic == 5:
                            for bit in range(32):
                                if bm0 & (1 << bit):
                                    self._snd_buf.pop((bak_una + 1 + bit) & 0xFFFF, None)
                            for bit in range(32):
                                if bm1 & (1 << bit):
                                    self._snd_buf.pop((bak_una + 33 + bit) & 0xFFFF, None)
                        payload = data[pos + UCP_HDR_LEN : bak_off]
                        if payload:
                            deliveries.append((chan, payload, True))
                elif is_confirm:
                    payload = data[pos + UCP_HDR_LEN : pos + pkt_len]
                    diff = _u16_diff(sn, self._rcv_nxt)
                    if diff >= 0 and sn not in self._rcv_buf:
                        self._rcv_buf[sn] = (chan, payload)
                    need_ack = True
                else:
                    payload = data[pos + UCP_HDR_LEN : pos + pkt_len]
                    deliveries.append((chan, payload, False))

                pos += pkt_len

            # Drain in-order reliable packets
            while self._rcv_nxt in self._rcv_buf:
                chan, payload = self._rcv_buf.pop(self._rcv_nxt)
                self._rcv_nxt = (self._rcv_nxt + 1) & 0xFFFF
                deliveries.append((chan, payload, False))

        # Immediately piggyback an ACK/BAK if we received reliable data
        if need_ack:
            self.send_hb(b"\x00")

        for chan, payload, is_hb in deliveries:
            if is_hb:
                if self.on_hb is not None:
                    self.on_hb(payload)
            elif chan == UCP_CHAN_DDS:
                if self.on_dds is not None:
                    self.on_dds(payload)


class ZenohOverUcpFrameParser:
    """Exact state-machine port of `mdZenohOverUcpFrameParserAppend`."""

    def __init__(self, on_frame: Callable[[int, int, bytes], None]) -> None:
        self._on_frame = on_frame
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        while self._buf:
            tag = self._buf[0]
            if tag in (ZENOH_FRAME_OPEN, ZENOH_FRAME_READY, ZENOH_FRAME_RESET):
                if len(self._buf) < 5:
                    return
                sid = struct.unpack_from(">I", self._buf, 1)[0]
                del self._buf[:5]
                self._on_frame(tag, sid, b"")
            elif tag == ZENOH_FRAME_DATA:
                if len(self._buf) < 5:
                    return
                sid16 = struct.unpack_from(">H", self._buf, 1)[0]
                zlen = struct.unpack_from("<H", self._buf, 3)[0]
                total = 5 + zlen
                if len(self._buf) < total:
                    return
                zenoh_frame = bytes(self._buf[3:total])  # includes [u16_le zlen][zbytes]
                del self._buf[:total]
                self._on_frame(tag, sid16, zenoh_frame)
            else:
                # Resync past non-Zenoh bytes (e.g., 1-byte dummy UCP HB payload)
                del self._buf[:1]


class ZenohOverUcpBridge:
    """
    Bridges a local loopback TCP socket (`tcp/127.0.0.1:<port>`) to a remote
    UCP + `mdZenohOverUcp` endpoint.

    In `role="client"` (connecting to Beni at `192.168.7.1:9007`):
      - Starts UCP UDP client handshake (`ucp:sync` -> `ucp:ack`).
      - Sends `0xa4 <session_id>` (OPEN).
      - Listens on `127.0.0.1:<ephemeral_port>` for a local `eclipse-zenoh` client
        to connect, forwarding `[u16_le len][zenoh_bytes]` <-> `0xa7` frames.
      - Call `send_ready()` after declaring publishers/subscribers to emit
        `0xa5 <session_id>` (READY).

    In `role="server"` (for local loopback testing):
      - Starts UCP UDP server, waits for `0xa4 <session_id>`, connects to a local
        Zenoh TCP listener (`target_tcp_port`), and bridges frames bidirectionally.
    """

    def __init__(
        self,
        host: str = "192.168.7.1",
        port: int = UCP_DEFAULT_PORT,
        *,
        role: str = "client",
        session_id: int = 1,
        target_tcp_port: int | None = None,
    ) -> None:
        self.role = role
        self.session_id = session_id
        self.target_tcp_port = target_tcp_port

        self._dds_parser = ZenohOverUcpFrameParser(self._on_ucp_frame)
        self._hb_parser = ZenohOverUcpFrameParser(self._on_ucp_frame)
        self.ucp = UcpEndpoint(
            host=host,
            port=port,
            role=role,
            on_dds=self._dds_parser.feed,
            on_hb=self._hb_parser.feed,
        )

        self._tcp_srv: socket.socket | None = None
        self._tcp_conn: socket.socket | None = None
        self._tcp_lock = threading.Lock()
        self._pending_to_tcp: list[bytes] = []
        self._running = False
        self.local_tcp_port: int = 0
        self.peer_ready = threading.Event()
        self.peer_opened = threading.Event()
        self._tcp_thread: threading.Thread | None = None

    @property
    def local_endpoint(self) -> str:
        return f"tcp/127.0.0.1:{self.local_tcp_port}"

    def start(self, timeout: float = 5.0) -> None:
        self.ucp.start(timeout=timeout)
        self._running = True

        if self.role == "client":
            self._tcp_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._tcp_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._tcp_srv.bind(("127.0.0.1", 0))
            self.local_tcp_port = self._tcp_srv.getsockname()[1]
            self._tcp_srv.listen(1)
            self._tcp_srv.settimeout(0.2)
            self._tcp_thread = threading.Thread(
                target=self._client_tcp_loop, name="ucp-tcp-client", daemon=True
            )
            self._tcp_thread.start()
            self.send_open()

    def send_open(self) -> None:
        frame = struct.pack(">BI", ZENOH_FRAME_OPEN, self.session_id)
        self.ucp.send_dds(frame)

    def send_ready(self) -> None:
        frame = struct.pack(">BI", ZENOH_FRAME_READY, self.session_id)
        self.ucp.send_dds(frame)

    def send_reset(self) -> None:
        frame = struct.pack(">BI", ZENOH_FRAME_RESET, self.session_id)
        self.ucp.send_dds(frame)

    def _on_ucp_frame(self, tag: int, sid: int, zenoh_frame: bytes) -> None:
        if tag == ZENOH_FRAME_OPEN:
            if self.role == "server":
                self.session_id = sid
                self.peer_opened.set()
                if self.target_tcp_port is not None and self._tcp_conn is None:
                    self._start_server_tcp_connect()
        elif tag == ZENOH_FRAME_READY:
            if sid == self.session_id:
                self.peer_ready.set()
        elif tag == ZENOH_FRAME_RESET:
            if sid == self.session_id:
                self._close_tcp_conn()
        elif tag == ZENOH_FRAME_DATA:
            if (sid & 0xFFFF) == (self.session_id & 0xFFFF):
                with self._tcp_lock:
                    if self._tcp_conn is not None:
                        try:
                            self._tcp_conn.sendall(zenoh_frame)
                        except OSError:
                            pass
                    else:
                        self._pending_to_tcp.append(zenoh_frame)

    def _start_server_tcp_connect(self) -> None:
        assert self.target_tcp_port is not None
        conn = socket.create_connection(("127.0.0.1", self.target_tcp_port), timeout=5.0)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._tcp_lock:
            self._tcp_conn = conn
            for pending in self._pending_to_tcp:
                conn.sendall(pending)
            self._pending_to_tcp.clear()
        self._tcp_thread = threading.Thread(
            target=self._pump_tcp_to_ucp, args=(conn,), name="ucp-tcp-srv", daemon=True
        )
        self._tcp_thread.start()

    def _client_tcp_loop(self) -> None:
        assert self._tcp_srv is not None
        while self._running:
            try:
                conn, _ = self._tcp_srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._tcp_lock:
                self._tcp_conn = conn
                for pending in self._pending_to_tcp:
                    conn.sendall(pending)
                self._pending_to_tcp.clear()
            self._pump_tcp_to_ucp(conn)
            break

    def _pump_tcp_to_ucp(self, conn: socket.socket) -> None:
        buf = bytearray()
        conn.settimeout(0.2)
        while self._running:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            while len(buf) >= 2:
                zlen = struct.unpack_from("<H", buf, 0)[0]
                if len(buf) < 2 + zlen:
                    break
                zframe = bytes(buf[: 2 + zlen])
                del buf[: 2 + zlen]
                wrapped = (
                    struct.pack(">BH", ZENOH_FRAME_DATA, self.session_id & 0xFFFF) + zframe
                )
                if _is_zenoh_keepalive_or_ping(zframe):
                    self.ucp.send_hb(wrapped)
                else:
                    self.ucp.send_dds(wrapped)

    def _close_tcp_conn(self) -> None:
        with self._tcp_lock:
            if self._tcp_conn is not None:
                try:
                    self._tcp_conn.close()
                except OSError:
                    pass
                self._tcp_conn = None

    def close(self) -> None:
        if self._running:
            try:
                self.send_reset()
            except Exception:
                pass
        self._running = False
        self._close_tcp_conn()
        if self._tcp_srv is not None:
            try:
                self._tcp_srv.close()
            except OSError:
                pass
            self._tcp_srv = None
        self.ucp.close()
