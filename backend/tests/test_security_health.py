"""Regressions for hosted showcase access and observed routing readiness."""

from __future__ import annotations

import sys
import time
import asyncio
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import security
from agents.route_optimization import RouteOptimizationAgent
from main import app
from models.schemas import Coordinate, RouteRequest


@pytest.fixture(autouse=True)
def fresh_security_state(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDGRID_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAPIDGRID_CLIENT_IP_HEADER", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(security, "_pending_revocations", {})
    monkeypatch.setattr(security, "_socket_sessions", {})
    security.reset_throttle()
    yield
    security.reset_throttle()


def login(client):
    response = client.post("/api/session", json={"token": "test-showcase-secret"})
    assert response.status_code == 200, response.text
    return response


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "operator")
    monkeypatch.setenv("RAPIDGRID_OPERATOR_TOKEN", "test-showcase-secret")
    monkeypatch.delenv("RAPIDGRID_ALLOWED_ORIGINS", raising=False)
    return TestClient(app, base_url="https://testserver")


def test_showcase_locks_http_and_socket_until_login(client):
    assert client.get("/api/session").json()["authenticated"] is False
    assert client.get("/api/incidents/").status_code == 401
    assert client.post("/api/incidents", json={}).status_code == 401
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ws/chat/no-access"):
            pass
    assert denied.value.code == 1008
    assert client.get("/health/live").json() == {"status": "alive"}


