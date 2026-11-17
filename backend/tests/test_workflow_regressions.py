"""Regressions for live review findings; all state and provider calls are isolated."""
from __future__ import annotations

import asyncio
import httpx
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app
from models.schemas import Coordinate
from routers import chat, driver, hospital, incident
from agents.hospital_intelligence import hospital_agent
import storage
from storage import load_collection, save_collection


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(incident, "DB_FILE", tmp_path / "incidents.json")
    previous = set(hospital_agent._on_divert)
    incident.ACTIVE_INCIDENTS.clear()
    chat.manager.chat_history.clear()
    chat.manager.active_connections.clear()
    hospital_agent._on_divert.clear()
    hospital_agent._fetch_cache.clear()  # cached rankings remember divert state
    previous_beds = dict(hospital_agent._reported_beds)
    hospital_agent._reported_beds.clear()
    yield TestClient(app)
    incident.ACTIVE_INCIDENTS.clear()
    chat.manager.chat_history.clear()
    chat.manager.active_connections.clear()
    hospital_agent._on_divert.clear()
    hospital_agent._fetch_cache.clear()
    hospital_agent._reported_beds.clear()
    hospital_agent._reported_beds.update(previous_beds)
    hospital_agent._on_divert.update(previous)


@pytest.fixture
def mocked_route(monkeypatch):
    def route(request):
        return SimpleNamespace(confidence=0.9, coordinates=[request.origin, request.destination], eta=600.0, distance=3500.0)
    monkeypatch.setattr(incident.route_agent, "compute_route", route)


def seed(status="awaiting_dispatcher_approval", **extra):
    hospital_record = {
        "hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)",
        "location": {"lat": 13.0473, "lng": 77.5908},
    }
    report = {
        "incident_id": "INC-review", "status": status,
        "location": {"lat": 13.035, "lng": 77.597}, "citizen_text": "Synthetic review case",
        "citizen_name": "Demo reviewer", "vehicle_required": "Ambulance",
        "action_plan": {"recommended_hospital": hospital_record, "all_hospitals": [hospital_record]},
        "citizen_view": {"origin": {"lat": 13.035, "lng": 77.597}, "hospital_name": hospital_record["name"],
                         "hospital_location": hospital_record["location"], "phase1_eta_minutes": 4},
        **extra,
    }
    incident.ACTIVE_INCIDENTS.append(report)
    return report


def test_hospital_phone_never_selects_another_branch_or_a_placeholder():
    assert incident._hospital_phone("ASTER CMI HOSPITAL (HEBBAL)") == "+91 80 4342 0107"
    assert incident._hospital_phone("St Johns Medical College Hospital") == "+91 80 2206 5000"
    assert incident._hospital_phone("Aster CMI Hospital (Hebbal) Annex") is None
    assert incident._hospital_phone("Aster") is None
    assert incident._hospital_phone("Unknown hospital") is None


def test_pipeline_exception_cannot_overwrite_dispatcher_rejection(client, monkeypatch):
    import asyncio

    report = seed("processing")

    async def reject_during_fusion(_request):
        await incident.reject_incident(report["incident_id"], "reviewer")
        raise RuntimeError("Late provider failure after rejection")

    monkeypatch.setattr(incident.fusion_engine, "fuse", reject_during_fusion)
    payload = incident.IncidentReportPayload(
        citizen_text="Test report", location={"lat": 13.035, "lng": 77.597},
    )
    asyncio.run(incident.run_pipeline(report["incident_id"], payload))
    assert report["status"] == "failed"
    assert report["error"] == "Rejected by dispatcher"
    incident.ACTIVE_INCIDENTS.clear()
    incident.load_db()
    assert incident.ACTIVE_INCIDENTS[0]["error"] == "Rejected by dispatcher"


def test_full_approval_pickup_handover_survives_persistence(client, mocked_route):
    seed()
    assert client.get("/api/driver/active").json()["dispatches"] == []
    approved = client.post("/api/incidents/INC-review/approve", json={
        "dispatcher_id": "reviewer", "approved_hospital": "blr-007", "approved_route": "review-route",
    })
    assert approved.status_code == 200
    assert approved.json()["citizen_view"]["phase1_eta_minutes"] == 4
    payload = {"incident_id": "INC-review", "driver_id": "unit-review"}
    assert client.post("/api/driver/claim", json=payload).status_code == 200
    assert client.post("/api/driver/arrived_pickup", json=payload).status_code == 200
    incident.ACTIVE_INCIDENTS.clear()
    incident.load_db()  # Simulate a process restart during transport.
    queue = client.get("/api/driver/active").json()["dispatches"]
    assert queue[0]["status"] == "patient_picked_up"
    assert queue[0]["driver_stage"] == "hospital"
    assert client.post("/api/incidents/INC-review/arrived").status_code == 200
    assert client.get("/api/driver/active").json()["dispatches"] == []
    repeat = client.post("/api/incidents/INC-review/approve", json={
        "dispatcher_id": "reviewer", "approved_hospital": "blr-007", "approved_route": "review-route",
    })
    assert repeat.status_code == 409
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "completed"


@pytest.mark.parametrize("status", ["processing", "awaiting_dispatcher_approval", "completed", "failed"])
def test_driver_cannot_bypass_approval_or_rewind_terminal_cases(client, status):
    seed(status)
    payload = {"incident_id": "INC-review", "driver_id": "unit-review"}
    assert client.post("/api/driver/claim", json=payload).status_code == 409
    assert client.post("/api/driver/arrived_pickup", json=payload).status_code == 409
    assert incident.ACTIVE_INCIDENTS[0]["status"] == status


def test_claim_does_not_rewind_transport_and_other_driver_cannot_take_over(client):
    seed("patient_picked_up", driver_claimed=True, assigned_driver="unit-review")
    assert client.post("/api/driver/claim", json={"incident_id": "INC-review", "driver_id": "another-unit"}).status_code == 409
    assert client.post("/api/driver/claim", json={"incident_id": "INC-review", "driver_id": "unit-review"}).status_code == 200
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "patient_picked_up"


