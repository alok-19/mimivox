from __future__ import annotations

import io
import sys

import pytest
from fastapi.testclient import TestClient

from mimivox import server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server.app.state, "api_token", "a-test-token")
    return TestClient(server.app, base_url="http://127.0.0.1")


def test_private_routes_require_a_bearer_token(client):
    response = client.get("/api/status")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"

    response = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_bearer_token_allows_private_routes(client):
    response = client.get("/api/status?events=0", headers={"Authorization": "Bearer a-test-token"})
    assert response.status_code == 200


def test_stream_and_control_routes_require_authentication(client):
    assert client.get("/stream").status_code == 401
    assert client.get("/health").status_code == 401
    assert client.post("/control", json={"action": "stop"}).status_code == 401


def test_root_page_is_available_without_token(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "MimiVox" in response.text


def test_rejects_non_loopback_host_even_with_token(client):
    response = client.get(
        "/api/status",
        headers={"Authorization": "Bearer a-test-token", "Host": "attacker.example"},
    )
    assert response.status_code == 421


def test_accepts_loopback_hosts():
    assert server._is_loopback_host("localhost")
    assert server._is_loopback_host("127.0.0.1")
    assert server._is_loopback_host("::1")
    assert not server._is_loopback_host("attacker.example")


def test_server_binds_only_to_loopback_and_prints_fragment_token(monkeypatch, capsys):
    monkeypatch.setattr(server.app.state, "api_token", "a-test-token")
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["mimivox-server", "--host", "localhost"])

    server.main()

    output = capsys.readouterr().out
    assert "http://localhost:8765/#token=a-test-token" in output


def test_native_mode_requires_launch_credentials(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mimivox-server", "--parent-pid", "42"])
    monkeypatch.setattr(server, "watch_parent", lambda _pid: None)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))

    with pytest.raises(SystemExit):
        server.main()


def test_native_mode_uses_tls_without_printing_token(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mimivox-server", "--parent-pid", "42"])
    monkeypatch.setattr(server, "watch_parent", lambda _pid: None)
    monkeypatch.setattr(server.app.state, "api_token", "old-token")
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"token":"a-native-secret","pem":"cGVt"}'))
    )
    observed = {}

    def record_run(app, **kwargs):
        observed["app"] = app
        observed["token"] = app.state.api_token
        with open(kwargs["ssl_certfile"], "rb") as cert:
            observed["cert"] = cert.read()
        with open(kwargs["ssl_keyfile"], "rb") as key:
            observed["key"] = key.read()

    monkeypatch.setattr(server.uvicorn, "run", record_run)

    server.main()

    assert observed == {
        "app": server.app,
        "token": "a-native-secret",
        "cert": b"pem",
        "key": b"pem",
    }
    assert "a-native-secret" not in capsys.readouterr().out


def test_rejects_network_bind(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mimivox-server", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        server.main()


def test_native_mode_rejects_incomplete_launch_credentials(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mimivox-server", "--parent-pid", "42"])
    monkeypatch.setattr(server, "watch_parent", lambda _pid: None)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"token":"a-native-secret"}')))

    with pytest.raises(SystemExit):
        server.main()
