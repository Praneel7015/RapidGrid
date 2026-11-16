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
    valid_but_expired = old + "." + security._signature(old, "test-showcase-secret")
    for cookie in (valid_but_expired, "tampered", "9" * 5000 + ".bad", "123.☃"):
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
        for agent in (main.route_agent, main.communication_agent, main.coordinator_agent, main.learning_agent):
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