def test_medical_handover_requires_pickup(client):
    seed("dispatched")
    assert client.post("/api/incidents/INC-review/arrived").status_code == 409
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "dispatched"


def test_failed_route_does_not_invent_pickup_eta_or_change_state(client, monkeypatch):
    seed("dispatched")
    def unavailable(_):
        raise RuntimeError("Provider and offline graph unavailable")
    monkeypatch.setattr(incident.route_agent, "compute_route", unavailable)
    response = client.post("/api/driver/arrived_pickup", json={"incident_id": "INC-review", "driver_id": "review"})
    assert response.status_code == 503
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "dispatched"
    assert "distance_meters" not in incident.ACTIVE_INCIDENTS[0]["citizen_view"]


def test_hospital_detail_is_read_only_and_capacity_survives_reload(client):
    before = client.get("/api/hospital/blr-007")
    assert before.status_code == 200
    assert before.json()["icu_available"] is True
    update = client.patch("/api/hospital/blr-007/resources", json={"icu_available": False, "beds_available": 3})
    assert update.status_code == 200
    assert update.json()["on_divert"] is True
    hospital_agent._on_divert.clear()
    hospital.restore_capacity()
    assert "blr-007" in hospital_agent.diverted
    after = client.get("/api/hospital/blr-007").json()
    assert after["icu_available"] is False
    assert after["beds_available"] == 3
    assert "blr-007" in hospital_agent.diverted  # GET never flips capacity.


def test_invalid_hospital_and_capacity_are_rejected(client):
    assert client.get("/api/hospital/not-a-hospital").status_code == 404
    assert client.patch("/api/hospital/all/resources", json={"icu_available": False}).status_code == 404
    assert client.patch("/api/hospital/blr-007/resources", json={"icu_available": True, "beds_available": -1}).status_code == 422
    assert client.patch("/api/hospital/blr-007/resources", json={"icu_available": True, "beds_available": 10000000}).status_code == 422


def test_hospital_name_query_cannot_broaden_facility_scope(client):
    seed("dispatched", approved_hospital_id="blr-007")
    own = client.get("/api/hospital/blr-007/incoming").json()["incoming"]
    other = client.get("/api/hospital/blr-001/incoming?name=Hospital").json()["incoming"]
    assert len(own) == 1
    assert other == []


def test_diverted_hospital_cannot_be_approved(client, mocked_route):
    seed()
    hospital_agent.set_divert("blr-007", True)
    response = client.post("/api/incidents/INC-review/approve", json={
        "dispatcher_id": "reviewer", "approved_hospital": "blr-007", "approved_route": "route",
    })
    assert response.status_code == 409
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "awaiting_dispatcher_approval"


def test_new_incident_uses_full_unique_id(client, monkeypatch):
    monkeypatch.setattr(incident, "run_pipeline", AsyncMock())
    response = client.post("/api/incidents", json={"citizen_text": "demo", "location": {"lat": 13.0, "lng": 77.6}})
    assert response.status_code == 202
    assert len(response.json()["incident_id"].removeprefix("INC-")) == 32


def test_legacy_migration_and_interrupted_report_recovery(client):
    incident.DB_FILE.write_text(json.dumps([{"incident_id": "old-report", "status": "processing"}]))
    incident.load_db()
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "failed"
    assert "restart" in incident.ACTIVE_INCIDENTS[0]["error"]
    stored = [json.loads(p) for p in storage.load_incident_payloads(database=incident.persistence_path())]
    assert stored[0]["status"] == "failed"
    assert incident.DB_FILE.exists()  # Migration preserves rollback source.


def test_chat_history_survives_new_connection_and_process_cache_reset(client):
    seed()
    message = {"incident_id": "INC-review", "sender_role": "dispatcher", "sender_name": "Review", "message": "Demo message"}
    assert client.post("/api/chat/INC-review/send", json=message).status_code == 200
    class Socket:
        async def accept(self):
            pass
    socket = Socket()
    asyncio.run(chat.manager.connect(socket, "INC-review"))
    assert len(chat.manager.history("INC-review")) == 1
    chat.manager.chat_history.clear()
    assert client.get("/api/chat/INC-review/messages").json()["messages"][0]["message"] == "Demo message"


def test_chat_channel_mismatch_is_rejected(client):
    response = client.post("/api/chat/one/send", json={"incident_id": "two", "sender_role": "citizen", "sender_name": "Review", "message": "demo"})
    assert response.status_code == 400


@pytest.mark.parametrize("ambulance_first", [False, True])
def test_primary_and_backup_claim_and_complete_independently(client, mocked_route, ambulance_first):
    report = seed("dispatched", vehicle_required="Fire Engine", include_ambulance_backup=True)
    primary = {"incident_id": "INC-review", "driver_id": "fire-review", "unit_type": "primary"}
    backup = {"incident_id": "INC-review", "driver_id": "ambulance-review", "unit_type": "ambulance"}
    assert client.post("/api/driver/claim", json=primary).status_code == 200
    assert client.post("/api/driver/claim", json=backup).status_code == 200
    assert report["unit_assignments"] == {"primary": "fire-review", "ambulance": "ambulance-review"}
    conflict = {**backup, "driver_id": "another-ambulance"}
    assert client.post("/api/driver/claim", json=conflict).status_code == 409
    assert client.post("/api/driver/arrived_pickup", json=primary).status_code == 409
    if not ambulance_first:
        scene = client.post("/api/incidents/INC-review/arrived?unit_type=primary")
        assert scene.status_code == 200
        assert scene.json()["incident_status"] == "dispatched"
        # Scene arrival must not clear the backup assignment from its queue.
        assert client.get("/api/driver/active").json()["dispatches"][0]["unit_statuses"]["ambulance"] == "dispatched"
    pickup = client.post("/api/driver/arrived_pickup", json=backup)
    assert pickup.status_code == 200
    assert pickup.json()["unit_statuses"]["ambulance"] == "patient_picked_up"
    # Restart cannot discard the independently claimed backup or its progress.
    incident.ACTIVE_INCIDENTS.clear()
    incident.load_db()
    report = incident.ACTIVE_INCIDENTS[0]
    assert report["unit_assignments"]["ambulance"] == "ambulance-review"
    assert len(client.get("/api/hospital/blr-007/incoming").json()["incoming"]) == 1
    handover = client.post("/api/incidents/INC-review/arrived?unit_type=ambulance")
    assert handover.status_code == 200
    if ambulance_first:
        assert handover.json()["incident_status"] == "dispatched"
        assert client.get("/api/hospital/blr-007/incoming").json()["incoming"] == []
        assert client.post("/api/incidents/INC-review/arrived?unit_type=primary").status_code == 200
    assert report["status"] == "completed"
    assert report["unit_statuses"] == {"primary": "arrived", "ambulance": "arrived"}
    assert client.get("/api/driver/active").json()["dispatches"] == []
    # Idempotent repeats retain both receipts, rather than returning early and
    # silently losing the backup unit's completion.
    assert client.post("/api/incidents/INC-review/arrived?unit_type=ambulance").json()["unit_statuses"] == report["unit_statuses"]


