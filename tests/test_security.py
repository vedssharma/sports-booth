import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from booth import server
from booth.security import COOKIE_NAME, Auth

TOKEN = "t0ken-" + "x" * 20


@pytest.fixture
def client():
    with TestClient(server.app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(server.app.state, "auth", Auth(TOKEN))
    return Auth(TOKEN)


def cookie_for(auth):
    return {"cookie": f"{COOKIE_NAME}={auth.session_value()}"}


# ── No token configured (default loopback setup) ──────────────────────────────

def test_open_mode_needs_no_credentials(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"


# ── Token configured ──────────────────────────────────────────────────────────

def test_everything_but_liveness_requires_the_token(client, locked):
    page = client.get("/")
    assert page.status_code == 401 and "access token" in page.text
    health = client.get("/health")
    assert health.status_code == 401 and health.headers["www-authenticate"] == "Bearer"
    assert client.get("/healthz").status_code == 200          # container probes must keep working


def test_token_in_the_url_is_exchanged_for_a_cookie_and_removed_from_the_url(client, locked):
    r = client.get(f"/?token={TOKEN}")
    assert r.status_code == 303 and r.headers["location"] == "/"       # token no longer in the URL
    set_cookie = r.headers["set-cookie"]
    assert COOKIE_NAME in set_cookie and "HttpOnly" in set_cookie and "SameSite=strict" in set_cookie
    assert TOKEN not in set_cookie                                     # cookie is a digest, not the token
    # the browser now sends the cookie
    assert client.get("/", headers=cookie_for(locked)).status_code == 200
    assert client.get("/health", headers=cookie_for(locked)).status_code == 200


def test_wrong_token_is_rejected(client, locked):
    assert client.get("/?token=nope").status_code == 401
    assert client.get("/?token=").status_code == 401
    assert client.get("/health", headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.get("/health", headers={"cookie": f"{COOKIE_NAME}=nope"}).status_code == 401
    # a cookie minted for a different token must not work
    assert client.get("/health", headers=cookie_for(Auth("another-long-secret-value"))).status_code == 401


def test_bearer_token_works_for_scripts_and_the_raw_token_is_not_a_valid_cookie(client, locked):
    assert client.get("/health", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/health", headers={"cookie": f"{COOKIE_NAME}={TOKEN}"}).status_code == 401


def test_query_token_does_not_authenticate_other_endpoints(client, locked):
    assert client.get(f"/health?token={TOKEN}").status_code == 401     # tokens in URLs leak into logs


# ── WebSocket ─────────────────────────────────────────────────────────────────

def test_websocket_without_credentials_is_closed_with_1008(client, locked):
    with client.websocket_connect("/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 1008


def test_websocket_with_cookie_or_bearer_gets_the_snapshot(client, locked):
    with client.websocket_connect("/ws", headers=cookie_for(locked)) as ws:
        assert ws.receive_json()["type"] == "snapshot"
    with client.websocket_connect("/ws", headers={"authorization": f"Bearer {TOKEN}"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"


# ── Origin check (applies even without a token: any website can reach localhost) ─

def test_cross_origin_websocket_is_rejected_even_in_open_mode(client):
    with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 1008


def test_same_origin_websocket_is_accepted(client):
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_allowed_origins_are_honoured_for_reverse_proxies(client, monkeypatch):
    monkeypatch.setattr(server.app.state, "auth", Auth(None, ("https://booth.example.com",)))
    with client.websocket_connect("/ws", headers={"origin": "https://booth.example.com"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"
    with client.websocket_connect("/ws", headers={"origin": "https://other.example.com"}) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# ── Headers ───────────────────────────────────────────────────────────────────

def test_security_headers_on_every_response(client):
    for path in ("/", "/healthz", "/health"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in h["content-security-policy"]
