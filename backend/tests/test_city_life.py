"""The busy city (simulated calls around the stage call), units on and off
duty, and the stations on the map."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts
import fleet
import security
import staff
from agents import city_life, simulator
from main import app
from routers import incident as incident_router
from tests.test_fleet_tracking import PINS, signed_in, with_pins  # noqa: F401  (fixture)
from tests.test_monitoring_agent import ASTER_HUB, call_to, city  # noqa: F401  (fixture)

ORIGIN = {"Origin": "http://testserver"}
HEBBAL = {"lat": 13.035, "lng": 77.597}


@pytest.fixture(autouse=True)
def fresh():
    fleet.load()
    fleet.reset()
    city_life.reset()
    yield
    city_life.reset()
    fleet.reset()


def test_the_example_fleet_has_more_than_one_unit_of_each_kind():
    by_type = {}
    for u in fleet.units():
        by_type.setdefault(u["type"], []).append(u["unit_id"])
    assert len(by_type["Ambulance"]) == 9
    assert all(len(units) >= 2 for units in by_type.values())


def test_simulated_calls_never_take_a_stage_unit():
    taken = []
    for n in range(6):
        inc = {"incident_id": f"INC-bg{n}", "location": HEBBAL, "vehicle_required": "Ambulance",
               "background": True, "citizen_view": {"phase1_hub": {"name": "Aster CMI Emergency Ambulance Hub"}}}
        unit_id, _created = incident_router._reserve_unit(inc)
        taken.append(unit_id)
    assert not set(taken) & city_life.STAGE_UNITS
    # With every other ambulance busy, the stage call still gets its own.
    stage = {"incident_id": "INC-stage", "location": HEBBAL, "vehicle_required": "Ambulance",
             "citizen_view": {"phase1_hub": {"name": "Aster CMI Emergency Ambulance Hub"}}}
    assert incident_router._reserve_unit(stage)[0] == "AMB-01"
    # And a seventh simulated call is refused rather than taking one.
    with pytest.raises(HTTPException) as refused:
        incident_router._reserve_unit({"incident_id": "INC-bg9", "location": HEBBAL, "vehicle_required": "Ambulance",
                                       "background": True, "citizen_view": {}})
    assert refused.value.status_code == 409


def test_a_simulated_crew_finishes_its_call_after_a_short_stop(city, monkeypatch):
    incident = call_to((12.9756, 77.6066), ASTER_HUB, incident_id="INC-bgpolice")
    incident.update(vehicle_required="Police Cruiser", background=True, driver_stage="pickup")
    fleet.release("INC-bgpolice")
    unit = fleet.assign("INC-bgpolice", "Police Cruiser", incident["location"], exclude=city_life.STAGE_UNITS)
    incident["assigned_unit"] = unit["unit_id"]
    end = incident["citizen_view"]["phase1_route_coordinates"][-1]
    fleet.record_position(unit["unit_id"], end["lat"], end["lng"], source="simulated")

    asyncio.run(city_life.advance())  # arrived: the stop starts
    assert incident["status"] == "dispatched"
    key = next(iter(city_life._arrived_at))
    city_life._arrived_at[key] -= city_life.DWELL_S + 1
    asyncio.run(city_life.advance())
    assert incident["status"] == "completed"
    assert fleet.get(unit["unit_id"])["status"] == "available"


def test_the_server_acts_for_the_simulated_city_only_in_process(with_pins):
    with pytest.raises(HTTPException):
        staff.require(None, "dispatcher")  # sign-in is on: nobody, no access
    with staff.system_actor("city"):
        assert staff.require(None, "dispatcher") == ("system", "city")
    with pytest.raises(HTTPException):
        staff.require(None, "dispatcher")
    # A real request never gets it, even while the city is acting.
    anonymous = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    with staff.system_actor("city"):
        assert anonymous.post("/api/units/AMB-01/duty", json={"on_duty": False}).status_code == 401


def test_in_the_open_demo_a_visitor_can_start_the_busy_city(monkeypatch):
    calls = []

    async def fake(scenario, head_start=0.0):
        calls.append(scenario)

    monkeypatch.setattr(city_life, "create_call", fake)
    visitor = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    started = visitor.post("/api/monitoring/city-life", json={"seed": 3, "background": True, "interval_s": 60})
    assert started.status_code == 200
    body = started.json()
    assert body["started"] == 3 and body["background_caller"] is True and body["interval_s"] == 60
    assert visitor.post("/api/monitoring/city-life", json={"seed": 9}).status_code == 422


def test_a_demo_reset_stops_the_background_caller():
    city_life.configure(background=True, interval_s=60)
    city_life.reset()
    assert city_life.status()["background_caller"] is False


def test_an_off_duty_unit_is_never_dispatched():
    for uid in [u["unit_id"] for u in fleet.units() if u["type"] == "Disaster Rescue"]:
        fleet.set_duty(uid, False)
    assert fleet.assign("INC-1", "Disaster Rescue", HEBBAL) is None
    fleet.set_duty("RESCUE-01", True)
    assert fleet.assign("INC-1", "Disaster Rescue", HEBBAL)["unit_id"] == "RESCUE-01"
    with pytest.raises(ValueError):  # on a call: it finishes the call first
        fleet.set_duty("RESCUE-01", False)


def test_duty_survives_a_restart():
    fleet.set_duty("AMB-04", False)
    fleet._units.clear()
    fleet._loaded = False
    assert fleet.get("AMB-04")["on_duty"] is False


def test_a_crew_changes_its_own_duty_and_dispatch_can_change_any(with_pins):
    crew = signed_in("unit", "AMB-01", "222222")
    off = crew.post("/api/units/AMB-01/duty", json={"on_duty": False})
    assert off.status_code == 200 and off.json()["on_duty"] is False
    assert crew.post("/api/units/AMB-02/duty", json={"on_duty": False}).status_code == 403
    dispatch = signed_in("dispatcher", "dispatch", "111111")
    assert dispatch.post("/api/units/AMB-02/duty", json={"on_duty": False}).status_code == 200
    listed = {u["unit_id"]: u for u in dispatch.get("/api/units").json()["units"]}
    assert listed["AMB-01"]["on_duty"] is False and listed["AMB-02"]["on_duty"] is False


def test_every_station_is_listed_for_the_map():
    stations = TestClient(app, base_url="http://testserver").get("/api/stations").json()["stations"]
    assert len(stations) == 11
    assert {s["type"] for s in stations} == {"Ambulance", "Fire Engine", "Police Cruiser", "Disaster Rescue"}
    assert all(12.8 < s["lat"] < 13.2 and 77.4 < s["lng"] < 77.8 for s in stations)


def test_a_simulated_unit_can_start_part_way_along_its_leg(city):
    incident = call_to((12.985, 77.594), ASTER_HUB, incident_id="INC-bgpart")
    unit = fleet.unit_for_incident("INC-bgpart")
    simulator.start_part_way(unit["unit_id"], incident, 0.5)
    simulator.step([incident])
    route = incident["citizen_view"]["phase1_route_coordinates"]
    here = fleet.get(unit["unit_id"])["position"]
    done = simulator.along(route, here)
    assert 0.45 * simulator.length(route) < done < 0.7 * simulator.length(route)