def test_backup_pickup_cannot_bypass_approval_or_claim_conflict(client, mocked_route):
    report = seed("awaiting_dispatcher_approval", vehicle_required="Fire Engine", include_ambulance_backup=True)
    payload = {"incident_id": "INC-review", "driver_id": "ambulance-review", "unit_type": "ambulance"}
    assert client.post("/api/driver/arrived_pickup", json=payload).status_code == 409
    report["status"] = "dispatched"
    assert client.post("/api/driver/claim", json=payload).status_code == 200
    assert client.post("/api/driver/arrived_pickup", json={**payload, "driver_id": "wrong-unit"}).status_code == 409
    assert report["status"] == "dispatched"


def test_diverted_hospital_cannot_be_selected_during_transport(client, mocked_route):
    report = seed("patient_picked_up")
    original_view = dict(report["citizen_view"])
    assert client.patch("/api/hospital/blr-007/resources", json={"icu_available": False}).status_code == 200
    response = client.post("/api/driver/change_hospital", json={"incident_id": "INC-review", "hospital_name": "blr-007"})
    assert response.status_code == 409
    assert report["citizen_view"] == original_view


def test_hospital_diversion_while_route_is_loading_does_not_commit(client, monkeypatch):
    report = seed("patient_picked_up")
    original_view = dict(report["citizen_view"])

    async def newly_diverted(origin, destination):
        hospital_agent.set_divert("blr-007", True)
        return {"route_coordinates": [origin, destination], "eta_minutes": 3, "distance_meters": 1000}

    monkeypatch.setattr(driver, "_route", newly_diverted)
    response = client.post("/api/driver/change_hospital", json={"incident_id": "INC-review", "hospital_name": "blr-007"})
    assert response.status_code == 409
    assert report["citizen_view"] == original_view


# --- No-candidate ranking -------------------------------------------------------

def test_rank_with_no_candidates_is_503_not_500(client, monkeypatch):
    monkeypatch.setattr(hospital_agent, "_fetch_live_hospitals", AsyncMock(return_value=[]))
    response = client.post("/api/hospital/rank", json={
        "incident_location": {"lat": 12.97, "lng": 77.59}, "emergency_type": "cardiac",
    })
    assert response.status_code == 503
    assert "No hospital is currently accepting patients" in response.json()["detail"]


def test_fusion_with_no_candidates_is_503(client, monkeypatch):
    monkeypatch.setattr(hospital_agent, "_fetch_live_hospitals", AsyncMock(return_value=[]))
    response = client.post("/api/fusion/combine", json={
        "incident_location": {"lat": 12.97, "lng": 77.59}, "emergency_type": "cardiac",
        "severity": 0.8, "vehicle_location": {"lat": 12.95, "lng": 77.6},
    })
    assert response.status_code == 503


def test_incident_with_no_candidates_ends_failed_with_readable_error(client, monkeypatch):
    monkeypatch.setattr(hospital_agent, "_fetch_live_hospitals", AsyncMock(return_value=[]))
    report = seed("processing")
    payload = incident.IncidentReportPayload(
        citizen_text="Chest pain and cannot breathe", location={"lat": 13.035, "lng": 77.597},
    )
    asyncio.run(incident.run_pipeline(report["incident_id"], payload))
    assert report["status"] == "failed"
    assert "No hospital is currently accepting patients" in report["error"]


# --- Demo ER phone numbers ------------------------------------------------------

def test_demo_phone_keys_are_curated_hospital_names_and_resolve():
    curated = {h["name"] for h in hospital_agent._load_capabilities().values()}
    assert set(incident.DEMO_HOSPITAL_PHONES) <= curated
    for name in curated:
        expected = incident.DEMO_HOSPITAL_PHONES.get(name)
        assert incident._hospital_phone(name) == expected
    assert incident._hospital_phone("NIMHANS (National Institute of Mental Health and Neurosciences)") == "+91 80 2699 5000"


# --- ER number follows a diversion ---------------------------------------------

