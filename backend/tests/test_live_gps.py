"""Real vehicles on real, noisy GPS: what the live-vehicle reviews of
25 Nov 2026 reproduced (false deviations, invented movement, stale fixes)."""

from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
from agents import monitor, simulator
from tests.test_fleet_tracking import signed_in, with_pins  # noqa: F401  (fixture)
from tests.test_monitoring_agent import ASTER_HUB, call_to, city  # noqa: F401  (fixture)


def _jitter(point: dict, metres: float, rng: random.Random) -> dict:
    angle = rng.uniform(0, 2 * math.pi)
    d = abs(rng.gauss(0, metres))
    return {"lat": point["lat"] + d * math.cos(angle) / 111_320,
            "lng": point["lng"] + d * math.sin(angle) / (111_320 * math.cos(math.radians(point["lat"])))}


def test_a_fix_is_not_matched_to_a_later_pass_of_a_route_that_comes_back_on_itself():
    # 2 km east, then back west 38 m to the north (a U-turn on a divided road).
    out = [{"lat": 13.0, "lng": 77.0 + i * 0.002} for i in range(10)]
    back = [{"lat": 13.00034, "lng": 77.018 - i * 0.002} for i in range(10)]
    route = out + back
    near_start_but_closer_to_return = {"lat": 13.00025, "lng": 77.003}  # 28 m from out, 10 m from back
    _off, unbounded = monitor.project(route, near_start_but_closer_to_return, 0)
    assert unbounded >= 10  # the old match jumped ~1.7 km ahead
    # The fix is ~324 m along the route, so the reach must cover it (a real unit
    # is matched from its last matched place, which this test does not model).
    off, index = monitor.project_near(route, near_start_but_closer_to_return, 0, 400.0)
    assert index <= 2 and off < 40


