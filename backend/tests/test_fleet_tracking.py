"""Fleet, live positions and the simulator (objective 1 of the brief)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts
import fleet
import staff
from agents import simulator
from main import app

PINS = {
    "dispatch": {"name": "Dispatch", "staff_pin": "111111"},
    "units": [
        {"unit_id": "AMB-01", "type": "Ambulance", "station": "Aster CMI Emergency Ambulance Hub",
         "crew": "Crew A", "phone": "+91 90000 00002", "staff_pin": "222222"},
        {"unit_id": "AMB-02", "type": "Ambulance", "station": "Manipal Ambulance Base Depot",
         "crew": "Crew B", "phone": "+91 90000 00003", "staff_pin": "333333"},
        {"unit_id": "POLICE-01", "type": "Police Cruiser", "station": "Hebbal Police Patrol Base",
         "staff_pin": "444444"},
    ],
}
ORIGIN = {"Origin": "http://testserver"}
HEBBAL = {"lat": 13.035, "lng": 77.597}


@pytest.fixture
def with_pins(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret")
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(PINS))
    contacts.reload()
    staff.reset_throttle()
    fleet.load()


def signed_in(role, subject, pin):
    client = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    assert client.post("/api/staff/session", json={"role": role, "subject": subject, "pin": pin}).status_code == 200
    return client


def test_units_come_from_the_directory_parked_at_their_stations():
    units = {u["unit_id"]: u for u in fleet.units()}
    assert {"AMB-01", "FIRE-01", "POLICE-01"} <= set(units)
    for u in units.values():
        assert u["status"] == "available" and u["position"]["source"] == "station"
        assert (u["position"]["lat"], u["position"]["lng"]) == (u["station_location"]["lat"], u["station_location"]["lng"])


def test_assignment_prefers_the_planned_station_then_the_nearest_free_unit():
    first = fleet.assign("INC-1", "Ambulance", HEBBAL, prefer_station="Manipal Ambulance Base Depot")
    assert first["unit_id"] == "AMB-02"
    second = fleet.assign("INC-2", "Ambulance", HEBBAL)
    assert second["unit_id"] == "AMB-01"  # nearest free ambulance to Hebbal
    assert fleet.assign("INC-1", "Ambulance", HEBBAL)["unit_id"] == "AMB-02"  # idempotent
    fleet.release("INC-1")
    assert fleet.get("AMB-02")["status"] == "available"


def test_assignment_survives_a_restart():
    fleet.assign("INC-1", "Ambulance", HEBBAL)
    fleet._units.clear()
    fleet._loaded = False
    assert fleet.unit_for_incident("INC-1")["status"] == "assigned"


def test_no_free_unit_of_that_type_returns_none():
    assert fleet.assign("INC-1", "Disaster Rescue", HEBBAL)["unit_id"] == "RESCUE-01"
    assert fleet.assign("INC-2", "Disaster Rescue", HEBBAL) is None


def test_only_the_units_own_crew_can_post_its_position(with_pins):
    fix = {"lat": 13.04, "lng": 77.59, "accuracy_m": 12}
    anonymous = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    assert anonymous.post("/api/units/AMB-01/position", json=fix).status_code == 401
    assert signed_in("unit", "AMB-02", "333333").post("/api/units/AMB-01/position", json=fix).status_code == 403
    assert signed_in("dispatcher", "dispatch", "111111").post("/api/units/AMB-01/position", json=fix).status_code == 403
    own = signed_in("unit", "AMB-01", "222222")
    stored = own.post("/api/units/AMB-01/position", json=fix)
    assert stored.status_code == 200 and stored.json()["position"]["source"] == "gps"
    assert fleet.get("AMB-01")["position"]["accuracy_m"] == 12


def test_bad_fixes_are_refused(with_pins):
    own = signed_in("unit", "AMB-01", "222222")
    assert own.post("/api/units/AMB-01/position", json={"lat": 28.6, "lng": 77.2}).status_code == 422  # Delhi
    assert own.post("/api/units/AMB-01/position", content='{"lat": NaN, "lng": 77.59}',
                    headers={"Content-Type": "application/json", **ORIGIN}).status_code == 422
    assert own.post("/api/units/AMB-01/position", json={"lat": 13.04, "lng": 77.59, "accuracy_m": -1}).status_code == 422
    assert own.post("/api/units/AMB-01/position", json={"lat": 13.04, "lng": 77.59}).status_code == 200
    assert own.post("/api/units/AMB-01/position", json={"lat": 13.04, "lng": 77.59}).status_code == 429


def test_the_fleet_list_is_for_dispatch_and_holds_no_crew_details(with_pins):
    assert signed_in("unit", "AMB-01", "222222").get("/api/units").status_code == 403
    listing = signed_in("dispatcher", "dispatch", "111111").get("/api/units")
    assert listing.status_code == 200
    text = listing.text
    assert "+91" not in text and "Crew A" not in text and "222222" not in text


def test_the_simulator_moves_an_assigned_unit_along_its_leg_and_labels_it():
    fleet.assign("INC-1", "Ambulance", HEBBAL, prefer_station="Aster CMI Emergency Ambulance Hub")
    route = [{"lat": 13.0473, "lng": 77.5908}, {"lat": 13.0410, "lng": 77.5940}, HEBBAL]
    incident = {"incident_id": "INC-1", "status": "dispatched",
                "citizen_view": {"phase1_route_coordinates": route, "phase1_eta_minutes": 5}}
    start = fleet.get("AMB-01")["position"]
    assert simulator.step([incident]) == 1
    moved = fleet.get("AMB-01")["position"]
    assert moved["source"] == "simulated"
    assert (moved["lat"], moved["lng"]) != (start["lat"], start["lng"])
    for _ in range(100):
        simulator.step([incident])
    end = fleet.get("AMB-01")["position"]
    assert abs(end["lat"] - HEBBAL["lat"]) < 1e-6 and abs(end["lng"] - HEBBAL["lng"]) < 1e-6  # stops at the scene
    assert len(fleet.trail("AMB-01")) >= 2


def test_a_phone_sending_gps_overrides_the_simulator():
    fleet.assign("INC-1", "Ambulance", HEBBAL, prefer_station="Aster CMI Emergency Ambulance Hub")
    fleet.record_position("AMB-01", 13.0400, 77.5950, source="gps", accuracy_m=8)
    incident = {"incident_id": "INC-1", "status": "dispatched",
                "citizen_view": {"phase1_route_coordinates": [{"lat": 13.0473, "lng": 77.5908}, HEBBAL],
                                 "phase1_eta_minutes": 5}}
    assert simulator.step([incident]) == 0
    assert fleet.get("AMB-01")["position"]["source"] == "gps"


def test_track_shows_the_unit_and_plan_without_crew_details():
    from routers import incident as incident_router

    incident_router.ACTIVE_INCIDENTS.append({
        "incident_id": "INC-track", "status": "dispatched", "location": HEBBAL,
        "citizen_view": {"phase1_route_coordinates": [HEBBAL], "route_coordinates": []},
    })
    try:
        fleet.assign("INC-track", "Ambulance", HEBBAL)
        body = TestClient(app).get("/api/incidents/INC-track/track").json()
        assert body["unit"]["unit_id"] == "AMB-01"
        assert body["planned"]["to_scene"] == [HEBBAL]
        assert "crew" not in body["unit"] and "phone" not in json.dumps(body)
        assert TestClient(app).get("/api/incidents/INC-missing/track").status_code == 404
    finally:
        incident_router.ACTIVE_INCIDENTS[:] = [i for i in incident_router.ACTIVE_INCIDENTS
                                               if i["incident_id"] != "INC-track"]


def test_a_unit_away_from_the_leg_start_drives_from_where_it_is():
    fleet.assign("INC-1", "Ambulance", HEBBAL, prefer_station="Aster CMI Emergency Ambulance Hub")
    fleet.record_position("AMB-01", 13.0300, 77.6000, source="simulated")  # 2 km from the leg start
    route = [{"lat": 13.0473, "lng": 77.5908}, HEBBAL]
    incident = {"incident_id": "INC-1", "status": "dispatched",
                "citizen_view": {"phase1_route_coordinates": route, "phase1_eta_minutes": 5}}
    simulator.step([incident], dt_s=0.1)
    pos = fleet.get("AMB-01")["position"]
    assert abs(pos["lat"] - 13.0300) < 0.002 and abs(pos["lng"] - 77.6000) < 0.002  # no jump back
