"""Sprint fixes (23-27 Nov): the demo must run end to end on stage."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
from agents import monitor
from agents.city_state import city_state
from agents.local_router import local_router
from routers import monitoring
from tests.test_monitoring_agent import ASTER_HUB, call_to, city, drive  # noqa: F401  (fixture)

GUIDED_SCENE = (13.0358, 77.597)       # GuidedDemo.jsx SCENE, near Hebbal flyover
ASTER_CMI = {"lat": 13.0473, "lng": 77.5908}  # the guided demo's recommended hospital


def test_a_staged_closure_never_cuts_the_scene_off_from_the_hospital(city):
    incident = call_to(GUIDED_SCENE, ASTER_HUB)
    incident["citizen_view"]["hospital_location"] = ASTER_CMI
    drive(incident, 2)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    scene = incident["location"]
    edges = monitoring.edges_to_close(route, index, unit["position"], [(scene, ASTER_CMI)])
    assert edges, "nothing could be staged"
    city_state.close_road(edges, "demo")
    # The trip the crew makes after pickup still has a road route.
    onward = local_router.find_path((scene["lat"], scene["lng"]), (ASTER_CMI["lat"], ASTER_CMI["lng"]))
    assert onward and not set(onward["edge_ids"]) & set(edges)


def test_pickup_goes_to_the_next_reviewed_hospital_when_the_first_has_no_road(monkeypatch):
    import asyncio as _asyncio
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from main import app
    from routers import driver, incident as incident_router
    from tests.test_workflow_regressions import seed

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", _asyncio.Lock())
    incident_router.ACTIVE_INCIDENTS.clear()
    second = {"hospital_id": "blr-001", "name": "Second Choice", "location": {"lat": 12.97, "lng": 77.6}}
    report = seed("dispatched", assigned_unit="AMB-01")
    report["action_plan"]["all_hospitals"].append(second)
    fleet.assign("INC-review", "Ambulance", near=report["location"])
    report["assigned_unit"] = fleet.unit_for_incident("INC-review")["unit_id"]
    planned = report["citizen_view"]["hospital_location"]

    async def route(origin, destination):
        if destination == planned:
            raise HTTPException(status_code=503, detail="Routing is unavailable; assignment has not changed")
        return {"route_coordinates": [origin, destination], "eta_minutes": 7, "distance_meters": 3000.0,
                "route_degraded": False}

    monkeypatch.setattr(driver, "_route", route)
    r = TestClient(app).post("/api/driver/arrived_pickup",
                             json={"incident_id": "INC-review", "driver_id": report["assigned_unit"]})
    assert r.status_code == 200, r.text
    assert report["citizen_view"]["hospital_name"] == "Second Choice"
    assert report["approved_hospital_id"] == "blr-001"
    assert "No road route to" in report["citizen_view"]["message"]
    incident_router.ACTIVE_INCIDENTS.clear()


def test_accepting_the_reroute_does_not_raise_the_same_closure_again(city, monkeypatch):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    city_state.close_road(monitoring.edges_to_close(route, index, unit["position"]), "closure")
    alerts = monitor.tick([incident])
    assert len(alerts) == 1 and alerts[0][1]["routes"]
    # Dispatch accepts the recommended route (as accept_reroute does).
    monitoring._set_leg(incident, "to_scene", alerts[0][1]["routes"][0])
    monitor.clear_alert("AMB-01")
    # The new route can pass close to the closure it goes round (a parallel
    # carriageway); the agent must still not raise that closure again.
    real = monitor._disruptions_near
    monkeypatch.setattr(monitor, "_disruptions_near",
                        lambda route, within: {**real(route, within), "closed": [{"edge_id": 1}]})
    for _ in range(10):
        drive(incident, 1)
        assert monitor.tick([incident]) == []


def test_a_dismissed_wrong_turn_stays_quiet(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 2)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    from agents import simulator
    simulator.set_detour("AMB-01", incident, monitoring.wrong_turn_path(route, index, unit["position"]))
    first = []
    for _ in range(30):
        drive(incident, 1)
        first += monitor.tick([incident])
        if first:
            break
    assert first and first[0][1]["trigger"] == "deviation"
    monitor.clear_alert("AMB-01", dismissed_deviation=True)
    dismissed_at = monitor._watches["AMB-01"].quiet_off_m
    for _ in range(10):
        drive(incident, 1)
        new = monitor.tick([incident])
        # Quiet, unless the unit went much further off (a real new problem).
        assert new == [] or monitor._last_off["AMB-01"] >= dismissed_at + monitor.DISMISS_FURTHER_M
