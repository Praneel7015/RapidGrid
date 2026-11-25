"""Fixes from CodeRabbit's review of the finals PR (#21): the monitoring
agent and the simulator."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
from agents import monitor, simulator
from agents.city_state import city_state
from routers import monitoring
from tests.test_monitoring_agent import ASTER_HUB, call_to, city, drive  # noqa: F401  (fixture)


def test_closed_road_crossing_the_route_between_points_is_on_it():
    route = [{"lat": 13.0, "lng": 77.0}, {"lat": 13.0, "lng": 77.01}]  # about 1 km east-west
    crossing = [{"lat": 12.999, "lng": 77.005}, {"lat": 13.001, "lng": 77.005}]  # 110 m each side
    assert monitor._near_route(crossing, route, 35.0, monitor._box(route, 35.0))
    far = [{"lat": 13.002, "lng": 77.005}, {"lat": 13.003, "lng": 77.005}]
    assert not monitor._near_route(far, route, 35.0, monitor._box(route, 35.0))


def test_one_gps_fix_is_counted_once_however_many_ticks_see_it(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    route = incident["citizen_view"]["phase1_route_coordinates"]
    fleet.record_position("AMB-01", route[0]["lat"], route[0]["lng"], source="gps", accuracy_m=5)
    monitor.tick([incident])
    # One accurate fix 400 m off the route, then the phone goes quiet.
    fleet.record_position("AMB-01", route[0]["lat"] + 0.0036, route[0]["lng"], source="gps", accuracy_m=5,
                          at=time.time() + 10)
    for _ in range(5):
        assert monitor.tick([incident]) == []
    assert monitor._watches["AMB-01"].off_count == 1


def test_a_reroute_with_the_same_number_of_points_starts_progress_afresh():
    a = [{"lat": 13.0, "lng": 77.0}, {"lat": 13.0, "lng": 77.01}]
    b = [{"lat": 13.0, "lng": 77.0}, {"lat": 13.01, "lng": 77.0}]
    assert simulator.route_key(a) != simulator.route_key(b)
    assert simulator.route_key(a) == simulator.route_key([dict(p) for p in a])


def test_a_second_closure_while_the_first_alert_is_open_is_still_reported(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    edges = monitoring.edges_to_close(route, index, unit["position"])
    city_state.close_road(edges, "first")
    first = monitor.tick([incident])
    assert len(first) == 1
    # The first closure is lifted and a second one appears before dispatch
    # acts; then dispatch dismisses the first alert.
    city_state.clear()
    city_state.set_hour(3)
    city_state.close_road(edges[-1:], "second")
    assert monitor.tick([incident]) == []  # one open alert per unit
    monitor.clear_alert("AMB-01")
    again = monitor.tick([incident])
    assert len(again) == 1 and again[0][1]["trigger"] == "blocked_ahead"


def test_a_dismissed_alert_is_not_raised_again_for_the_same_closure(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    city_state.close_road(monitoring.edges_to_close(route, index, unit["position"]), "only")
    assert len(monitor.tick([incident])) == 1
    monitor.clear_alert("AMB-01")
    assert monitor.tick([incident]) == []


def test_a_unit_drives_up_to_a_closure_far_along_a_long_segment(city, monkeypatch):
    route = [{"lat": 13.0, "lng": 77.0}, {"lat": 13.0, "lng": 77.0093}]  # about 1 km
    closure = [{"lat": 13.0, "lng": 77.0075}, {"lat": 13.0, "lng": 77.0079}]  # 810-850 m along
    monkeypatch.setattr(city_state, "closure_overlay", lambda: [{"coordinates": closure}])
    monkeypatch.setattr(city_state, "closed_edges", {1})
    assert simulator._closure_on(route, 0.0, 40.0) is None  # not reached this tick
    stop = simulator._closure_on(route, 0.0, 900.0)
    assert stop is not None and 780 <= stop <= 800


def test_a_real_vehicle_whose_phone_goes_quiet_is_never_simulated(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 2)
    route = incident["citizen_view"]["phase1_route_coordinates"]
    far = route[len(route) // 2]
    fleet.record_position("AMB-01", far["lat"], far["lng"], source="gps", accuracy_m=5)
    # The phone stops (a locked screen, a tunnel): the unit stays where its
    # phone last put it, and the screens say the signal is lost.
    fleet._units["AMB-01"]["position"]["at"] = time.time() - 90
    for _ in range(4):
        simulator.step([incident])
    here = fleet.get("AMB-01")["position"]
    assert (here["lat"], here["lng"], here["source"]) == (far["lat"], far["lng"], "gps")
    live = monitor.live_status(incident, fleet.get("AMB-01"))
    assert live["signal_lost"] is True and live["fix_source"] == "gps"


def test_a_real_vehicle_stays_where_it_is_when_its_call_ends(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 2)
    far = incident["citizen_view"]["phase1_route_coordinates"][5]
    fleet.record_position("AMB-01", far["lat"], far["lng"], source="gps", accuracy_m=5)
    fleet.release(incident["incident_id"])
    here = fleet.get("AMB-01")
    assert here["status"] == "available" and here.get("gps_incident") is None
    assert (here["position"]["lat"], here["position"]["source"]) == (far["lat"], "gps")


def test_a_unit_already_on_a_road_that_just_closed_stops(city, monkeypatch):
    route = [{"lat": 13.0, "lng": 77.0}, {"lat": 13.0, "lng": 77.0093}]
    closure = [{"lat": 13.0, "lng": 77.002}, {"lat": 13.0, "lng": 77.006}]  # about 215-650 m
    monkeypatch.setattr(city_state, "closure_overlay", lambda: [{"coordinates": closure}])
    monkeypatch.setattr(city_state, "closed_edges", {1})
    stop = simulator._closure_on(route, 400.0, 440.0)  # the unit is at 400 m, inside it
    assert stop is not None and stop <= 400.0
