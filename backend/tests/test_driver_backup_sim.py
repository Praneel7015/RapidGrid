"""The simulator moves the unit that holds each leg: a backup ambulance that
confirmed pickup on another unit's call drives the hospital leg (CodeRabbit,
PR 22: simulate the unit assigned to each leg)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
from agents import simulator

HOSPITAL = {"lat": 13.035, "lng": 77.597}
ROUTE = [{"lat": 13.0473, "lng": 77.5908}, {"lat": 13.0410, "lng": 77.5940}, HOSPITAL]


def _fire_call_with_backup_pickup():
    fleet.reset()
    # The call's own (fleet-linked) unit: a fire engine, which never drives the hospital leg.
    fleet.assign("INC-fire", "Ambulance", HOSPITAL, prefer_station="Aster CMI Emergency Ambulance Hub")
    fleet.record_position("AMB-01", 13.0300, 77.6000, source="simulated")
    backup = fleet.get("AMB-02")
    fleet.record_position("AMB-02", ROUTE[0]["lat"], ROUTE[0]["lng"], source="simulated")
    incident = {
        "incident_id": "INC-fire", "status": "patient_picked_up", "vehicle_required": "Fire",
        "unit_assignments": {"primary": "AMB-01", "ambulance": "AMB-02"},
        "unit_statuses": {"primary": "dispatched", "ambulance": "patient_picked_up"},
        "citizen_view": {"phase1_route_coordinates": ROUTE, "phase1_eta_minutes": 5,
                         "route_coordinates": ROUTE, "eta_minutes": 5},
    }
    assert backup is not None
    return incident


def test_the_backup_that_picked_up_the_patient_is_the_unit_driven_on_the_hospital_leg():
    incident = _fire_call_with_backup_pickup()
    before_call_unit = dict(fleet.get("AMB-01")["position"])
    start = dict(fleet.get("AMB-02")["position"])
    assert simulator.step([incident], dt_s=5.0) == 1
    moved = fleet.get("AMB-02")["position"]
    assert moved["source"] == "simulated"
    assert (moved["lat"], moved["lng"]) != (start["lat"], start["lng"])
    # The fire engine (the fleet-linked unit) is not moved on the hospital leg.
    assert fleet.get("AMB-01")["position"] == before_call_unit


def test_the_backup_stops_at_the_hospital_and_a_pending_backup_is_not_driven():
    incident = _fire_call_with_backup_pickup()
    for _ in range(200):
        simulator.step([incident])
    end = fleet.get("AMB-02")["position"]
    assert abs(end["lat"] - HOSPITAL["lat"]) < 1e-6 and abs(end["lng"] - HOSPITAL["lng"]) < 1e-6

    incident = _fire_call_with_backup_pickup()
    incident["unit_statuses"]["ambulance"] = "dispatched"  # the backup has not picked up yet
    start = dict(fleet.get("AMB-02")["position"])
    assert simulator.step([incident]) == 0
    assert fleet.get("AMB-02")["position"] == start
