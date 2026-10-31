"""A fake obs-websocket v5 server for tests and the headless UI smoke test.

Speaks the real protocol: Hello (with optional salt/challenge), Identify with
the SHA-256 auth response, Identified, then RequestResponse for the handful of
requests the timer uses.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading

from websockets.sync.server import serve

PASSWORD = "s3cret-pass"
SALT = "c2FsdA=="
CHALLENGE = "Y2hhbGxlbmdl"
BIG_STRING = "x" * 200_000  # forces a 64-bit length frame from the server


def sha_b64(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


class FakeOBS:
    """Enough of OBS 30's websocket server for the timer's use case."""

    def __init__(self, require_password: bool = True, password: str = PASSWORD):
        self.require_password = require_password
        self.password = password
        self.recording = False
        self.requests: list[str] = []
        self.auth_failures = 0
        self.record_started_at: float | None = None
        self.record_stopped_at: float | None = None
        self.closed = threading.Event()
        self._server = None
        self.port = 0
        self._thread = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeOBS":
        self._server = serve(self._handler, "127.0.0.1", 0, close_timeout=1)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    @property
    def url(self) -> str:
        return "127.0.0.1:%d" % self.port

    # -- protocol ----------------------------------------------------------
    def _handler(self, conn) -> None:
        hello = {"op": 0, "d": {"obsWebSocketVersion": "5.5.2", "rpcVersion": 1}}
        if self.require_password:
            hello["d"]["authentication"] = {"challenge": CHALLENGE, "salt": SALT}
        conn.send(json.dumps(hello))

        first = json.loads(conn.recv())
        if first.get("op") != 1:
            conn.close(4002, "expected Identify")
            return
        if self.require_password:
            expected = sha_b64(sha_b64(self.password + SALT) + CHALLENGE)
            if first["d"].get("authentication") != expected:
                self.auth_failures += 1
                conn.close(4009, "Authentication failed.")
                self.closed.set()
                return
        conn.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))

        try:
            for raw in conn:
                msg = json.loads(raw)
                if msg.get("op") != 6:
                    continue
                request_id = msg["d"]["requestId"]
                request_type = msg["d"]["requestType"]
                self.requests.append(request_type)
                result, comment, data = self._handle_request(request_type)
                conn.send(
                    json.dumps(
                        {
                            "op": 7,
                            "d": {
                                "requestType": request_type,
                                "requestId": request_id,
                                "requestStatus": {
                                    "result": result,
                                    "code": 100 if result else 500,
                                    "comment": comment,
                                },
                                "responseData": data,
                            },
                        }
                    )
                )
        except Exception:
            pass

    def _handle_request(self, request_type: str):
        import time as _time

        if request_type == "GetVersion":
            return True, None, {
                "obsVersion": "30.1.2",
                "obsWebSocketVersion": "5.5.2",
                "rpcVersion": 1,
            }
        if request_type == "GetRecordStatus":
            return True, None, {
                "outputActive": self.recording,
                "outputPaused": False,
                "outputTimecode": "00:00:00.000",
                "outputDuration": 0,
            }
        if request_type == "StartRecord":
            if self.recording:
                return False, "Recording already active", {}
            self.recording = True
            self.record_started_at = _time.monotonic()
            return True, None, {}
        if request_type == "StopRecord":
            if not self.recording:
                return False, "Recording not active", {}
            self.recording = False
            self.record_stopped_at = _time.monotonic()
            return True, None, {"outputPath": "C:/Videos/session.mkv"}
        if request_type == "GetSceneList":
            return True, None, {
                "scenes": [{"sceneName": BIG_STRING}],
                "currentProgramSceneName": "Main",
            }
        return False, "Unknown request type", {}
