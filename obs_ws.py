"""Minimal, dependency-free OBS WebSocket v5 client.

obs-websocket 5.x ships inside OBS Studio 28+ (Tools -> WebSocket Server
Settings).  Talking to it means we never have to focus the OBS window, never
simulate keystrokes and never care which monitor OBS is on -- which is exactly
the class of bug the hotkey approach has on a dual-monitor desk.

Protocol reference: https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import uuid

DEFAULT_PORT = 4455
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    """Transport or protocol level failure."""


class WSClosed(WSError):
    """The peer sent a WebSocket Close frame."""

    def __init__(self, code: int, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__("OBS closed the WebSocket connection (%s%s)" % (code, ": " + reason if reason else ""))


class OBSAuthError(WSError):
    """Password rejected (or required and missing)."""


class OBSRequestError(WSError):
    """OBS understood the request and refused it."""

    def __init__(self, request_type: str, status: dict):
        self.request_type = request_type
        self.status = status or {}
        super().__init__(
            "%s failed: %s (code %s)"
            % (
                request_type,
                self.status.get("comment") or "no comment",
                self.status.get("code"),
            )
        )


def _sha256_b64(data: str) -> str:
    return base64.b64encode(hashlib.sha256(data.encode("utf-8")).digest()).decode("ascii")


def build_auth(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 authentication: base64(sha256(base64(sha256(pw+salt)) + challenge))."""
    secret = _sha256_b64(password + salt)
    return _sha256_b64(secret + challenge)


class _WebSocket:
    """Just enough RFC 6455: masked client text frames, ping/pong, close."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    # -- lifecycle ---------------------------------------------------------
    def connect(self, path: str = "/") -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, self.host, self.port, key)
        )
        sock.sendall(request.encode("ascii"))
        header = self._read_http_header()
        status_line = header.split("\r\n", 1)[0]
        if "101" not in status_line:
            raise WSError("WebSocket upgrade refused: %s" % status_line.strip())
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        for line in header.split("\r\n")[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                if name.strip().lower() == "sec-websocket-accept":
                    if value.strip() != expected:
                        raise WSError("Bad Sec-WebSocket-Accept from server")
                    break
        else:
            raise WSError("Server omitted Sec-WebSocket-Accept")

    def close(self) -> None:
        if not self._sock:
            return
        try:
            self._send_frame(0x8, b"\x03\xe8")
        except Exception:
            pass
        try:
            self._sock.close()
        finally:
            self._sock = None
            self._buf.clear()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise WSError("Not connected")
        return self._sock

    # -- low level ---------------------------------------------------------
    def _read_http_header(self) -> str:
        sock = self._require_socket()
        while b"\r\n\r\n" not in self._buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSError("Connection closed during WebSocket handshake")
            self._buf.extend(chunk)
        head, _, rest = bytes(self._buf).partition(b"\r\n\r\n")
        self._buf = bytearray(rest)
        return head.decode("latin-1")

    def _recv_exact(self, count: int) -> bytes:
        sock = self._require_socket()
        while len(self._buf) < count:
            chunk = sock.recv(max(4096, count - len(self._buf)))
            if not chunk:
                raise WSError("Connection closed by OBS")
            self._buf.extend(chunk)
        data = bytes(self._buf[:count])
        del self._buf[:count]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._require_socket()
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        """Return the next complete text message, transparently handling ping/pong."""
        fragments: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                raise WSClosed(code, reason)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x0, 0x1, 0x2):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")
                continue
            # Unknown opcode: ignore.


class ObsClient:
    """Speaks just the OBS WebSocket v5 ops we need."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        password: str = "",
        timeout: float = 5.0,
    ):
        self.host = host or "127.0.0.1"
        self.port = int(port or DEFAULT_PORT)
        self.password = password or ""
        self.timeout = timeout
        self._ws: _WebSocket | None = None
        self._pending: dict[str, dict] = {}
        self.server_version = ""
        self.identified = False

    # -- connection --------------------------------------------------------
    def connect(self) -> dict:
        """Handshake + identify. Returns the Hello payload."""
        self.close()
        ws = _WebSocket(self.host, self.port, timeout=self.timeout)
        ws.connect()
        self._ws = ws
        hello = self._recv_op(0)
        data = hello.get("d", {}) or {}
        self.server_version = data.get("obsWebSocketVersion", "")
        auth_info = data.get("authentication")
        identify: dict = {"rpcVersion": 1, "eventSubscriptions": 0}
        if auth_info:
            if not self.password:
                self.close()
                raise OBSAuthError(
                    "OBS requires a WebSocket password (Tools > WebSocket Server Settings)"
                )
            identify["authentication"] = build_auth(
                self.password, auth_info.get("salt", ""), auth_info.get("challenge", "")
            )
        ws.send_text(json.dumps({"op": 1, "d": identify}))
        try:
            self._recv_until(lambda m: m.get("op") == 2)
        except WSClosed as exc:
            self.close()
            if exc.code == 4009:
                raise OBSAuthError("OBS rejected the WebSocket password") from None
            raise
        self.identified = True
        return data

    def close(self) -> None:
        self.identified = False
        self._pending.clear()
        if self._ws:
            self._ws.close()
            self._ws = None

    def __enter__(self) -> "ObsClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- protocol ----------------------------------------------------------
    def _recv_op(self, want_op: int) -> dict:
        msg = self._recv_until(lambda m: m.get("op") == want_op)
        return msg

    def _recv_until(self, predicate) -> dict:
        if not self._ws:
            raise WSError("Not connected to OBS")
        while True:
            raw = self._ws.recv_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            op = msg.get("op")
            if predicate(msg):
                return msg
            if op == 7:  # late RequestResponse for an abandoned request
                self._pending[msg.get("d", {}).get("requestId", "")] = msg.get("d", {})
            # Events (op 5) and anything else are simply ignored.

    def request(self, request_type: str, request_data: dict | None = None) -> dict:
        """Send one request, return its responseData dict."""
        if not self._ws or not self.identified:
            raise WSError("Not connected to OBS")
        request_id = uuid.uuid4().hex
        payload = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": request_data or {},
            },
        }
        self._ws.send_text(json.dumps(payload))
        response = self._recv_until(
            lambda m: m.get("op") == 7
            and (m.get("d") or {}).get("requestId") == request_id
        )
        data = response.get("d", {}) or {}
        status = data.get("requestStatus", {}) or {}
        if not status.get("result", False):
            raise OBSRequestError(request_type, status)
        return data.get("responseData", {}) or {}

    # -- convenience -------------------------------------------------------
    def get_version(self) -> dict:
        return self.request("GetVersion")

    def get_record_status(self) -> dict:
        return self.request("GetRecordStatus")

    def is_recording(self) -> bool:
        return bool(self.get_record_status().get("outputActive"))

    def start_record(self) -> dict:
        return self.request("StartRecord")

    def stop_record(self) -> dict:
        return self.request("StopRecord")

    def probe(self) -> str:
        """Human-readable one-liner for the UI."""
        version = self.get_version()
        return "OBS %s (obs-websocket %s) at %s:%d" % (
            version.get("obsVersion", "?"),
            version.get("obsWebSocketVersion", self.server_version or "?"),
            self.host,
            self.port,
        )