def test_a_real_drive_with_ordinary_gps_noise_raises_no_alert(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    route = incident["citizen_view"]["phase1_route_coordinates"]
    total = simulator.length(route)
    rng = random.Random(7)
    now = time.time()
    t, done, alerts = now, 0.0, []
    while done < total:
        point, _h, _end = simulator.point_along(route, done)
        fix = _jitter(point, 20, rng)
        # Now and then a multipath fix: 110 m out while claiming 15 m.
        if rng.random() < 0.05:
            fix = _jitter(point, 110, rng)
        fleet.record_position("AMB-01", fix["lat"], fix["lng"], source="gps",
                              accuracy_m=15, at=t, captured_at=t)
        alerts += monitor.tick([incident])
        t += 2.0
        done += 9.0 * 2.0  # about 32 km/h
    assert [a for _i, a in alerts if a["trigger"] == "deviation"] == []


def test_a_gps_gap_neither_moves_the_vehicle_nor_raises_a_false_alert(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    route = incident["citizen_view"]["phase1_route_coordinates"]
    now = time.time()
    first, _h, _e = simulator.point_along(route, 200)
    fleet.record_position("AMB-01", first["lat"], first["lng"], source="gps", accuracy_m=8, at=now, captured_at=now)
    assert monitor.tick([incident]) == []
    # 90 s with no fix (a tunnel): nothing moves it.
    for _ in range(5):
        simulator.step([incident])
    held = fleet.get("AMB-01")["position"]
    assert (held["lat"], held["lng"]) == (first["lat"], first["lng"])
    # The phone comes back 900 m further on: on its route, no alert.
    later = now + 90
    for k in range(4):
        p, _h, _e = simulator.point_along(route, 1100 + 20 * k)
        fleet.record_position("AMB-01", p["lat"], p["lng"], source="gps", accuracy_m=10,
                              at=later + 2 * k, captured_at=later + 2 * k)
        assert monitor.tick([incident]) == []
    assert monitor._last_off["AMB-01"] < 30


def test_a_genuine_wrong_turn_on_real_gps_is_still_caught(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    route = incident["citizen_view"]["phase1_route_coordinates"]
    now = time.time()
    p, _h, _e = simulator.point_along(route, 300)
    fleet.record_position("AMB-01", p["lat"], p["lng"], source="gps", accuracy_m=8, at=now, captured_at=now)
    monitor.tick([incident])
    raised = []
    for k in range(1, 6):  # driving away from the route, 400 m and more
        fleet.record_position("AMB-01", p["lat"] + 0.0036 + 0.0005 * k, p["lng"], source="gps", accuracy_m=8,
                              at=now + 2 * k, captured_at=now + 2 * k)
        raised += monitor.tick([incident])
    assert [a["trigger"] for _i, a in raised] == ["deviation"]


def test_out_of_order_and_old_fixes_are_not_used():
    fleet.load()
    fleet.reset()
    now = time.time()
    fleet.record_position("AMB-01", 13.04, 77.59, source="gps", accuracy_m=8, at=now, captured_at=now)
    with pytest.raises(fleet.StaleFix):  # taken before the one already stored
        fleet.record_position("AMB-01", 13.05, 77.59, source="gps", at=now + 3, captured_at=now - 1)
    with pytest.raises(fleet.StaleFix):  # held in a queue for five minutes
        fleet.record_position("AMB-01", 13.05, 77.59, source="gps", at=now + 6, captured_at=now - 300)
    stored = fleet.record_position("AMB-01", 13.05, 77.59, source="gps", at=now + 9, captured_at=now + 600)
    assert stored["captured_at"] == now + 9  # a phone clock ahead is clamped to now
    assert fleet.get("AMB-01")["position"]["lat"] == 13.05


def test_the_phone_screen_sends_its_capture_time_and_a_stale_fix_is_ignored(with_pins):
    own = signed_in("unit", "AMB-01", "222222")
    now_ms = time.time() * 1000
    first = own.post("/api/units/AMB-01/position",
                     json={"lat": 13.04, "lng": 77.59, "accuracy_m": 9, "captured_at_ms": now_ms, "speed_mps": 8.5})
    assert first.status_code == 200 and first.json()["position"]["speed_mps"] == 8.5
    time.sleep(1.05)
    late = own.post("/api/units/AMB-01/position",
                    json={"lat": 13.05, "lng": 77.59, "captured_at_ms": now_ms - 2000})
    assert late.status_code == 200 and late.json()["ignored"] == "older than the last fix"
    assert fleet.get("AMB-01")["position"]["lat"] == 13.04


def test_a_long_run_of_inaccurate_fixes_never_walks_progress_back(city):
    # An urban canyon: a minute of +-80 m fixes while the vehicle drives on.
    # Progress must not creep backwards, and no false alert follows.
    incident = call_to((12.985, 77.594), ASTER_HUB)
    route = incident["citizen_view"]["phase1_route_coordinates"]
    total = simulator.length(route)
    t, done, alerts, highest = time.time(), 0.0, [], 0
    while done < total:
        point, _h, _e = simulator.point_along(route, done)
        accuracy = 80 if 600 <= done < 600 + 24 * 30 else 10
        fleet.record_position("AMB-01", point["lat"], point["lng"], source="gps",
                              accuracy_m=accuracy, at=t, captured_at=t)
        alerts += monitor.tick([incident])
        watch = monitor._watches["AMB-01"]
        assert watch.progress >= 0
        cum = monitor._cumulative(route)
        assert cum[watch.progress] >= cum[highest] - monitor.BACK_WINDOW_M
        highest = max(highest, watch.progress)
        t += 2.0
        done += 24.0
    assert [a for _i, a in alerts if a["trigger"] == "deviation"] == []


def test_a_real_vehicle_is_still_real_and_in_place_after_a_restart(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    p = incident["citizen_view"]["phase1_route_coordinates"][4]
    fleet.record_position("AMB-01", p["lat"], p["lng"], source="gps", accuracy_m=6)
    fleet._units.clear()
    fleet._loaded = False
    unit = fleet.get("AMB-01")
    assert fleet.real_vehicle(unit)
    assert (unit["position"]["lat"], unit["position"]["source"]) == (p["lat"], "gps")


def test_a_real_vehicle_freed_by_reconcile_stays_where_it_is(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    p = incident["citizen_view"]["phase1_route_coordinates"][4]
    fleet.record_position("AMB-01", p["lat"], p["lng"], source="gps", accuracy_m=6)
    assert fleet.reconcile(set()) == ["AMB-01"]
    assert fleet.get("AMB-01")["position"]["lat"] == p["lat"]


def test_the_first_match_takes_the_earliest_pass_of_a_route_that_comes_back():
    out = [{"lat": 13.0, "lng": 77.0 + i * 0.002} for i in range(10)]
    back = [{"lat": 13.00034, "lng": 77.018 - i * 0.002} for i in range(10)]
    near_both = {"lat": 13.00025, "lng": 77.003}  # 28 m from out, 10 m from back
    off, index = monitor.first_on_route(out + back, near_both, monitor.OFF_ROUTE_M)
    assert index <= 2 and off < 40
