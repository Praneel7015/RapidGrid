"""Pickup with no road to the planned hospital: each fallback is checked for
divert right before and right after routing, so a hospital that went on
divert meanwhile is skipped and the next reviewed one is taken (CodeRabbit,
PR 22: recheck diversion before routing to each fallback)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.hospital_intelligence import hospital_agent
from agents.local_router import local_router
from routers import driver, incident as incident_router

PLANNED = {"hospital_id": "blr-007", "name": "Planned", "location": {"lat": 13.0473, "lng": 77.5908}}
B = {"hospital_id": "blr-002", "name": "Fallback B", "location": {"lat": 13.01, "lng": 77.60}}
C = {"hospital_id": "blr-003", "name": "Fallback C", "location": {"lat": 12.97, "lng": 77.61}}
ORIGIN = {"lat": 13.035, "lng": 77.597}


@pytest.fixture
def pickup(monkeypatch):
    monkeypatch.setattr(driver, "_INCIDENTS_LOCK", asyncio.Lock())
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", driver._INCIDENTS_LOCK)
    monkeypatch.setattr(driver, "_require_unit", lambda request, driver_id: None)
    monkeypatch.setattr(driver, "_attach_ranking", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(type(local_router), "available", property(lambda self: True))
    monkeypatch.setattr(local_router, "find_path", lambda *a, **k: None)  # no road to the planned hospital
    hospital_agent._on_divert.clear()
    incident = {
        "incident_id": "INC-div", "status": "dispatched", "vehicle_required": "Ambulance",
        "assigned_unit": "AMB-01", "location": ORIGIN, "approved_hospital_id": PLANNED["hospital_id"],
        "action_plan": {"all_hospitals": [PLANNED, B, C]},
        "citizen_view": {"origin": ORIGIN, "hospital_name": PLANNED["name"],
                         "hospital_location": PLANNED["location"], "phase1_eta_minutes": 4},
    }
    monkeypatch.setattr(incident_router, "ACTIVE_INCIDENTS", [incident])
    monkeypatch.setattr(driver, "_incident", lambda incident_id: incident)
    monkeypatch.setattr(driver, "commit", lambda inc: asyncio.sleep(0))
    yield incident
    hospital_agent._on_divert.clear()


def _route_factory(calls, on_route):
    async def _route(origin, destination):
        calls.append((round(destination["lat"], 4), round(destination["lng"], 4)))
        if (destination["lat"], destination["lng"]) == (PLANNED["location"]["lat"], PLANNED["location"]["lng"]):
            raise HTTPException(status_code=503, detail="no road")
        on_route(destination)
        return {"route_coordinates": [origin, destination], "eta_minutes": 5,
                "distance_meters": 1000, "route_degraded": False}
    return _route


def _pick_up(incident_id="INC-div"):
    payload = driver.ClaimPayload(incident_id=incident_id, driver_id="AMB-01", unit_type="primary")
    return asyncio.run(driver.arrived_pickup(payload, request=None))


def test_a_fallback_that_goes_on_divert_while_routing_is_skipped_for_the_next(pickup, monkeypatch):
    calls = []

    def divert_b_while_routing(destination):
        if destination == B["location"]:
            hospital_agent._on_divert.add(B["hospital_id"])  # B goes on divert during its route

    monkeypatch.setattr(driver, "_route", _route_factory(calls, divert_b_while_routing))
    result = _pick_up()
    assert result["status"] == "patient_picked_up"
    assert pickup["approved_hospital_id"] == C["hospital_id"]
    assert pickup["citizen_view"]["hospital_location"] == C["location"]
    assert (round(B["location"]["lat"], 4), round(B["location"]["lng"], 4)) in calls


def test_a_fallback_already_on_divert_is_not_routed_to(pickup, monkeypatch):
    calls = []
    hospital_agent._on_divert.add(B["hospital_id"])  # diverted after the planned route was checked
    monkeypatch.setattr(driver, "_route", _route_factory(calls, lambda destination: None))
    result = _pick_up()
    assert result["status"] == "patient_picked_up"
    assert pickup["approved_hospital_id"] == C["hospital_id"]
    assert (round(B["location"]["lat"], 4), round(B["location"]["lng"], 4)) not in calls