@pytest.mark.parametrize("destination,expected", [
    ("blr-001", "+91 80 2502 4444"),  # Manipal Hospital (Old Airport Road)
    ("blr-013", None),               # Sparsh Hospital: curated, but no demo number
])
def test_diversion_updates_incident_level_er_phone(client, monkeypatch, destination, expected):
    report = seed("patient_picked_up", hospital_phone="+91 80 4342 0107")
    report["citizen_view"]["hospital_phone"] = "+91 80 4342 0107"
    report["action_plan"]["all_hospitals"] += [
        {"hospital_id": "blr-001", "name": "Manipal Hospital (Old Airport Road)", "location": {"lat": 12.9583, "lng": 77.6483}},
        {"hospital_id": "blr-013", "name": "Sparsh Hospital (Palace Road)", "location": {"lat": 12.9868, "lng": 77.5855}},
    ]

    async def route(origin, destination):
        return {"route_coordinates": [origin, destination], "eta_minutes": 5, "distance_meters": 2000}

    monkeypatch.setattr(driver, "_route", route)
    response = client.post("/api/driver/change_hospital", json={"incident_id": "INC-review", "hospital_name": destination})
    assert response.status_code == 200
    assert report["hospital_phone"] == expected
    assert report["citizen_view"]["hospital_phone"] == expected
    assert response.json()["citizen_view"]["hospital_phone"] == expected


# --- Log injection --------------------------------------------------------------

def test_reject_log_line_cannot_be_forged_by_dispatcher_id(client, caplog):
    seed()
    caplog.set_level(logging.INFO)
    response = client.post("/api/incidents/INC-review/reject", params={"dispatcher_id": "ops\nINFO forged entry\r"})
    assert response.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("REJECTED")]
    assert len(lines) == 1
    assert "\n" not in lines[0] and "\r" not in lines[0]
    assert "forged entry" in lines[0]


def test_approve_log_line_cannot_be_forged_by_dispatcher_id(client, mocked_route, caplog):
    seed()
    caplog.set_level(logging.INFO)
    response = client.post("/api/incidents/INC-review/approve", json={
        "dispatcher_id": "ops\nINFO forged entry", "approved_hospital": "blr-007", "approved_route": "route",
    })
    assert response.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PLAN_APPROVED")]
    assert len(lines) == 1
    assert "\n" not in lines[0] and "forged entry" in lines[0]


def test_websocket_failure_log_line_cannot_be_forged_by_incident_id(client, caplog):
    caplog.set_level(logging.ERROR)
    seed(incident_id="INC-x\nERROR forged")  # a channel exists only for a stored incident
    with client.websocket_connect("/ws/chat/INC-x%0AERROR%20forged") as ws:
        ws.send_text("not json")
        # Frames are handled in order: once this echo arrives, the bad one was processed.
        ws.send_json({"message": "after", "sender_role": "citizen", "sender_name": "Review"})
        assert ws.receive_json()["message"] == "after"
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("WebSocket chat message failed")]
    assert len(lines) == 1
    assert "\n" not in lines[0] and "forged" in lines[0]



@pytest.mark.parametrize("status", ["failed", "completed"])
def test_terminal_incident_cannot_be_approved(client, status):
    report = seed(status)
    response = client.post("/api/incidents/INC-review/approve", json={
        "dispatcher_id": "reviewer", "approved_hospital": "blr-007", "approved_route": "route",
    })
    assert response.status_code == 409
    assert report["status"] == status
    assert "assigned_driver" not in report


# --- Audit fixes: input bounds, service area, city model, diversion, chat -------

REPORT = {"citizen_text": "Chest pain", "location": {"lat": 13.035, "lng": 77.597}}


