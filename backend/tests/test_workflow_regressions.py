"""Regressions for live review findings; all state and provider calls are isolated."""
from __future__ import annotations

import asyncio
import json
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
    yield TestClient(app)
    incident.ACTIVE_INCIDENTS.clear()
    chat.manager.chat_history.clear()
    chat.manager.active_connections.clear()
    hospital_agent._on_divert.clear()
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
    response = client.post("/api/incidents", json={"citizen_text": "demo", "location": {"lat": 13.0, "lng": 77.0}})
    assert response.status_code == 202
    assert len(response.json()["incident_id"].removeprefix("INC-")) == 32


def test_legacy_migration_and_interrupted_report_recovery(client):
    incident.DB_FILE.write_text(json.dumps([{"incident_id": "old-report", "status": "processing"}]))
    incident.load_db()
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "failed"
    assert "restart" in incident.ACTIVE_INCIDENTS[0]["error"]
    assert load_collection("incidents", database=incident.persistence_path())[0]["status"] == "failed"
    assert incident.DB_FILE.exists()  # Migration preserves rollback source.


def test_chat_history_survives_new_connection_and_process_cache_reset(client):
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