def test_login_session_cookie_and_logout(client):
    denied = client.post("/api/session", json={"token": "wrong"})
    assert denied.status_code == 401
    assert client.post("/api/session", json={"token": "☃"}).status_code == 401
    response = client.post("/api/session", json={"token": "test-showcase-secret"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "test-showcase-secret" not in cookie
    assert client.get("/api/session").json()["operator_authenticated"] is True
    assert client.get("/api/incidents/").status_code == 200
    assert client.delete("/api/session").status_code == 200
    assert client.get("/api/incidents/").status_code == 401


def test_missing_or_unknown_mode_fails_closed(client, monkeypatch):
    monkeypatch.delenv("RAPIDGRID_OPERATOR_TOKEN")
    assert client.get("/api/incidents/").status_code == 503
    assert client.post("/api/session", json={"token": "anything"}).status_code == 503
    monkeypatch.setenv("RAPIDGRID_MODE", "typo")
    assert client.get("/api/session").json()["mode"] == "operator"
    assert client.get("/api/incidents/").status_code == 503


def test_expired_tampered_and_malformed_cookies_are_rejected(client):
    old = str(int(time.time()) - security.SESSION_SECONDS - 10)
    sid = "s" * 24
    valid_but_expired = old + "." + sid + "." + security._signature(old, sid, "test-showcase-secret")
    now = str(int(time.time()))
    wrong_key = now + "." + sid + "." + security._signature(now, sid, "another-secret")
    for cookie in (valid_but_expired, wrong_key, "tampered", "9" * 5000 + ".bad", "123.☃", "1.2.3.4"):
        # Cookie headers are latin-1 on the wire; unicode attacks use the
        # authentication helper directly elsewhere via JSON login above.
        if not cookie.isascii():
            continue
        client.cookies.set(security.COOKIE_NAME, cookie)
        assert client.get("/api/incidents/").status_code == 401


def test_cross_origin_login_actions_and_sockets_refused(client):
    headers = {"Origin": "https://untrusted.example"}
    assert client.post("/api/session", json={"token": "test-showcase-secret"}, headers=headers).status_code == 403
    client.post("/api/session", json={"token": "test-showcase-secret"})
    assert client.post("/api/incidents/clear", headers=headers).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat/no-access", headers=headers):
            pass


def test_demo_allows_synthetic_views_but_not_bulk_wipes(client, monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    assert client.get("/api/session").json()["authenticated"] is True
    assert client.get("/api/incidents/").status_code == 200
    assert client.post("/api/incidents/clear").status_code == 401
    # The city model holds only demo closures, so the demo may reset it.
    assert client.post("/api/city/clear").status_code == 200


def test_bearer_authentication_for_cli(client):
    assert client.get("/api/incidents/", headers={"Authorization": "Bearer test-showcase-secret"}).status_code == 200


def test_render_without_mode_fails_closed(client, monkeypatch):
    monkeypatch.delenv("RAPIDGRID_MODE")
    monkeypatch.setenv("RENDER", "true")
    assert client.get("/api/session").json()["mode"] == "operator"
    assert client.get("/api/incidents/").status_code == 401
    monkeypatch.delenv("RAPIDGRID_OPERATOR_TOKEN")
    assert client.get("/api/incidents/").status_code == 503


def test_public_health_does_not_leak_operator_interventions(client, monkeypatch):
    from main import city_state

    monkeypatch.setattr(type(city_state), "ready", property(lambda self: True))
    monkeypatch.setattr(city_state, "snapshot", lambda: {
        "closed_segments": 1,
        "interventions": [{"detail": "Private operator incident label", "target": [123]}],
    })
    login(client)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["city_model"] == {"closed_segments": 1}
    assert "Private operator incident label" not in response.text


def _request():
    return RouteRequest(origin=Coordinate(lat=13.035, lng=77.597), destination=Coordinate(lat=12.929, lng=77.5878))


def test_nonempty_key_is_unverified_without_provider_call(monkeypatch):
    agent = RouteOptimizationAgent()
    agent.api_key = "configured-but-not-tested"
    with patch("agents.route_optimization.httpx.Client") as network:
        assert agent.stats()["live_api_status"] == "unverified"
        network.assert_not_called()


def test_success_cache_failure_and_recovery_observed_without_health_probes():
    agent = RouteOptimizationAgent()
    agent.api_key = "test-key"
    request = _request()
    payload = {"routes": [{"duration": "100s", "distanceMeters": 1000, "polyline": {"geoJsonLinestring": {"coordinates": [[77.597, 13.035], [77.5878, 12.929]]}}}]}
    success = httpx.Response(200, json=payload, request=httpx.Request("POST", "https://routes.googleapis.com/test"))
    with patch("agents.route_optimization.httpx.Client") as network:
        network.return_value.__enter__.return_value.post.return_value = success
        agent.compute_route(request)
        assert agent.stats()["live_api_status"] == "available"
        assert agent.stats()["active_source"] == "live"
        agent.compute_route(request)
        assert agent.stats()["active_source"] == "live_cache"
        assert agent.api_calls == 1
        agent._cache.clear()
        rejected = httpx.Response(403, request=httpx.Request("POST", "https://routes.googleapis.com/test"))
        network.return_value.__enter__.return_value.post.return_value = rejected
        with patch.object(agent, "_local_route", return_value=None):
            agent.compute_route(request)
        assert agent.stats()["live_api_status"] == "unavailable"
        assert agent.stats()["active_source"] != "live"
        assert agent.stats()["fallback_reason"] == "Google Routes HTTP 403"
        network.return_value.__enter__.return_value.post.return_value = success
        agent.compute_route(request)
        assert agent.stats()["live_api_status"] == "available"
        assert agent.stats()["fallback_reason"] is None


def test_stale_success_is_not_labeled_live():
    agent = RouteOptimizationAgent()
    agent.api_key = "test-key"
    agent._provider_success()
    agent._last_live_success_monotonic -= 301
    assert agent.stats()["live_api_status"] == "stale"
    assert agent.stats()["active_source"] == "unverified"


def test_malformed_google_geometry_uses_offline_fallback():
    agent = RouteOptimizationAgent()
    agent.api_key = "test-key"
    response = httpx.Response(200, json={"routes": [{"duration": "bad"}]}, request=httpx.Request("POST", "https://routes.googleapis.com/test"))
    with patch("agents.route_optimization.httpx.Client") as network, patch.object(agent, "_local_route", return_value=None) as offline:
        network.return_value.__enter__.return_value.post.return_value = response
        agent.compute_route(_request())
        offline.assert_called_once()
    assert agent.stats()["live_api_status"] == "unavailable"


def test_health_does_not_call_google_and_does_not_trust_key(client, monkeypatch):
    from main import route_agent
    monkeypatch.setattr(route_agent, "api_key", "configured")
    monkeypatch.setattr(route_agent, "_last_live_success_monotonic", None)
    monkeypatch.setattr(route_agent, "_last_live_error_monotonic", None)
    login(client)
    with patch("agents.route_optimization.httpx.Client") as network:
        body = client.get("/health").json()
        assert body["routing"]["live_api_key_configured"] is True
        assert body["routing"]["live_api_status"] == "unverified"
        assert body["status"] != "healthy"
        network.assert_not_called()


def test_startup_without_offline_graph_still_registers_prediction(monkeypatch):
    """Missing graph must degrade routing, not crash on a shadowed import."""
    import main

    monkeypatch.setattr(main.local_router, "load", lambda: False)
    monkeypatch.setattr(main.hospital, "restore_capacity", lambda: None)
    monkeypatch.setattr(main.traffic_agent, "load_network", lambda: None)
    monkeypatch.setattr(main.traffic_agent, "publish_snapshot", AsyncMock())
    with patch.object(main.prediction_agent, "register_on_bus") as register:
        for agent in (main.route_agent,):
            monkeypatch.setattr(agent, "register_on_bus", lambda: None)

        async def start():
            async with main.lifespan(main.app):
                pass

        asyncio.run(start())
        register.assert_called_once()


def test_static_path_containment_refuses_escapes_and_serves_inside(tmp_path):
    import main

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("index")
    (dist / "assets" / "app.js").write_text("js")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (dist / "leak.txt").symlink_to(secret)
    (dist / "leakdir").symlink_to(tmp_path)

    inside = main._safe_frontend_file("assets/app.js", dist)
    assert inside is not None and Path(inside) == (dist / "assets" / "app.js").resolve()
    assert main._safe_frontend_file("../secret.txt", dist) is None
    assert main._safe_frontend_file("assets/../../secret.txt", dist) is None
    assert main._safe_frontend_file(str(secret), dist) is None  # absolute path
    assert main._safe_frontend_file("leak.txt", dist) is None  # symlink out of dist
    assert main._safe_frontend_file("leakdir/secret.txt", dist) is None
    assert main._safe_frontend_file("", dist) is None
    assert main._safe_frontend_file("assets", dist) is None  # directory, not a file
    assert main._safe_frontend_file("missing.js", dist) is None
    assert main._safe_frontend_file("bad\x00name", dist) is None


def test_spa_route_serves_inside_files_and_falls_back_for_escapes(tmp_path, monkeypatch):
    import main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX")
    (dist / "robots.txt").write_text("ROBOTS")
    (tmp_path / "secret.txt").write_text("SECRET")
    (dist / "leak.txt").symlink_to(tmp_path / "secret.txt")
    monkeypatch.setattr(main, "_FRONTEND_DIST", dist)
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    client = TestClient(main.app)
    # The SPA catch-all is only registered when a real dist/ existed at import time.
    if not any(getattr(r, "path", "") == "/{full_path:path}" for r in main.app.routes):
        pytest.skip("frontend/dist not built; catch-all route not registered")
    assert client.get("/robots.txt").text == "ROBOTS"
    for url in ("/%2e%2e/secret.txt", "/..%2fsecret.txt", "/leak.txt", "/%2Fetc/passwd", "/foo%00bar"):
        body = client.get(url).text
        assert "SECRET" not in body and "root:" not in body, url
        assert body == "INDEX", url


# --- F16: session and surface hardening ----------------------------------------

def test_logout_revokes_a_copied_session_cookie(client):
    login(client)
    stolen = client.cookies.get(security.COOKIE_NAME)
    assert stolen and stolen.count(".") == 2
    assert client.delete("/api/session").status_code == 200

    replay = TestClient(app, base_url="https://testserver")
    replay.cookies.set(security.COOKIE_NAME, stolen)
    assert replay.get("/api/incidents/").status_code == 401
    assert replay.get("/api/session").json()["operator_authenticated"] is False


def test_revocation_survives_a_restart(client, monkeypatch):
    login(client)
    stolen = client.cookies.get(security.COOKIE_NAME)
    client.delete("/api/session")
    monkeypatch.setattr(security, "_revoked", None)  # as after a restart
    replay = TestClient(app, base_url="https://testserver")
    replay.cookies.set(security.COOKIE_NAME, stolen)
    assert replay.get("/api/incidents/").status_code == 401


def test_other_sessions_survive_one_logout(client):
    login(client)
    other = TestClient(app, base_url="https://testserver")
    login(other)
    client.delete("/api/session")
    assert other.get("/api/incidents/").status_code == 200


def test_old_format_cookie_is_refused(client):
    issued = str(int(time.time()))
    import hashlib, hmac
    signature = hmac.new(b"test-showcase-secret", ("rapidgrid-session:" + issued).encode(), hashlib.sha256).hexdigest()
    client.cookies.set(security.COOKIE_NAME, issued + "." + signature)
    assert client.get("/api/incidents/").status_code == 401


def test_wrong_tokens_are_throttled_and_the_right_one_waits_too(client):
    for _ in range(security.MAX_FAILURES_PER_CLIENT):
        assert client.post("/api/session", json={"token": "guess"}).status_code == 401
    throttled = client.post("/api/session", json={"token": "test-showcase-secret"})
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0


def test_bearer_guesses_share_the_throttle(client):
    for _ in range(security.MAX_FAILURES_PER_CLIENT):
        assert client.get("/api/incidents/", headers={"Authorization": "Bearer guess"}).status_code == 401
    assert client.get("/api/incidents/", headers={"Authorization": "Bearer test-showcase-secret"}).status_code == 401
    assert client.post("/api/session", json={"token": "test-showcase-secret"}).status_code == 429


def test_throttle_is_per_client_with_a_global_ceiling(client, monkeypatch):
    monkeypatch.setenv("RAPIDGRID_CLIENT_IP_HEADER", "true-client-ip")
    for _ in range(security.MAX_FAILURES_PER_CLIENT):
        client.post("/api/session", json={"token": "guess"}, headers={"True-Client-IP": "198.51.100.1"})
    assert client.post("/api/session", json={"token": "guess"}, headers={"True-Client-IP": "198.51.100.1"}).status_code == 429
    # A different client is unaffected...
    assert client.post("/api/session", json={"token": "test-showcase-secret"},
                       headers={"True-Client-IP": "198.51.100.2"}).status_code == 200
    # ...until the total across all clients reaches the ceiling.
    monkeypatch.setattr(security, "MAX_FAILURES_TOTAL", security.MAX_FAILURES_PER_CLIENT + 1)
    client.post("/api/session", json={"token": "guess"}, headers={"True-Client-IP": "198.51.100.3"})
    assert client.post("/api/session", json={"token": "test-showcase-secret"},
                       headers={"True-Client-IP": "198.51.100.4"}).status_code == 429


def test_throttle_expires(client, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: clock[0])
    for _ in range(security.MAX_FAILURES_PER_CLIENT):
        client.post("/api/session", json={"token": "guess"})
    assert client.post("/api/session", json={"token": "test-showcase-secret"}).status_code == 429
    clock[0] += security.FAILURE_WINDOW_SECONDS
    assert client.post("/api/session", json={"token": "test-showcase-secret"}).status_code == 200


def test_api_docs_need_unlock_in_operator_mode(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    login(client)
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_api_docs_stay_open_in_demo_mode(client, monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    assert client.get("/openapi.json").status_code == 200


def test_public_health_is_a_status_line_until_unlock(client):
    body = client.get("/health").json()
    assert set(body) == {"status", "service"}
    login(client)
    assert {"routing", "city_model", "pipeline"} <= set(client.get("/health").json())


def test_security_headers_on_pages_and_api(client):
    for path in ("/health/live", "/api/incidents/", "/api/session"):
        headers = client.get(path).headers
        assert headers["x-frame-options"] == "DENY", path
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "microphone=(self)" in headers["permissions-policy"]
        assert headers["strict-transport-security"].startswith("max-age=")


def test_no_hsts_over_plain_http(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    plain = TestClient(app, base_url="http://testserver")
    assert "strict-transport-security" not in plain.get("/health/live").headers


def test_render_charges_failures_to_the_forwarded_client(client, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    for _ in range(security.MAX_FAILURES_PER_CLIENT):
        client.post("/api/session", json={"token": "guess"}, headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert client.post("/api/session", json={"token": "guess"},
                       headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
    # Someone else behind the same proxy can still unlock.
    assert client.post("/api/session", json={"token": "test-showcase-secret"},
                       headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.1"}).status_code == 200


# --- Review follow-ups ---------------------------------------------------------

def test_logout_closes_that_sessions_chat_socket_only(client):
    from routers import incident as incident_router

    incident_router.ACTIVE_INCIDENTS.append({"incident_id": "INC-socket-revoke", "status": "processing"})
    try:
        login(client)
        other = TestClient(app, base_url="https://testserver")
        login(other)
        def cookie_header(c):
            return {"cookie": f"{security.COOKIE_NAME}={c.cookies.get(security.COOKIE_NAME)}"}

        # The session cookie is Secure; send it explicitly on the test socket.
        with client.websocket_connect("/ws/chat/INC-socket-revoke", headers=cookie_header(client)) as mine, \
                other.websocket_connect("/ws/chat/INC-socket-revoke", headers=cookie_header(other)) as theirs:
            assert client.delete("/api/session").status_code == 200
            with pytest.raises(WebSocketDisconnect) as closed:
                mine.receive_text()
            assert closed.value.code == 1008
            # The other session's socket still works.
            theirs.send_text('{"message": "still here", "sender_role": "dispatcher"}')
            assert "still here" in theirs.receive_text()
    finally:
        incident_router.ACTIVE_INCIDENTS[:] = [
            i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] != "INC-socket-revoke"
        ]


def test_unreadable_revocations_fail_closed_and_retry(client, monkeypatch):
    import storage

    login(client)
    real_load = storage.load_collection

    def broken(name, *args, **kwargs):
        if name == security._REVOKED_COLLECTION:
            raise OSError("disk unavailable")
        return real_load(name, *args, **kwargs)

    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(storage, "load_collection", broken)
    assert client.get("/api/incidents/").status_code == 401  # fail closed
    assert security._revoked is None  # nothing cached
    monkeypatch.setattr(storage, "load_collection", real_load)
    assert client.get("/api/incidents/").status_code == 200  # retried


def test_failed_revocation_save_is_reported_and_still_applies(client, monkeypatch):
    import storage

    login(client)
    cookie = client.cookies.get(security.COOKIE_NAME)
    monkeypatch.setattr(storage, "save_collection", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    response = client.delete("/api/session")
    assert response.status_code == 503
    assert "set-cookie" not in response.headers  # the cookie is kept for a retry
    replay = TestClient(app, base_url="https://testserver")
    replay.cookies.set(security.COOKIE_NAME, cookie)
    assert replay.get("/api/incidents/").status_code == 401  # revoked in this process


def test_revocation_made_while_the_list_is_unreadable_is_kept(client, monkeypatch):
    import storage

    login(client)
    cookie = client.cookies.get(security.COOKIE_NAME)
    real_load = storage.load_collection
    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(storage, "load_collection", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    assert client.delete("/api/session").status_code == 503
    monkeypatch.setattr(storage, "load_collection", real_load)
    monkeypatch.setattr(security, "_pending_revocations", dict(security._pending_revocations))
    replay = TestClient(app, base_url="https://testserver")
    replay.cookies.set(security.COOKIE_NAME, cookie)
    assert replay.get("/api/incidents/").status_code == 401
    # The next successful revocation stores it too.
    login(client)
    assert client.delete("/api/session").status_code == 200
    monkeypatch.setattr(security, "_revoked", None)
    assert replay.get("/api/incidents/").status_code == 401


def test_empty_client_ip_header_setting_keeps_the_render_default(client, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RAPIDGRID_CLIENT_IP_HEADER", "")
    for _ in range(security.MAX_FAILURES_PER_CLIENT):
        client.post("/api/session", json={"token": "guess"}, headers={"X-Forwarded-For": "203.0.113.20"})
    assert client.post("/api/session", json={"token": "test-showcase-secret"},
                       headers={"X-Forwarded-For": "203.0.113.21"}).status_code == 200


# --- F18: DNS rebinding against a local demo -----------------------------------

def test_demo_api_refuses_unknown_hosts(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.delenv("RAPIDGRID_ALLOWED_ORIGINS", raising=False)
    rebinding = {"Host": "attacker.example", "Origin": "http://attacker.example"}
    local = TestClient(app, base_url="http://testserver")
    assert local.get("/api/incidents/", headers=rebinding).status_code == 421
    assert local.post("/api/city/clear", headers=rebinding).status_code == 421
    with pytest.raises(WebSocketDisconnect) as closed:
        with local.websocket_connect("/ws/chat/any", headers=rebinding):
            pass
    assert closed.value.code == 1008
    # Local names work, with or without a port, and the page itself is not affected.
    for host in ("localhost:5173", "127.0.0.1:8000", "[::1]:8000"):
        assert local.get("/api/incidents/", headers={"Host": host}).status_code == 200, host
    assert local.get("/health/live", headers=rebinding).status_code == 200


def test_demo_hosts_can_be_extended(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_ALLOWED_HOSTS", "testserver, demo.lan")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "rapidgrid.onrender.com")
    local = TestClient(app, base_url="http://testserver")
    assert local.get("/api/incidents/", headers={"Host": "demo.lan:8000"}).status_code == 200
    assert local.get("/api/incidents/", headers={"Host": "rapidgrid.onrender.com"}).status_code == 200


def test_operator_mode_does_not_pin_hosts(client):
    # Operator mode already needs a session; a custom domain must keep working.
    login(client)
    assert client.get("/api/incidents/", headers={"Host": "rapidgrid.example.org"}).status_code == 200


def test_malformed_host_is_refused_without_an_error(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    local = TestClient(app, base_url="http://testserver", raise_server_exceptions=True)
    assert local.get("/api/incidents/", headers={"Host": "[bad"}).status_code == 421
    with pytest.raises(WebSocketDisconnect) as closed:
        with local.websocket_connect("/ws/chat/any", headers={"Host": "[bad"}):
            pass
    assert closed.value.code == 1008


def test_plain_http_to_a_socket_path_gets_an_http_refusal(client, monkeypatch):
    # Operator mode, no session: an HTTP GET to /ws/... must get a response.
    assert client.get("/ws/chat/any").status_code == 401
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    local = TestClient(app, base_url="http://testserver")
    assert local.get("/ws/chat/any", headers={"Host": "attacker.example"}).status_code == 421
    cross = {"Origin": "https://untrusted.example"}
    assert client.get("/ws/chat/any", headers=cross).status_code == 403