@pytest.mark.parametrize("change", [
    {"citizen_text": "x" * 2001},
    {"citizen_text": "   "},
    {"citizen_name": "n" * 101},
    {"citizen_phone": "9" * 33},
    {"vehicle_required": "Helicopter"},
    {"location": {"lat": 28.6139, "lng": 77.2090}},   # Delhi
    {"location": {"lat": 0, "lng": 0}},
    {"location": {"lat": -33.87, "lng": 151.21}},     # Sydney
])
def test_report_rejects_unbounded_or_out_of_area_input(client, monkeypatch, change):
    monkeypatch.setattr(incident, "run_pipeline", AsyncMock())
    response = client.post("/api/incidents", json={**REPORT, **change})
    assert response.status_code == 422
    assert incident.ACTIVE_INCIDENTS == []


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_coordinates_are_422_not_500(client, value):
    body = '{"citizen_text": "Chest pain", "location": {"lat": %s, "lng": 77.6}}' % value
    response = client.post("/api/incidents", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_report_inside_the_service_area_is_accepted(client, monkeypatch):
    monkeypatch.setattr(incident, "run_pipeline", AsyncMock())
    assert client.post("/api/incidents", json=REPORT).status_code == 202


def test_approve_and_reject_bound_the_dispatcher_id(client, mocked_route):
    seed()
    long_id = {"dispatcher_id": "d" * 101, "approved_hospital": "blr-007", "approved_route": "r"}
    assert client.post("/api/incidents/INC-review/approve", json=long_id).status_code == 422
    assert client.post("/api/incidents/INC-review/approve", json={**long_id, "dispatcher_id": ""}).status_code == 422
    assert client.post("/api/incidents/INC-review/reject", params={"dispatcher_id": "d" * 101}).status_code == 422
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "awaiting_dispatcher_approval"


@pytest.mark.parametrize("edge_ids", [[999999999], [-1]])
@pytest.mark.parametrize("path", ["/api/city/incident", "/api/city/close"])
def test_unknown_road_segment_is_refused_and_leaves_the_city_model_working(client, path, edge_ids):
    from agents.city_state import city_state
    with client:  # run startup so the road graph is attached
        try:
            assert client.post(path, json={"edge_ids": edge_ids}).status_code == 422
            assert client.get("/api/city/overlay").status_code == 200
            assert client.get("/api/city/state").status_code == 200
        finally:
            city_state.clear()


def test_demo_can_reset_the_city_without_an_operator_token(client, monkeypatch):
    monkeypatch.delenv("RAPIDGRID_OPERATOR_TOKEN", raising=False)
    assert client.post("/api/city/clear").status_code == 200
    # Case records stay protected even in the demo.
    assert client.post("/api/incidents/clear").status_code == 503


def test_divert_after_approval_is_shown_to_driver_and_er(client, mocked_route):
    seed("dispatched", approved_hospital_id="blr-007")
    assert client.get("/api/driver/active").json()["dispatches"][0]["destination_on_divert"] is False
    hospital_agent.set_divert("blr-007", True)
    assert client.get("/api/driver/active").json()["dispatches"][0]["destination_on_divert"] is True
    assert client.get("/api/hospital/blr-007/incoming").json()["incoming"][0]["destination_on_divert"] is True
    # The patient is still picked up; the driver is told to choose another hospital.
    pickup = client.post("/api/driver/arrived_pickup", json={"incident_id": "INC-review", "driver_id": "drv-1"})
    assert pickup.status_code == 200
    assert pickup.json()["destination_on_divert"] is True


def test_all_hospitals_on_divert_fails_the_report_with_a_reason(client, monkeypatch):
    # No live Overpass: the catalogue is the curated seed list, so every
    # candidate has a divert key and the result does not depend on the network.
    async def no_network(*args, **kwargs):
        raise httpx.ConnectError("offline in tests")
    monkeypatch.setattr(httpx.AsyncClient, "get", no_network)
    for key in hospital_agent._load_capabilities():
        hospital_agent.set_divert(key, True)
    report = seed("processing")
    payload = incident.IncidentReportPayload(
        citizen_text="Chest pain and cannot breathe", location={"lat": 13.035, "lng": 77.597},
    )
    asyncio.run(incident.run_pipeline(report["incident_id"], payload))
    assert report["status"] == "failed"
    assert "No hospital is currently accepting patients" in report["error"]


def test_offline_route_does_not_label_the_plan_live(client, monkeypatch):
    monkeypatch.setattr(incident.route_agent, "api_key", "")
    report = seed("processing")
    payload = incident.IncidentReportPayload(
        citizen_text="Chest pain and cannot breathe", location={"lat": 13.035, "lng": 77.597},
    )
    with client:
        asyncio.run(incident.run_pipeline(report["incident_id"], payload))
    assert report["status"] == "awaiting_dispatcher_approval", report.get("error")
    assert report["action_plan"]["data_freshness"] != "live"


def test_chat_needs_a_reported_incident_and_bounded_fields(client):
    message = {"incident_id": "INC-ghost", "sender_role": "citizen", "sender_name": "Review", "message": "hello"}
    assert client.post("/api/chat/INC-ghost/send", json=message).status_code == 404
    assert client.get("/api/chat/INC-ghost/messages").json() == {"messages": []}
    assert "INC-ghost" not in chat.manager.chat_history
    seed()
    ok = {**message, "incident_id": "INC-review"}
    assert client.post("/api/chat/INC-review/send", json=ok).status_code == 200
    assert client.post("/api/chat/INC-review/send", json={**ok, "sender_role": "system"}).status_code == 422
    assert client.post("/api/chat/INC-review/send", json={**ok, "message": "m" * 2001}).status_code == 422
    assert client.post("/api/chat/INC-review/send", json={**ok, "sender_name": "n" * 81}).status_code == 422
    assert len(chat.manager.history("INC-review")) == 1


def test_chat_socket_refuses_unknown_incident_and_survives_bad_frames(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat/INC-ghost"):
            pass
    seed()
    with client.websocket_connect("/ws/chat/INC-review") as ws:
        ws.send_bytes(b"\x00\x01")                                  # binary frame is ignored
        ws.send_json({"message": "spoof", "sender_role": "system"})  # unknown role is dropped
        ws.send_json({"message": "m" * 2001, "sender_role": "citizen"})
        ws.send_json({"message": "still here", "sender_role": "citizen", "sender_name": "Review"})
        assert ws.receive_json()["message"] == "still here"
    assert [m["message"] for m in chat.manager.history("INC-review")] == ["still here"]


def test_provider_request_urls_are_not_logged():
    # httpx logs full request URLs at INFO; the weather URL carries the API key.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_chat_socket_closes_when_its_incident_is_cleared(client):
    from starlette.websockets import WebSocketDisconnect
    seed()
    with client.websocket_connect("/ws/chat/INC-review") as ws:
        incident.ACTIVE_INCIDENTS.clear()  # an operator cleared incidents meanwhile
        ws.send_json({"message": "late message", "sender_role": "citizen", "sender_name": "Review"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert chat.manager.chat_history.get("INC-review", []) == []  # nothing was stored
    assert storage.load_chat("INC-review", database=incident.persistence_path()) == []


def test_reroute_response_describes_the_new_destination(client, mocked_route):
    other = {"hospital_id": "blr-001", "name": "Manipal Hospital (Old Airport Road)",
             "location": {"lat": 12.9585, "lng": 77.6483}}
    report = seed("patient_picked_up", approved_hospital_id="blr-007")
    report["action_plan"]["all_hospitals"].append(other)
    hospital_agent.set_divert("blr-007", True)
    assert client.get("/api/driver/active").json()["dispatches"][0]["destination_on_divert"] is True
    response = client.post("/api/driver/change_hospital", json={"incident_id": "INC-review", "hospital_name": other["name"]})
    assert response.status_code == 200, response.text
    assert response.json()["hospital_name"] == other["name"]
    assert response.json()["destination_on_divert"] is False


def test_backup_ambulance_is_only_added_beside_a_non_medical_unit(client, monkeypatch, mocked_route):
    monkeypatch.setattr(incident, "run_pipeline", AsyncMock())
    medical = client.post("/api/incidents", json={**REPORT, "include_ambulance_backup": True}).json()["incident_id"]
    fire = client.post("/api/incidents", json={**REPORT, "vehicle_required": "Fire Engine",
                                               "include_ambulance_backup": True}).json()["incident_id"]
    by_id = {i["incident_id"]: i for i in incident.ACTIVE_INCIDENTS}
    assert by_id[medical]["include_ambulance_backup"] is False
    assert by_id[fire]["include_ambulance_backup"] is True
    by_id[medical]["status"] = "dispatched"
    assert client.post(f"/api/incidents/{medical}/request_ambulance").status_code == 409
    assert incident.required_units(by_id[medical]) == {"primary"}  # the call can still complete


def test_reroute_response_carries_the_new_er_number(client, mocked_route):
    other = {"hospital_id": "blr-001", "name": "Manipal Hospital (Old Airport Road)",
             "location": {"lat": 12.9585, "lng": 77.6483}}
    report = seed("patient_picked_up", hospital_phone="+91 80 4342 0107")
    report["action_plan"]["all_hospitals"].append(other)
    body = client.post("/api/driver/change_hospital", json={"incident_id": "INC-review", "hospital_name": other["name"]}).json()
    assert body["hospital_phone"] == "+91 80 2502 4444" == body["citizen_view"]["hospital_phone"]


# --- Row-level persistence -------------------------------------------------------

def _stored(incident_id="INC-review"):
    rows = [json.loads(p) for p in storage.load_incident_payloads(database=incident.persistence_path())]
    return next((r for r in rows if r["incident_id"] == incident_id), None)


def test_a_change_writes_only_its_own_incident(client, mocked_route, monkeypatch):
    seed()
    seed(incident_id="INC-other")
    incident.save_db()
    writes = []
    real = storage.write_incident
    monkeypatch.setattr(storage, "write_incident", lambda incident_id, payload, **kw: (writes.append(incident_id), real(incident_id, payload, **kw))[1])
    monkeypatch.setattr(storage, "replace_incidents", lambda *a, **kw: pytest.fail("a single change rewrote every incident"))
    assert client.post("/api/incidents/INC-review/reject").status_code == 200
    assert writes == ["INC-review"]
    assert _stored()["status"] == "failed" and _stored("INC-other")["status"] == "awaiting_dispatcher_approval"


def test_failed_write_leaves_memory_matching_disk_and_a_retry_works(client, mocked_route, monkeypatch):
    report = seed()
    incident.save_db()
    body = {"dispatcher_id": "reviewer", "approved_hospital": "blr-007", "approved_route": "r"}
    real = storage.write_incident

    def broken(*args, **kwargs):
        raise OSError("disk is read-only")
    monkeypatch.setattr(storage, "write_incident", broken)
    failing = TestClient(app, raise_server_exceptions=False)
    assert failing.post("/api/incidents/INC-review/approve", json=body).status_code == 500
    # Nothing moved: memory still says what disk says, and the call can be retried.
    assert report["status"] == "awaiting_dispatcher_approval"
    assert "dispatcher_id" not in report and "unit_statuses" not in report
    assert client.get("/api/incidents/INC-review").json()["status"] == "awaiting_dispatcher_approval"
    assert _stored()["status"] == "awaiting_dispatcher_approval"

    monkeypatch.setattr(storage, "write_incident", real)
    assert client.post("/api/incidents/INC-review/approve", json=body).status_code == 200
    assert _stored()["status"] == "dispatched"


def test_report_that_cannot_be_stored_is_not_left_processing(client, monkeypatch):
    pipeline = AsyncMock()
    monkeypatch.setattr(incident, "run_pipeline", pipeline)

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(storage, "write_incident", broken)
    failing = TestClient(app, raise_server_exceptions=False)
    assert failing.post("/api/incidents", json=REPORT).status_code == 500
    assert incident.ACTIVE_INCIDENTS == []
    pipeline.assert_not_called()


def test_finished_calls_beyond_the_limit_are_pruned_with_their_chat(client, monkeypatch):
    monkeypatch.setattr(incident, "run_pipeline", AsyncMock())
    monkeypatch.setattr(incident, "KEEP_FINISHED", 2)
    for n in range(4):
        seed("completed" if n % 2 else "failed", incident_id=f"INC-old-{n}")
    seed("dispatched", incident_id="INC-live")
    incident.save_db()
    message = {"incident_id": "INC-old-0", "sender_role": "citizen", "sender_name": "Review", "message": "old"}
    assert client.post("/api/chat/INC-old-0/send", json=message).status_code == 200

    assert client.post("/api/incidents", json=REPORT).status_code == 202
    kept = [i["incident_id"] for i in incident.ACTIVE_INCIDENTS]
    assert "INC-old-0" not in kept and "INC-old-1" not in kept       # oldest finished calls go
    assert {"INC-old-2", "INC-old-3", "INC-live"} <= set(kept)        # active calls are never pruned
    assert _stored("INC-old-0") is None
    assert storage.load_chat("INC-old-0", database=incident.persistence_path()) == []
    assert "INC-old-0" not in chat.manager.chat_history


def test_earlier_single_snapshot_is_migrated_to_rows(client):
    database = incident.persistence_path()
    save_collection("incidents", [{"incident_id": "INC-legacy", "status": "dispatched"}], database=database)
    save_collection("chat:INC-legacy", [{"id": "m1", "message": "from the old layout"}], database=database)
    incident.load_db()
    assert incident.ACTIVE_INCIDENTS[0]["incident_id"] == "INC-legacy"
    assert _stored("INC-legacy")["status"] == "dispatched"
    assert load_collection("incidents", database=database) is not None   # kept for rollback
    assert chat.manager.history("INC-legacy")[0]["message"] == "from the old layout"
    assert len(storage.load_chat("INC-legacy", database=database)) == 1


def test_state_and_chat_reload_from_rows_after_a_restart(client, mocked_route):
    seed("dispatched", unit_assignments={"primary": "drv-7"})
    incident.save_db()
    message = {"incident_id": "INC-review", "sender_role": "driver", "sender_name": "Unit 7", "message": "en route"}
    for _ in range(3):
        assert client.post("/api/chat/INC-review/send", json=message).status_code == 200
    incident.ACTIVE_INCIDENTS.clear()
    incident._PERSISTED.clear()
    chat.manager.chat_history.clear()
    storage.close_all()            # as a new process would start
    incident.load_db()
    assert incident.ACTIVE_INCIDENTS[0]["unit_assignments"] == {"primary": "drv-7"}
    assert len(chat.manager.history("INC-review")) == 3


def test_malformed_stored_record_stops_startup_instead_of_serving_500s(client):
    storage.write_incident("x", json.dumps({"status": "dispatched"}), database=incident.persistence_path())
    with pytest.raises(RuntimeError):
        incident.load_db()
    storage.delete_incidents(["x"], database=incident.persistence_path())
    incident.load_db()


def test_cleared_incidents_do_not_come_back_from_the_old_snapshot(client):
    database = incident.persistence_path()
    save_collection("incidents", [{"incident_id": "INC-legacy", "status": "dispatched"}], database=database)
    incident.load_db()                       # migrates once
    assert [i["incident_id"] for i in incident.ACTIVE_INCIDENTS] == ["INC-legacy"]
    incident.ACTIVE_INCIDENTS.clear()
    incident.save_db()                       # e.g. an operator cleared incidents
    storage.close_all()
    incident.load_db()                       # restart
    assert incident.ACTIVE_INCIDENTS == []


def test_legacy_chat_is_imported_once_even_if_loaded_twice(client):
    database = incident.persistence_path()
    save_collection("chat:INC-old", [{"id": "m1", "message": "old"}], database=database)
    first = storage.load_chat_migrating("INC-old", database=database)
    second = storage.load_chat_migrating("INC-old", database=database)
    assert first == second and len(storage.load_chat("INC-old", database=database)) == 1


def test_a_pruning_failure_does_not_lose_the_report(client, monkeypatch):
    pipeline = AsyncMock()
    monkeypatch.setattr(incident, "run_pipeline", pipeline)

    def broken():
        raise OSError("disk hiccup")
    monkeypatch.setattr(incident, "prune_finished", broken)
    response = client.post("/api/incidents", json=REPORT)
    assert response.status_code == 202
    assert _stored(response.json()["incident_id"])["status"] == "processing"
    pipeline.assert_called_once()


def test_a_poll_never_shows_a_change_whose_write_then_fails(client, mocked_route, monkeypatch):
    import time
    # A fresh lock: asyncio binds a lock to the first event loop that waits on it.
    monkeypatch.setattr(incident, "_INCIDENTS_LOCK", asyncio.Lock())
    seed()
    incident.save_db()

    def slow_failing_write(*args, **kwargs):
        time.sleep(0.3)
        raise OSError("disk is read-only")
    monkeypatch.setattr(storage, "write_incident", slow_failing_write)
    body = incident.ApprovePayload(dispatcher_id="reviewer", approved_hospital="blr-007", approved_route="r")

    async def race():
        approval = asyncio.create_task(incident.approve_incident("INC-review", body))
        await asyncio.sleep(0.1)                      # approval is now waiting on its write
        polled = await incident.get_incident("INC-review")
        listed = await incident.get_all_incidents()
        with pytest.raises(OSError):
            await approval
        return polled, listed

    polled, listed = asyncio.run(race())
    assert polled["status"] == "awaiting_dispatcher_approval"
    assert listed["incidents"][0]["status"] == "awaiting_dispatcher_approval"


def test_pipeline_failure_waits_for_a_pending_rejection(client, monkeypatch):
    import time
    # A fresh lock: asyncio binds a lock to the first event loop that waits on it.
    monkeypatch.setattr(incident, "_INCIDENTS_LOCK", asyncio.Lock())
    report = seed("processing")
    incident.save_db()
    real = storage.write_incident
    calls = {"n": 0}

    def write(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:          # the rejection's write: slow, then fails
            time.sleep(0.3)
            raise OSError("disk hiccup")
        return real(*args, **kwargs)
    monkeypatch.setattr(storage, "write_incident", write)

    async def race():
        rejection = asyncio.create_task(incident.reject_incident("INC-review", dispatcher_id="d1"))
        await asyncio.sleep(0.1)
        await incident._mark_failed("INC-review", RuntimeError("pipeline broke"))
        with pytest.raises(OSError):
            await rejection

    asyncio.run(race())
    # The rejection rolled back, then the pipeline failure was recorded.
    assert report["status"] == "failed" and "pipeline broke" in report["error"]
    assert _stored()["status"] == "failed"


def test_pruned_incident_legacy_chat_is_not_reimported(client):
    database = incident.persistence_path()
    save_collection("chat:INC-gone", [{"id": "m1", "message": "old"}], database=database)
    storage.write_incident("INC-gone", json.dumps({"incident_id": "INC-gone", "status": "completed"}), database=database)
    storage.delete_incidents(["INC-gone"], database=database)
    assert storage.load_chat_migrating("INC-gone", database=database) == []


def test_failure_marker_retries_until_the_write_succeeds(client, monkeypatch):
    monkeypatch.setattr(incident, "_INCIDENTS_LOCK", asyncio.Lock())
    monkeypatch.setattr(incident, "_FAIL_RETRY_DELAYS", (0.01,))
    report = seed("processing")
    incident.save_db()
    real = storage.write_incident
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("disk hiccup")
        return real(*args, **kwargs)
    monkeypatch.setattr(storage, "write_incident", flaky)
    asyncio.run(incident._mark_failed("INC-review", RuntimeError("pipeline broke")))
    assert calls["n"] == 3
    assert report["status"] == "failed" and _stored()["status"] == "failed"


def test_triage_result_survives_a_rolled_back_rejection(client, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(incident, "_INCIDENTS_LOCK", asyncio.Lock())
    report = seed("processing")
    for key in ("emergency_type", "severity", "details"):
        report.pop(key, None)
    incident.save_db()
    triage = SimpleNamespace(incident_report=SimpleNamespace(
        incident_type=SimpleNamespace(value="cardiac"), severity_estimate=0.9, extracted_details="chest pain",
        triage_notices=[]))
    monkeypatch.setattr(incident.accessibility_agent, "process_input", AsyncMock(return_value=triage))

    async def stop_after_triage(*args, **kwargs):
        raise RuntimeError("stop here")  # fusion is not under test
    monkeypatch.setattr(incident.fusion_engine, "fuse", stop_after_triage)
    payload = incident.IncidentReportPayload(citizen_text="Chest pain", location={"lat": 13.035, "lng": 77.597})
    monkeypatch.setattr(incident, "_mark_failed", AsyncMock())  # keep the record "processing"
    asyncio.run(incident.run_pipeline("INC-review", payload))
    stored = _stored()
    assert stored["emergency_type"] == "cardiac" and stored["severity"] == 0.9
    # A rollback now restores to a baseline that includes the triage result.
    incident._restore(report)
    assert report["emergency_type"] == "cardiac"


# --- Truthfulness: what may influence the recommendation ------------------------

def _rank_offline(monkeypatch, lat=13.035, lng=77.597, etype="cardiac"):
    from models.schemas import EmergencyType, HospitalRequest

    async def no_network(*args, **kwargs):
        raise httpx.ConnectError("offline in tests")
    monkeypatch.setattr(httpx.AsyncClient, "get", no_network)
    return asyncio.run(hospital_agent.rank_hospitals(HospitalRequest(
        incident_location={"lat": lat, "lng": lng}, emergency_type=EmergencyType(etype),
    ))).ranked_hospitals


def _bed_raw(entry):
    return next(b.raw_score for b in entry.score_breakdown if b.factor == "Bed availability")


def test_generated_bed_counts_do_not_score(client, monkeypatch):
    ranked = _rank_offline(monkeypatch)
    assert {_bed_raw(e) for e in ranked} == {0.5}  # identical for every hospital


def test_bed_count_entered_by_an_er_desk_changes_the_ranking(client, monkeypatch):
    before = _rank_offline(monkeypatch)
    top = before[0]
    key = hospital_agent.resolve_key(top.name)
    response = client.patch(f"/api/hospital/{key}/resources", json={"icu_available": True, "beds_available": 0})
    assert response.status_code == 200
    after = {e.name: e for e in _rank_offline(monkeypatch)}
    assert _bed_raw(after[top.name]) == 0.0
    assert after[top.name].score < top.score
    # Clearing the figure returns it to neutral.
    client.patch(f"/api/hospital/{key}/resources", json={"icu_available": True, "beds_available": None})
    assert _bed_raw({e.name: e for e in _rank_offline(monkeypatch)}[top.name]) == 0.5


def test_traffic_factor_follows_the_city_model(client, monkeypatch):
    from agents.city_state import city_state
    from agents.decision_fusion import fusion_engine
    from models.schemas import EmergencyType, FusionRequest

    async def no_network(*args, **kwargs):
        raise httpx.ConnectError("offline in tests")
    monkeypatch.setattr(httpx.AsyncClient, "get", no_network)
    request = FusionRequest(incident_id="INC-t", incident_location={"lat": 13.035, "lng": 77.597},
                            emergency_type=EmergencyType.CARDIAC, severity=0.8,
                            vehicle_location={"lat": 13.035, "lng": 77.597})

    def traffic(plan):
        return next(f for f in plan.model_dump(mode="json")["factor_breakdown"] if f["factor"] == "Traffic Conditions")

    with client:  # attaches the road graph and city model
        hour = city_state.hour
        try:
            city_state.set_hour(3)
            city_state.set_weather("Clear")
            calm = asyncio.run(fusion_engine.fuse(request))
            city_state.set_hour(9)
            city_state.set_weather("Thunderstorm")
            busy = asyncio.run(fusion_engine.fuse(request))
        finally:
            city_state.set_weather("Clear")
            city_state.set_hour(hour)
    assert traffic(busy)["score"] < traffic(calm)["score"]
    assert "1 active incident" not in calm.explanation
    # The rationale panel splits the explanation on "TRAFFIC:"; same pattern as AIRationale.jsx.
    import re
    keys = ["ROUTE", "HOSPITAL", "TRAFFIC", "ALTERNATIVE CONSIDERED", "FUSION", "WARNING"]
    section = re.search(r"TRAFFIC:\s*(.*?)(?=\s(?:%s):|$)" % "|".join(keys), calm.explanation, re.S)
    assert section and section.group(1).startswith("Modelled, no live feed")


def test_saving_beds_does_not_change_divert_state(client):
    hospital_agent.set_divert("blr-007", True)
    client.patch("/api/hospital/blr-007/resources", json={"icu_available": False})
    response = client.patch("/api/hospital/blr-007/resources", json={"beds_available": 4})
    assert response.status_code == 200
    assert response.json()["on_divert"] is True and response.json()["beds_available"] == 4
    assert "blr-007" in hospital_agent.diverted
    # A bed-only override on a hospital never toggled restores cleanly at startup.
    assert client.patch("/api/hospital/blr-001/resources", json={"beds_available": 9}).status_code == 200
    hospital.restore_capacity()
    assert "blr-001" not in hospital_agent.diverted


def test_traffic_factor_is_neutral_and_labelled_when_the_city_model_is_absent(client, monkeypatch):
    from agents.city_state import city_state
    from agents.decision_fusion import fusion_engine
    from models.schemas import EmergencyType, FusionRequest

    async def no_network(*args, **kwargs):
        raise httpx.ConnectError("offline in tests")
    monkeypatch.setattr(httpx.AsyncClient, "get", no_network)
    monkeypatch.setattr(type(city_state), "ready", property(lambda self: False))
    plan = asyncio.run(fusion_engine.fuse(FusionRequest(
        incident_id="INC-t", incident_location={"lat": 13.035, "lng": 77.597},
        emergency_type=EmergencyType.CARDIAC, severity=0.8, vehicle_location={"lat": 13.035, "lng": 77.597})))
    traffic = next(f for f in plan.factor_breakdown if f.factor == "Traffic Conditions")
    assert traffic.score == 0.5
    assert "unavailable" in plan.explanation and "Clear" not in plan.explanation
