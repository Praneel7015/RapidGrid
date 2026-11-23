"""Fixes from CodeRabbit's review of the finals PR (#21): calls, fleet, chat
and the agent's proposals stay consistent when things change or fail."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
import storage
from routers import chat, incident, monitoring
from tests.test_workflow_regressions import client, mocked_route, seed  # noqa: F401  (fixtures)

APPROVE = {"dispatcher_id": "reviewer", "approved_hospital": "blr-007", "approved_route": "r"}


def _busy_ambulances():
    fleet.units()  # loaded
    held = []
    for u in fleet._units.values():
        if u["type"] == "Ambulance" and u["status"] == "available":
            u.update(status="assigned", incident_id="INC-elsewhere")
            held.append(u["unit_id"])
    return held


def test_a_call_with_no_free_unit_stays_in_the_queue_until_one_is_back(client, mocked_route):
    seed()
    held = _busy_ambulances()
    r = client.post("/api/incidents/INC-review/approve", json=APPROVE)
    assert r.status_code == 409 and "free" in r.json()["detail"]
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "awaiting_dispatcher_approval"
    fleet._units[held[0]].update(status="available", incident_id=None)
    r = client.post("/api/incidents/INC-review/approve", json=APPROVE)
    assert r.status_code == 200 and r.json()["assigned_unit"] == held[0]


def test_a_failed_approval_write_frees_the_unit_it_reserved(client, mocked_route, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    asyncio.run(incident.commit(seed()))  # stored as awaiting approval

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "write_incident", broken)
    r = TestClient(app, raise_server_exceptions=False).post("/api/incidents/INC-review/approve", json=APPROVE)
    assert r.status_code == 500
    assert fleet.unit_for_incident("INC-review") is None
    assert incident.ACTIVE_INCIDENTS[0]["status"] == "awaiting_dispatcher_approval"


def test_a_fleet_assignment_that_cannot_be_stored_is_undone(client, monkeypatch):
    fleet.units()

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_collection", broken)
    with pytest.raises(OSError):
        fleet.assign("INC-x", "Ambulance", near={"lat": 13.0, "lng": 77.6})
    assert fleet.unit_for_incident("INC-x") is None


def test_changing_hospital_by_id_alone_closes_the_old_destinations_proposal(client, mocked_route):
    other = {"hospital_id": "blr-001", "name": "Other Hospital", "location": {"lat": 12.97, "lng": 77.6}}
    report = seed("patient_picked_up", assigned_unit="AMB-01", approved_hospital_id="blr-007")
    report["action_plan"]["all_hospitals"].append(other)
    report["monitoring"] = {"alerts": [{"alert_id": "A1", "status": "open", "leg": "to_hospital",
                                        "unit_id": "AMB-01", "routes": [{"label": "Via X"}]}]}
    r = client.post("/api/driver/change_hospital", json={"incident_id": "INC-review", "hospital_id": "blr-001"})
    assert r.status_code == 200, r.text
    assert report["monitoring"]["alerts"][0]["status"] == "superseded"
    accept = client.post("/api/incidents/INC-review/alerts/A1/accept", json={"route_index": 0})
    assert accept.status_code == 409


def test_a_late_backup_plan_keeps_the_destination_already_attached(client, monkeypatch):
    report = seed("dispatched", vehicle_required="Fire Engine", action_plan={"response_profile": "scene"})
    report["citizen_view"].pop("hospital_location")

    async def plan(inc):
        # Meanwhile, another request attached a hospital and the crew left.
        report["citizen_view"]["hospital_location"] = {"lat": 1.0, "lng": 2.0}
        return {"hospital": {"hospital_id": "blr-001", "name": "Late", "location": {"lat": 9.0, "lng": 9.0}},
                "ranked": [], "route": {}}

    monkeypatch.setattr(incident, "_backup_destination", plan)
    assert client.post("/api/incidents/INC-review/request_ambulance").status_code == 200
    assert report["citizen_view"]["hospital_location"] == {"lat": 1.0, "lng": 2.0}


def test_a_message_to_a_cleared_call_is_not_reported_as_sent(client, monkeypatch):
    seed("dispatched")
    real = chat.manager.load

    async def cleared_meanwhile(incident_id):
        history = await real(incident_id)
        incident.ACTIVE_INCIDENTS.clear()
        return history

    monkeypatch.setattr(chat.manager, "load", cleared_meanwhile)
    r = client.post("/api/chat/INC-review/send", json={
        "incident_id": "INC-review", "sender_role": "citizen", "sender_name": "A", "message": "hello"})
    assert r.status_code == 410
    assert storage.load_chat_migrating("INC-review", database=incident.persistence_path()) == []


def test_a_cancelled_post_keeps_the_write_lock_until_the_row_is_in(client, monkeypatch):
    seed("dispatched")
    import threading
    import time

    started, order = threading.Event(), []
    real = storage.append_chat

    def slow_append(*a, **k):
        started.set()
        time.sleep(0.3)
        real(*a, **k)
        order.append("insert")

    monkeypatch.setattr(storage, "append_chat", slow_append)

    async def scenario():
        post = asyncio.create_task(chat.post_system_message("INC-review", "note"))
        while not started.is_set():
            await asyncio.sleep(0.01)
        post.cancel()
        await chat.manager.clear_all()
        order.append("clear")

    asyncio.run(scenario())
    assert order == ["insert", "clear"]
    assert storage.load_chat_migrating("INC-review", database=incident.persistence_path()) == []


def test_a_disruption_is_not_staged_after_the_call_moved_on(client, monkeypatch):
    report = seed("dispatched", assigned_unit="AMB-01")
    fleet.assign("INC-review", "Ambulance", near=report["location"])
    report["assigned_unit"] = fleet.unit_for_incident("INC-review")["unit_id"]
    report["citizen_view"]["phase1_route_coordinates"] = [{"lat": 13.03, "lng": 77.59}, {"lat": 13.035, "lng": 77.597}]

    def edges(route, index, position, keep_open=()):
        report["status"] = "patient_picked_up"  # pickup happened during the search
        return [1, 2]

    monkeypatch.setattr(monitoring, "edges_to_close", edges)
    closed = []
    monkeypatch.setattr(monitoring.city_state, "close_road", lambda *a, **k: closed.append(a) or {})
    r = client.post("/api/monitoring/demo/INC-review", json={"action": "close_road_ahead"})
    assert r.status_code == 409 and closed == []


def test_a_scene_call_backup_goes_to_the_next_open_hospital(client, mocked_route):
    from agents.hospital_intelligence import hospital_agent

    first = {"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)", "location": {"lat": 13.0473, "lng": 77.5908}}
    second = {"hospital_id": "blr-001", "name": "Next Open", "location": {"lat": 12.97, "lng": 77.6}}
    report = seed(vehicle_required="Fire Engine", include_ambulance_backup=True,
                  action_plan={"response_profile": "scene", "recommended_hospital": first,
                               "all_hospitals": [first, second]})
    hospital_agent._on_divert.add("blr-007")
    r = client.post("/api/incidents/INC-review/approve", json={"dispatcher_id": "r", "approved_route": "scene"})
    assert r.status_code == 200, r.text
    assert report["approved_hospital_id"] == "blr-001"
    assert report["citizen_view"]["hospital_location"] == second["location"]
