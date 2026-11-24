"""Sprint fixes (23-27 Nov): the demo must run end to end on stage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    # The road graph confirms there is no way to the planned hospital.
    monkeypatch.setattr(local_router, "find_path", lambda *a, **k: None)
    monkeypatch.setattr(type(local_router), "available", property(lambda self: True))
    r = TestClient(app).post("/api/driver/arrived_pickup",
                             json={"incident_id": "INC-review", "driver_id": report["assigned_unit"]})
    assert r.status_code == 200, r.text
    assert report["citizen_view"]["hospital_name"] == "Second Choice"
    assert report["approved_hospital_id"] == "blr-001"
    assert "No road route found to" in report["citizen_view"]["message"]
    from agents import monitor as _monitor
    assert any("Second Choice" in e["text"] for e in _monitor.decision_log)
    incident_router.ACTIVE_INCIDENTS.clear()


def test_a_passing_routing_error_does_not_send_the_patient_elsewhere(monkeypatch):
    import asyncio as _asyncio
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from main import app
    from routers import driver, incident as incident_router
    from tests.test_workflow_regressions import seed

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", _asyncio.Lock())
    incident_router.ACTIVE_INCIDENTS.clear()
    report = seed("dispatched")
    report["action_plan"]["all_hospitals"].append(
        {"hospital_id": "blr-001", "name": "Second Choice", "location": {"lat": 12.97, "lng": 77.6}})
    fleet.assign("INC-review", "Ambulance", near=report["location"])
    report["assigned_unit"] = fleet.unit_for_incident("INC-review")["unit_id"]

    async def flaky(origin, destination):
        raise HTTPException(status_code=503, detail="Routing is unavailable; assignment has not changed")

    monkeypatch.setattr(driver, "_route", flaky)
    # The graph does have a road there: a provider error, not a closure.
    monkeypatch.setattr(local_router, "find_path", lambda *a, **k: {"edge_ids": [1]})
    monkeypatch.setattr(type(local_router), "available", property(lambda self: True))
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/driver/arrived_pickup", json={"incident_id": "INC-review", "driver_id": report["assigned_unit"]})
    assert r.status_code == 503
    assert report["citizen_view"]["hospital_name"] == "Aster CMI Hospital (Hebbal)"
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


def test_delays_are_worded_honestly_in_both_directions():
    assert monitor._delay_line(204, 680).startswith("Delay: +3.4 min")
    faster = monitor._delay_line(-210, 500)
    assert "No delay" in faster and "3.5 min faster" in faster and "-" not in faster.split(";")[0]
    assert monitor._delay_line(10, 500).startswith("No delay against the plan")


def test_unnamed_roads_are_described_not_named_by_class():
    assert monitor.readable_road("Trunk Link") == "an unnamed slip road"
    assert monitor.readable_road("Tertiary") == "an unnamed side road"
    assert monitor.readable_road("Primary") == "an unnamed main road"
    assert monitor.readable_road("Outer Ring Road") == "Outer Ring Road"
    assert monitor.readable_road("NH44") == "NH44"
    assert monitor.main_road([{"road": "Trunk"}, {"road": "Trunk"}, {"road": "Bellary Road"}]) == "Bellary Road"


def test_the_closure_alert_reads_without_road_class_words(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    city_state.close_road(monitoring.edges_to_close(route, index, unit["position"]), "closure")
    alert = monitor.tick([incident])[0][1]
    import re
    bare = re.compile(r"\b(on|via|,) (Motorway|Trunk|Primary|Secondary|Tertiary)( Link)?\b")
    assert not bare.search(alert["explanation"]), alert["explanation"]
    for junction in alert["corridor"]:
        assert not re.fullmatch(r"(Motorway|Trunk|Primary|Secondary|Tertiary)( Link)?", junction["road"])
    assert "1 junctions" not in alert["explanation"]


def test_the_city_clock_is_bengaluru_time_on_a_utc_server(monkeypatch):
    import os
    import time as _time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from agents.city_state import city_hour

    monkeypatch.setenv("TZ", "UTC")
    _time.tzset()
    try:
        assert city_hour() == datetime.now(ZoneInfo("Asia/Kolkata")).hour
    finally:
        monkeypatch.delenv("TZ", raising=False)
        _time.tzset()
    assert os.environ.get("TZ") != "UTC"


def test_reset_sets_the_city_to_bengaluru_time(monkeypatch):
    import asyncio as _asyncio
    from agents import city_state as cs_module
    from routers import incident as incident_router

    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", _asyncio.Lock())
    monkeypatch.setattr(cs_module, "city_hour", lambda: 14)
    city_state.set_hour(3)
    _asyncio.run(monitoring.reset_event_state())
    assert city_state.hour == 14


def test_the_accepted_route_is_named_plainly(monkeypatch):
    import asyncio as _asyncio
    from fastapi.testclient import TestClient
    from main import app
    from routers import incident as incident_router
    from tests.test_workflow_regressions import seed

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", _asyncio.Lock())
    incident_router.ACTIVE_INCIDENTS.clear()
    monitor.reset()
    report = seed("dispatched")
    route = {"label": "Recommended", "coordinates": [{"lat": 13.0, "lng": 77.6}, {"lat": 13.035, "lng": 77.597}],
             "eta_s": 300.0, "distance_m": 2000.0, "roads": []}
    report["monitoring"] = {"alerts": [{"alert_id": "R1", "status": "open", "leg": "to_scene",
                                        "unit_id": "AMB-01", "routes": [route]}]}
    assert TestClient(app).post("/api/incidents/INC-review/alerts/R1/accept",
                                json={"route_index": 0}).status_code == 200
    text = monitor.decision_log[0]["text"]
    assert text.startswith("Dispatcher accepted the recommended route for AMB-01"), text
    incident_router.ACTIVE_INCIDENTS.clear()


def test_the_live_remaining_time_counts_down_as_the_unit_drives(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 1)
    first = monitor.live_status(incident, fleet.unit_for_incident("INC-mon"))
    drive(incident, 5)
    later = monitor.live_status(incident, fleet.unit_for_incident("INC-mon"))
    assert first["leg"] == "to_scene" and first["provenance"] == "modelled"
    assert later["remaining_s"] < first["remaining_s"]
    assert later["remaining_m"] < first["remaining_m"]
    assert later["progress"] > first["progress"]
    assert later["off_route_m"] < 30  # the simulator drives on the route
    assert later["fix_source"] == "simulated"


def test_the_track_endpoint_carries_the_live_status(city, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 2)
    body = TestClient(app).get("/api/incidents/INC-mon/track").json()
    assert body["live"]["leg"] == "to_scene" and body["live"]["remaining_s"] > 0


def test_each_alert_carries_the_agents_steps(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    city_state.close_road(monitoring.edges_to_close(route, index, unit["position"]), "closure")
    alert = monitor.tick([incident])[0][1]
    steps = [s["step"] for s in alert["trace"]]
    assert steps == ["Observe", "Detect", "Explain", "Predict", "Options", "Advise", "Total"]
    text = {s["step"]: s["text"] for s in alert["trace"]}
    assert "AMB-01" in text["Observe"] and "closure or accident" in text["Detect"]
    assert alert["cause"]["text"] in text["Explain"]
    assert "Recommended" in text["Options"]
    assert alert["trace"][-1]["ms"] > 0


def _stage(incident, kind="close"):
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    edges = monitoring.edges_to_close(route, index, unit["position"])
    if kind == "close":
        city_state.close_road(edges, "closure")
    else:
        city_state.report_incident(edges, label="accident")
    return monitor.tick([incident])[0][1]


def test_the_agent_keeps_the_unit_when_a_backup_would_not_help(city):
    # The guided demo's scene near Hebbal: AMB-01 (Aster CMI hub) is close,
    # every other ambulance is across town.
    alert = _stage(call_to(GUIDED_SCENE, ASTER_HUB))
    assert alert["backup"] is not None  # a free unit exists ...
    assert alert["recommendation"] == "reroute"  # ... but is not worth sending
    assert "Backup: not needed" in alert["explanation"]
    assert "Keep AMB-01 on the new route" in alert["explanation"]
    advise = next(s["text"] for s in alert["trace"] if s["step"] == "Advise")
    assert advise.startswith("Keep this unit on the recommended route")


def test_an_accident_ahead_is_named_as_the_cause(city):
    alert = _stage(call_to((12.985, 77.594), ASTER_HUB), kind="accident")
    assert alert["trigger"] == "blocked_ahead"
    assert alert["cause"]["kind"] == "accident" and alert["cause"]["likely"] is False
    assert alert["cause"]["text"].startswith("accident reported on ")
    assert "Cause: accident reported on" in alert["explanation"]


def test_a_closure_delay_is_positive_and_adds_up(city):
    alert = _stage(call_to((12.985, 77.594), ASTER_HUB))
    assert alert["delay_s"] > 0
    assert abs(alert["new_eta_s"] - (alert["planned_remaining_s"] + alert["delay_s"])) < 1.0
    assert "Delay: +" in alert["explanation"]


def test_a_fire_engine_stays_at_the_scene_after_the_backup_ambulance_picks_up(city):
    from agents import simulator
    incident = call_to((12.985, 77.594), ASTER_HUB)  # assigns AMB-01 as the fleet unit
    incident["vehicle_required"] = "Fire Engine"
    hospital_leg = local_router.find_path((12.985, 77.594), (13.0473, 77.5908))
    incident.update(status="patient_picked_up")
    incident["citizen_view"].update(route_coordinates=hospital_leg["coordinates"], eta_minutes=10)
    before = dict(fleet.get("AMB-01")["position"])
    simulator.step([incident])
    assert simulator.fleet_leg(incident)[0] is None
    assert fleet.get("AMB-01")["position"]["lat"] == before["lat"]  # not driven to hospital
    assert monitor.tick([incident]) == [] and monitor.live_status(incident, fleet.get("AMB-01")) is None


def test_an_unreadable_fleet_state_hands_out_no_units_and_retries(monkeypatch):
    import storage as _storage

    calls = {"n": 0}
    real = _storage.load_collection

    def failing(name, *a, **k):
        if name == fleet._COLLECTION:
            calls["n"] += 1
            raise OSError("disk unreadable")
        return real(name, *a, **k)

    monkeypatch.setattr(_storage, "load_collection", failing)
    monkeypatch.setattr(fleet, "_loaded", False)
    monkeypatch.setattr(fleet, "_units", {})
    monkeypatch.setattr(fleet.time, "sleep", lambda s: None)
    fleet.load()  # startup: three tries
    assert fleet._loaded is False and calls["n"] == 3
    # Later requests retry once, at most every RELOAD_EVERY_S, and get no unit.
    monkeypatch.setattr(fleet, "_load_failed_at", fleet._load_failed_at - fleet.RELOAD_EVERY_S)
    assert fleet.assign("INC-x", "Ambulance", near={"lat": 13.0, "lng": 77.6}) is None
    assert calls["n"] == 4
    assert fleet.assign("INC-x", "Ambulance", near={"lat": 13.0, "lng": 77.6}) is None
    assert calls["n"] == 4  # no retry storm
    # A reset while unloaded leaves the stored assignments alone.
    saved = []
    monkeypatch.setattr(_storage, "save_collection", lambda *a, **k: saved.append(a))
    fleet.reset()
    assert saved == []
    monkeypatch.undo()
    fleet._load_failed_at = None
    fleet.load()


def test_a_backup_is_routed_from_where_it_is_now(city, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from routers import incident as incident_router
    from tests.test_monitoring_agent import MANIPAL_HUB

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    scene = (MANIPAL_HUB[0] + 0.004, MANIPAL_HUB[1] + 0.003)
    incident = call_to(scene, ASTER_HUB)
    alert = _stage(incident)
    assert alert["recommendation"] == "dispatch_backup"
    incident.setdefault("monitoring", {"alerts": []})["alerts"].insert(0, alert)
    backup = alert["backup"]["unit_id"]
    # The backup moved after the alert (say, repositioned 1 km north).
    moved = {"lat": MANIPAL_HUB[0] + 0.009, "lng": MANIPAL_HUB[1]}
    fleet.record_position(backup, moved["lat"], moved["lng"], source="simulated")
    r = TestClient(app).post(f"/api/incidents/INC-mon/alerts/{alert['alert_id']}/backup")
    assert r.status_code == 200, r.text
    first = incident_router.ACTIVE_INCIDENTS[-1]["citizen_view"]["phase1_route_coordinates"][0]
    assert monitor._dist(first, moved) < 300  # the new route starts where the unit is


def test_a_delayed_fleet_load_frees_units_left_on_cleared_calls(monkeypatch):
    import storage as _storage

    fleet.load()
    fleet.assign("INC-gone", "Ambulance", near={"lat": 13.0, "lng": 77.6})
    held = fleet.unit_for_incident("INC-gone")["unit_id"]
    # The next start-up cannot read the fleet; meanwhile INC-gone is cleared.
    real = _storage.load_collection
    monkeypatch.setattr(_storage, "load_collection",
                        lambda name, *a, **k: (_ for _ in ()).throw(OSError("down")) if name == fleet._COLLECTION
                        else real(name, *a, **k))
    monkeypatch.setattr(fleet, "_loaded", False)
    monkeypatch.setattr(fleet.time, "sleep", lambda s: None)
    fleet.load()
    assert fleet._loaded is False
    monkeypatch.setattr(_storage, "load_collection", real)
    monkeypatch.setattr(fleet, "active_calls", lambda: set())
    monkeypatch.setattr(fleet, "_load_failed_at", fleet._load_failed_at - fleet.RELOAD_EVERY_S)
    assert fleet.get(held)["status"] == "available"  # freed on the delayed load
    monkeypatch.setattr(fleet, "_load_failed_at", None)


def test_a_date_next_to_a_phone_number_does_not_shield_it():
    from routers import incident as incident_router
    masked = incident_router._mask_numbers("On 12.05.2026 9876543210 call me")
    assert "9876543210" not in masked and "[number hidden]" in masked
    assert incident_router._mask_numbers("Since 12.05.2026 10.30") == "Since 12.05.2026 10.30"


def test_dates_with_colon_times_or_seconds_are_left_alone():
    from routers import incident as incident_router
    for text in ("Since 12.05.2026 10:30", "At 12.05.2026 10.30.45", "On 12-05-26 9"):
        assert incident_router._mask_numbers(text) == text, text


def test_live_status_after_a_reroute_uses_the_new_route(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 6)
    monitor.tick([incident])  # the watch now has progress on the long route
    here = fleet.get("AMB-01")["position"]
    short = [{"lat": here["lat"], "lng": here["lng"]}, {"lat": 12.985, "lng": 77.594}]
    incident["citizen_view"]["phase1_route_coordinates"] = short  # rerouted, before the next tick
    live = monitor.live_status(incident, fleet.get("AMB-01"))
    import json
    json.dumps(live, allow_nan=False)  # no infinity in the response
    assert live["off_route_m"] < 5 and live["progress"] < 0.05


@pytest.mark.parametrize("text", ["Call 98-76-5432 10", "Call 45.67.8901 23", "Call 12.05.2026 25:61",
                                  "Call 1.2.345 12.34.56", "Call 12.05.2026 10:99"])
def test_a_phone_number_shaped_like_a_date_is_still_masked(text):
    from routers import incident as incident_router
    assert "[number hidden]" in incident_router._mask_numbers(text), text


def test_real_dates_and_times_stay_readable():
    from routers import incident as incident_router
    for text in ("Since 12.05.2026 10:30", "On 05/12/2026", "At 31-12-2025 23.59.59", "At 2026-05-12 10:30"):
        assert incident_router._mask_numbers(text) == text, text


def test_only_one_disruption_is_staged_at_a_time(city, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from agents import simulator

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 2)
    client = TestClient(app)
    incident["monitoring"] = {"alerts": [{"alert_id": "O1", "status": "open", "leg": "to_scene", "unit_id": "AMB-01"}]}
    r = client.post("/api/monitoring/demo/INC-mon", json={"action": "close_road_ahead"})
    assert r.status_code == 409 and "open alert" in r.json()["detail"]
    incident["monitoring"]["alerts"][0]["status"] = "dismissed"
    simulator._detours["AMB-01"] = ("k", [], 0.0)
    r = client.post("/api/monitoring/demo/INC-mon", json={"action": "wrong_turn"})
    assert r.status_code == 409 and "wrong turn" in r.json()["detail"]


def test_a_destination_that_goes_on_divert_during_transport_is_flagged_and_switched(city, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from agents.hospital_intelligence import hospital_agent

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    scene = (13.0358, 77.597)
    first = {"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)", "location": {"lat": 13.0473, "lng": 77.5908}}
    second = {"hospital_id": "blr-001", "name": "Next Open Hospital", "location": {"lat": 13.02, "lng": 77.59}}
    leg = local_router.find_path(scene, (first["location"]["lat"], first["location"]["lng"]))
    incident = call_to(scene, ASTER_HUB)
    incident.update(status="patient_picked_up", approved_hospital_id="blr-007",
                    action_plan={"all_hospitals": [first, second]})
    incident["citizen_view"].update(route_coordinates=leg["coordinates"], eta_minutes=6,
                                    hospital_name=first["name"], hospital_location=first["location"])
    fleet.record_position("AMB-01", scene[0], scene[1], source="simulated")
    assert monitor.tick([incident]) == []
    previous = set(hospital_agent._on_divert)
    hospital_agent.set_divert("blr-007", on_divert=True)
    try:
        alerts = monitor.tick([incident])
        assert len(alerts) == 1
        alert = alerts[0][1]
        assert alert["trigger"] == "destination_divert" and alert["cause"]["kind"] == "divert"
        assert alert["new_destination"]["name"] == "Next Open Hospital" and alert["routes"]
        assert "went on divert" in alert["explanation"] and "patient is on board" in alert["explanation"]
        assert monitor.tick([incident]) == []  # raised once
        incident.setdefault("monitoring", {"alerts": []})["alerts"].insert(0, alert)
        r = TestClient(app).post(f"/api/incidents/INC-mon/alerts/{alert['alert_id']}/accept", json={"route_index": 0})
        assert r.status_code == 200, r.text
        assert incident["citizen_view"]["hospital_name"] == "Next Open Hospital"
        assert incident["approved_hospital_id"] == "blr-001"
        assert "to Next Open Hospital" in monitor.decision_log[0]["text"]
    finally:
        hospital_agent._on_divert.clear()
        hospital_agent._on_divert.update(previous)


def test_rush_hour_in_the_rain_alone_raises_a_slowing_alert(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)  # planned at 03:00, clear
    drive(incident, 2)
    assert monitor.tick([incident]) == []
    city_state.set_hour(18)
    city_state.set_weather("Rain")
    alerts = monitor.tick([incident])
    assert len(alerts) == 1, "the evening rush in the rain raised no alert"
    alert = alerts[0][1]
    assert alert["trigger"] == "slowing_ahead"
    assert alert["delay_s"] >= monitor.SLOWING_MIN_S
    assert alert["cause"]["kind"] in {"congestion", "weather"}
    assert alert["cause"]["text"] == "city-wide slowdown: evening rush hour (18:00, 71% of peak) and rain (x1.35 travel time)"
    assert "left the planned route" not in alert["explanation"]
    assert "is being slowed on its route" in alert["explanation"]
    assert monitor.tick([incident]) == []  # once, not every tick


def test_a_plan_slower_than_the_map_at_dispatch_raises_no_slowing_alert(city):
    # A plan whose ETA came from elsewhere (say, Google) and is much shorter
    # than the offline map's: no city change, so no "slowed" alert.
    incident = call_to((12.985, 77.594), ASTER_HUB)
    incident["citizen_view"]["phase1_eta_minutes"] = 2
    drive(incident, 2)
    assert monitor.tick([incident]) == []



def test_a_plan_eta_from_another_source_never_raises_a_slowing_alert(city):
    # The plan says 2 min (a provider's ETA); the map says much more. A city
    # change elsewhere must not turn that disagreement into "slowed".
    incident = call_to((12.985, 77.594), ASTER_HUB)
    incident["citizen_view"]["phase1_eta_minutes"] = 2
    drive(incident, 2)
    assert monitor.tick([incident]) == []
    city_state.close_road([1], "far away")  # an unrelated change anywhere in the city
    assert monitor.tick([incident]) == []


def test_a_dismissed_slowdown_alerts_again_only_if_it_gets_worse(city, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 2)
    assert monitor.tick([incident]) == []
    city_state.set_hour(18)
    city_state.set_weather("Rain")
    alert = monitor.tick([incident])[0][1]
    incident.setdefault("monitoring", {"alerts": []})["alerts"].insert(0, alert)
    assert TestClient(app).post(f"/api/incidents/INC-mon/alerts/{alert['alert_id']}/dismiss").status_code == 200
    monitor.tick([incident])  # re-measures from today's conditions
    city_state.close_road([1], "unrelated")  # an unrelated change, no further slowdown
    assert monitor.tick([incident]) == []


def test_a_divert_alert_that_was_not_stored_is_raised_again(city):
    from agents.hospital_intelligence import hospital_agent

    first = {"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)", "location": {"lat": 13.0473, "lng": 77.5908}}
    second = {"hospital_id": "blr-001", "name": "Next Open Hospital", "location": {"lat": 13.02, "lng": 77.59}}
    scene = (13.0358, 77.597)
    leg = local_router.find_path(scene, (first["location"]["lat"], first["location"]["lng"]))
    incident = call_to(scene, ASTER_HUB)
    incident.update(status="patient_picked_up", approved_hospital_id="blr-007", action_plan={"all_hospitals": [first, second]})
    incident["citizen_view"].update(route_coordinates=leg["coordinates"], eta_minutes=6,
                                    hospital_name=first["name"], hospital_location=first["location"])
    previous = set(hospital_agent._on_divert)
    hospital_agent.set_divert("blr-007", on_divert=True)
    try:
        lost = monitor.tick([incident])[0][1]
        monitor.forget_unstored("AMB-01", lost)  # as store_alerts does when it drops one
        again = monitor.tick([incident])
        assert len(again) == 1 and again[0][1]["trigger"] == "destination_divert"
    finally:
        hospital_agent._on_divert.clear()
        hospital_agent._on_divert.update(previous)
