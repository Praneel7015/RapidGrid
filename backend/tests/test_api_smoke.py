"""
API smoke tests for incident report, chat, and honest /health.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app  # noqa: E402
from routers import chat as chat_router  # noqa: E402
from routers import incident as incident_router  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_disk_db(tmp_path, monkeypatch):
    """
    Redirect DB_FILE to a per-test temp file so that save_db() / load_db()
    never touch the real backend/data/incidents_db.json and never leak state
    between test runs.  The real file is never written during the test suite.
    """
    tmp_db = tmp_path / "incidents_db.json"
    tmp_db.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(incident_router, "DB_FILE", tmp_db)


@pytest.fixture
def client():
    # Isolate in-memory stores between tests.
    incident_router.ACTIVE_INCIDENTS.clear()
    chat_router.manager.chat_history.clear()
    chat_router.manager.active_connections.clear()
    yield TestClient(app)
    # Teardown: wipe in-memory stores so nothing leaks to the next test.
    incident_router.ACTIVE_INCIDENTS.clear()
    chat_router.manager.chat_history.clear()
    chat_router.manager.active_connections.clear()


def test_health_reports_status_and_routing(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in {"healthy", "degraded", "unhealthy"}
    assert "routing" in body
    assert "offline_graph_available" in body["routing"]
    assert "live_api_key_configured" in body["routing"]


def test_chat_send_and_history_have_message_ids(client):
    inc_id = "INC-test"
    send = client.post(
        f"/api/chat/{inc_id}/send",
        json={
            "incident_id": inc_id,
            "sender_role": "citizen",
            "sender_name": "Ananya",
            "message": "Need help near Jayanagar",
        },
    )
    assert send.status_code == 200
    msg = send.json()["message"]
    assert msg["id"]
    assert msg["timestamp_iso"]
    assert msg["message"] == "Need help near Jayanagar"

    hist = client.get(f"/api/chat/{inc_id}/messages")
    assert hist.status_code == 200
    messages = hist.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["id"] == msg["id"]


def test_chat_rejects_empty_message(client):
    res = client.post(
        "/api/chat/INC-empty/send",
        json={
            "incident_id": "INC-empty",
            "sender_role": "dispatcher",
            "sender_name": "Dispatch",
            "message": "   ",
        },
    )
    assert res.status_code == 400


def test_incident_report_accepts_voice_modality(client):
    with patch.object(incident_router, "run_pipeline", new=AsyncMock(return_value=None)):
        res = client.post(
            "/api/incidents",
            json={
                "citizen_text": "Chest pain and sweating",
                "citizen_name": "Ananya Sharma",
                "citizen_phone": "+91 98765 43210",
                "vehicle_required": "Ambulance",
                "location": {"lat": 12.9279, "lng": 77.5937},
                "input_modality": "voice",
            },
        )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "processing"
    assert data["incident_id"]
    poll = client.get(data["poll_url"])
    assert poll.status_code == 200
    body = poll.json()
    assert body["input_modality"] == "voice"
    assert body["status"] == "processing"


def test_pipeline_failure_marks_incident_failed(client):  # noqa: ARG001
    # Use the `client` fixture so _reset_disk_db (autouse) has already
    # redirected DB_FILE to a temp path before this test touches ACTIVE_INCIDENTS.
    incident_router.ACTIVE_INCIDENTS.clear()
    incident_router.ACTIVE_INCIDENTS.append(
        {
            "incident_id": "INC-fail",
            "status": "processing",
            "citizen_phone": "+91 98765 43210",
        }
    )
    # save_db() writes to the temp DB_FILE (redirected by _reset_disk_db),
    # so no real patch needed — but we keep one to avoid unnecessary I/O.
    with patch.object(incident_router, "save_db"):
        incident_router._mark_failed("INC-fail", RuntimeError("fusion exploded"))
    inc = incident_router.ACTIVE_INCIDENTS[0]
    assert inc["status"] == "failed"
    assert "fusion exploded" in inc["error"]
    # Teardown is handled by the client fixture's yield + ACTIVE_INCIDENTS.clear().


def _reject_during():
    # Stand-in for an agent call that is still awaiting when the dispatcher
    # rejects the incident.
    async def side_effect(*_args, **_kwargs):
        with patch.object(incident_router, "save_db"):
            await incident_router.reject_incident("INC-race")
        result = MagicMock()
        result.recommended_hospital.name = "Test Hospital"
        return result

    return AsyncMock(side_effect=side_effect)


@pytest.mark.parametrize("step", ["accessibility", "fusion"])
def test_pipeline_keeps_rejection_made_while_running(client, step):  # noqa: ARG001
    import asyncio

    incident_router.ACTIVE_INCIDENTS.append(
        {"incident_id": "INC-race", "status": "processing", "citizen_phone": "+91 98765 43210"}
    )
    payload = incident_router.IncidentReportPayload(
        citizen_text="Chest pain", location={"lat": 12.9279, "lng": 77.5937}
    )
    accessibility = (
        _reject_during() if step == "accessibility" else AsyncMock(return_value=MagicMock())
    )
    fusion = _reject_during() if step == "fusion" else AsyncMock()
    with (
        patch.object(incident_router.accessibility_agent, "process_input", new=accessibility),
        patch.object(incident_router.fusion_engine, "fuse", new=fusion),
        patch.object(incident_router, "FusionRequest", MagicMock()),
        patch.object(incident_router.route_agent, "compute_route", side_effect=RuntimeError),
        patch.object(incident_router, "save_db"),
    ):
        asyncio.run(incident_router.run_pipeline("INC-race", payload))

    inc = incident_router.ACTIVE_INCIDENTS[0]
    assert inc["status"] == "failed"
    assert inc["error"] == "Rejected by dispatcher"
    assert "action_plan" not in inc
    assert "citizen_view" not in inc
    if step == "accessibility":
        fusion.assert_not_awaited()


def test_hospital_divert_round_trip(client):
    # GET /{id} used to raise NameError and PATCH /{id}/resources was missing.
    res = client.get("/api/hospital/blr-019")
    assert res.status_code == 200
    assert res.json()["icu_available"] is True

    res = client.patch("/api/hospital/blr-019/resources", json={"icu_available": False})
    assert res.status_code == 200
    assert res.json()["on_divert"] is True
    try:
        assert client.get("/api/hospital/blr-019").json()["icu_available"] is False
    finally:
        client.patch("/api/hospital/blr-019/resources", json={"icu_available": True})


def test_unknown_hospital_is_404(client):
    assert client.get("/api/hospital/no-such-hospital-xyz").status_code == 404


def test_safe_frontend_file_stays_inside_dist(tmp_path):
    import os

    from main import _safe_frontend_file

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    assert _safe_frontend_file("assets/app.js", dist) == os.path.realpath(
        dist / "assets" / "app.js"
    )
    assert _safe_frontend_file("../secret.txt", dist) is None
    assert _safe_frontend_file("assets/../../secret.txt", dist) is None
    assert _safe_frontend_file(str(tmp_path / "secret.txt"), dist) is None
    assert _safe_frontend_file("", dist) is None
    assert _safe_frontend_file("missing.js", dist) is None


def test_unmatched_api_path_is_404_not_the_spa(tmp_path):
    from fastapi import FastAPI

    from main import _mount_frontend

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    spa = FastAPI()
    _mount_frontend(spa, dist)
    c = TestClient(spa)

    assert c.get("/api/does-not-exist").status_code == 404
    assert c.get("/api").status_code == 404
    page = c.get("/dispatcher")
    assert page.status_code == 200
    assert "app" in page.text


def test_hospital_phone_needs_an_exact_name():
    phone = incident_router._hospital_phone
    assert phone("NIMHANS") == "+91 80 2699 5000"
    assert phone("NIMHANS (National Institute of Mental Health and Neurosciences)") == (
        "+91 80 2699 5000"
    )
    assert phone("st johns medical college hospital") == "+91 80 2206 5000"
    # A partial name could be a different branch, and an unknown one has no number.
    assert phone("Manipal Hospital") is None
    assert phone("Some Clinic") is None
    assert phone(None) is None


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_terminal_incident_cannot_be_approved(client, status):
    incident_router.ACTIVE_INCIDENTS.append(
        {"incident_id": "INC-done", "status": status, "error": "Rejected by dispatcher"}
    )
    res = client.post(
        "/api/incidents/INC-done/approve",
        json={"dispatcher_id": "D-2", "approved_hospital": "HOSP-AUTO", "approved_route": "R"},
    )
    assert res.status_code == 409
    inc = incident_router.ACTIVE_INCIDENTS[0]
    assert inc["status"] == status
    assert "assigned_driver" not in inc


def test_diversion_updates_the_er_phone(client):
    incident_router.ACTIVE_INCIDENTS.append(
        {
            "incident_id": "INC-div",
            "status": "dispatched",
            "location": {"lat": 12.97, "lng": 77.59},
            "hospital_phone": "+91 80 2699 5000",
            "citizen_view": {"hospital_phone": "+91 80 2699 5000"},
        }
    )
    with patch.object(incident_router.route_agent, "compute_route", side_effect=RuntimeError):
        res = client.post(
            "/api/driver/change_hospital",
            json={"incident_id": "INC-div", "hospital_name": "Victoria Hospital (BMCRI)"},
        )
    assert res.status_code == 200
    view = res.json()["citizen_view"]
    assert view["hospital_phone"] == "+91 80 2670 1150"
    # The destination must be the same ER as the phone, not a default point.
    assert view["hospital_location"] == {"lat": 12.963496, "lng": 77.573695}
    assert incident_router.ACTIVE_INCIDENTS[0]["hospital_phone"] == "+91 80 2670 1150"


@pytest.mark.parametrize("endpoint", ["arrived_pickup", "change_hospital"])
@pytest.mark.parametrize("status", ["processing", "awaiting_dispatcher_approval", "failed"])
def test_pickup_and_diversion_need_a_live_dispatch(client, endpoint, status):
    incident_router.ACTIVE_INCIDENTS.append({"incident_id": "INC-p", "status": status})
    res = client.post(
        f"/api/driver/{endpoint}",
        json={"incident_id": "INC-p", "hospital_name": "Victoria Hospital (BMCRI)"},
    )
    assert res.status_code == 409
    assert incident_router.ACTIVE_INCIDENTS[0]["status"] == status


def test_diversion_to_unknown_hospital_is_404(client):
    incident_router.ACTIVE_INCIDENTS.append({"incident_id": "INC-div", "status": "dispatched"})
    res = client.post(
        "/api/driver/change_hospital",
        json={"incident_id": "INC-div", "hospital_name": "Nowhere General"},
    )
    assert res.status_code == 404


@pytest.mark.parametrize("status", ["processing", "awaiting_dispatcher_approval", "failed"])
def test_only_dispatched_incidents_can_be_claimed(client, status):
    incident_router.ACTIVE_INCIDENTS.append({"incident_id": "INC-c", "status": status})
    res = client.post("/api/driver/claim", json={"incident_id": "INC-c", "driver_id": "drv-2"})
    assert res.status_code == 409
    assert incident_router.ACTIVE_INCIDENTS[0]["status"] == status


@pytest.mark.parametrize("status", ["processing", "failed"])
def test_arrival_needs_a_dispatched_incident(client, status):
    incident_router.ACTIVE_INCIDENTS.append({"incident_id": "INC-a", "status": status})
    res = client.post("/api/incidents/INC-a/arrived")
    assert res.status_code == 409
    assert incident_router.ACTIVE_INCIDENTS[0]["status"] == status


def test_arrival_completes_a_dispatched_incident(client):
    incident_router.ACTIVE_INCIDENTS.append({"incident_id": "INC-a", "status": "dispatched"})
    res = client.post("/api/incidents/INC-a/arrived")
    assert res.status_code == 200
    assert incident_router.ACTIVE_INCIDENTS[0]["status"] == "completed"


def test_hospital_ranking_with_no_candidates_is_503(client):
    from agents.hospital_intelligence import hospital_agent

    with patch.object(hospital_agent, "_fetch_live_hospitals", new=AsyncMock(return_value=[])):
        res = client.post(
            "/api/hospital/rank",
            json={"incident_location": {"lat": 12.97, "lng": 77.59}, "emergency_type": "cardiac"},
        )
    assert res.status_code == 503
    assert "No candidate hospitals" in res.json()["detail"]


def test_empty_google_polyline_uses_the_offline_router():
    from agents.route_optimization import route_agent
    from models.schemas import Coordinate, RouteRequest

    request = RouteRequest(
        origin=Coordinate(lat=12.97, lng=77.59), destination=Coordinate(lat=12.93, lng=77.62)
    )
    google = MagicMock()
    google.json.return_value = {
        "routes": [{"duration": "300s", "distanceMeters": 2000, "polyline": {}}]
    }
    client_cm = MagicMock()
    client_cm.__enter__.return_value.post.return_value = google
    with (
        patch.object(route_agent, "api_key", "test-key"),
        patch.object(route_agent, "_cache_get", return_value=None),
        patch("agents.route_optimization.httpx.Client", return_value=client_cm),
        patch.object(route_agent, "_local_route", return_value="offline") as local,
    ):
        assert route_agent.compute_route(request) == "offline"
    assert local.call_args.kwargs["reason"] == "Google Routes returned an empty polyline"


def test_startup_survives_missing_offline_graph():
    # prediction_agent used to be imported only on the offline-graph path,
    # so startup raised UnboundLocalError when the graph was missing.
    import copy

    from agents.city_state import city_state
    from agents.local_router import local_router
    from agents.route_optimization import route_agent
    from agents.traffic_intelligence import traffic_agent
    from bus.event_bus import bus

    # Lifespan loads networks into these singletons and registers handlers on
    # the bus. Snapshot their state and put it back so later tests start from
    # the same place. Dicts of lists (the bus's per-topic subscriber lists) get
    # their lists copied too, since startup appends handlers to them in place.
    def snapshot(value):
        if isinstance(value, dict):
            value = copy.copy(value)
            for key, item in value.items():
                if isinstance(item, list):
                    value[key] = list(item)
            return value
        return copy.copy(value)

    singletons = (bus, city_state, local_router, route_agent, traffic_agent)
    saved = [(o, {k: snapshot(v) for k, v in vars(o).items()}) for o in singletons]
    try:
        local_router.load = lambda *a, **k: False
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200
    finally:
        for obj, state in saved:
            vars(obj).clear()
            vars(obj).update(state)
