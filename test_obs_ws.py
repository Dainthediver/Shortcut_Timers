"""Tests for obs_ws.py against a fake obs-websocket v5 server.

Run:  python -m pytest test_obs_ws.py test_timers.py -v
"""

from __future__ import annotations

import socket

import pytest

from fake_obs_server import BIG_STRING, PASSWORD, FakeOBS, sha_b64
from obs_ws import OBSAuthError, OBSRequestError, ObsClient, WSError, build_auth


@pytest.fixture()
def fake_obs():
    server = FakeOBS().start()
    yield server
    server.stop()


def client(server: FakeOBS, password: str = PASSWORD) -> ObsClient:
    return ObsClient("127.0.0.1", server.port, password, timeout=5)


def test_auth_hash_matches_obs_spec():
    # Independent reference implementation of the documented algorithm.
    secret = sha_b64(PASSWORD + "salt-value")
    expected = sha_b64(secret + "challenge-value")
    assert build_auth(PASSWORD, "salt-value", "challenge-value") == expected


def test_connect_and_probe(fake_obs):
    with client(fake_obs) as obs:
        assert obs.identified
        assert "OBS 30.1.2" in obs.probe()


def test_start_and_stop_recording(fake_obs):
    with client(fake_obs) as obs:
        assert obs.is_recording() is False
        obs.start_record()
        assert obs.is_recording() is True
        assert fake_obs.recording is True
        obs.stop_record()
        assert obs.is_recording() is False
        assert fake_obs.requests == [
            "GetRecordStatus",
            "StartRecord",
            "GetRecordStatus",
            "StopRecord",
            "GetRecordStatus",
        ]


def test_request_error_is_raised(fake_obs):
    with client(fake_obs) as obs:
        obs.start_record()
        with pytest.raises(OBSRequestError) as exc:
            obs.start_record()
        assert "already active" in str(exc.value)


def test_wrong_password(fake_obs):
    obs = client(fake_obs, password="nope")
    with pytest.raises(OBSAuthError):
        obs.connect()
    assert obs.identified is False
    assert fake_obs.auth_failures == 1


def test_missing_password_when_required(fake_obs):
    with pytest.raises(OBSAuthError):
        ObsClient("127.0.0.1", fake_obs.port, "").connect()
    assert fake_obs.requests == []


def test_no_auth_server():
    server = FakeOBS(require_password=False).start()
    try:
        with ObsClient("127.0.0.1", server.port, "") as obs:
            obs.start_record()
            assert server.recording is True
    finally:
        server.stop()


def test_large_response_frame(fake_obs):
    with client(fake_obs) as obs:
        data = obs.request("GetSceneList")
        assert data["scenes"][0]["sceneName"] == BIG_STRING


def test_connection_refused():
    # Bind then close to obtain a port nobody is listening on.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    with pytest.raises((WSError, OSError)):
        ObsClient("127.0.0.1", dead_port, "", timeout=1).connect()


def test_reconnect_after_close(fake_obs):
    obs = client(fake_obs)
    obs.connect()
    obs.close()
    assert obs.identified is False
    obs.connect()  # a fresh handshake on the same object must work
    assert obs.probe()
    obs.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
