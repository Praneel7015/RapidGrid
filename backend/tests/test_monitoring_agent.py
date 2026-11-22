"""The official scenario end to end on the real road graph (objectives 2-6).

An ambulance drives to a call; the road ahead is closed or it takes a wrong
turn; the agent detects it, finds the cause, predicts the delay, proposes
routes and says whether to keep the unit or send a backup.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
from agents import monitor, simulator
from agents.city_state import city_state
from agents.local_router import local_router
from main import app
from routers import incident as incident_router
from routers import monitoring

ASTER_HUB = (13.0473, 77.5908)   # AMB-01's station
MANIPAL_HUB = (12.9585, 77.6483)  # AMB-02's station


@pytest.fixture
def city(monkeypatch):
    if not local_router.available and not local_router.load():
        pytest.skip("offline road graph not available")
    city_state.attach(local_router)
    city_state.clear()
    monitor.reset()
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", asyncio.Lock())
    incident_router.ACTIVE_INCIDENTS.clear()
    yield
    city_state.clear()
    monitor.reset()
    incident_router.ACTIVE_INCIDENTS.clear()


def call_to(scene, unit_station, incident_id="INC-mon"):
    leg = local_router.find_path(unit_station, scene)
    assert leg, "no road route for the test call"
    incident = {
        "incident_id": incident_id, "status": "dispatched", "vehicle_required": "Ambulance",
        "location": {"lat": scene[0], "lng": scene[1]},
        "citizen_view": {"phase1_route_coordinates": leg["coordinates"],
                         "phase1_eta_minutes": max(1, round(leg["travel_time_s"] / 60))},
    }
    incident_router.ACTIVE_INCIDENTS.append(incident)
    station = "Aster CMI Emergency Ambulance Hub" if unit_station == ASTER_HUB else "Manipal Ambulance Base Depot"
    fleet.assign(incident_id, "Ambulance", incident["location"], prefer_station=station)
    return incident


def drive(incident, ticks=3):
    for _ in range(ticks):
        simulator.step([incident])


def test_no_alert_while_the_unit_follows_its_route(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    for _ in range(5):
        drive(incident, 1)
        assert monitor.tick([incident]) == []


def test_a_road_closed_ahead_is_detected_explained_and_rerouted(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    _off, index = monitor.project(incident["citizen_view"]["phase1_route_coordinates"], unit["position"])
    edges = monitoring.edges_to_close(incident["citizen_view"]["phase1_route_coordinates"], index, unit["position"])
    assert edges
    city_state.close_road(edges, "test closure")

    alerts = monitor.tick([incident])
    assert len(alerts) == 1
    alert = alerts[0][1]
    assert alert["trigger"] == "blocked_ahead"
    assert alert["cause"]["kind"] == "roadblock"
    assert alert["delay_s"] is not None and alert["new_eta_s"] > 0
    assert 1 <= len(alert["routes"]) <= 3
    assert not set(edges) & {e for r in monitor.alternatives(unit["position"], incident["location"]) for e in r["edge_ids"]}
    assert "Cause: road closed ahead" in alert["explanation"]
    assert alert["recommendation"] in {"reroute", "dispatch_backup"}
    assert alert["provenance"] == "modelled"
    # One open alert per unit: the next tick does not repeat it.
    assert monitor.tick([incident]) == []


def test_a_wrong_turn_is_detected_as_a_deviation(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident, 2)
    unit = fleet.unit_for_incident("INC-mon")
    pos = unit["position"]
    away = local_router.find_path((pos["lat"], pos["lng"]), (pos["lat"] + 0.004, pos["lng"] + 0.006))
    assert away
    simulator.set_detour(unit["unit_id"], incident, away["coordinates"])
    alerts = []
    for _ in range(12):
        drive(incident, 1)
        alerts += monitor.tick([incident])
        if alerts:
            break
    assert alerts, "the wrong turn was never flagged"
    alert = alerts[0][1]
    assert alert["trigger"] == "deviation"
    assert alert["cause"]["kind"] in {"unexplained", "congestion", "roadblock", "accident"}
    assert alert["explanation"].startswith("AMB-01 left its planned route")


def test_backup_is_advised_when_a_free_unit_is_much_closer(city):
    # AMB-01 drives across town from Hebbal; the scene is next to AMB-02's station.
    scene = (MANIPAL_HUB[0] + 0.004, MANIPAL_HUB[1] + 0.003)
    incident = call_to(scene, ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    city_state.close_road(monitoring.edges_to_close(route, index, unit["position"]), "test closure")
    alert = monitor.tick([incident])[0][1]
    assert alert["backup"]["unit_id"] == "AMB-02"
    assert alert["recommendation"] == "dispatch_backup"
    assert "dispatch AMB-02" in alert["explanation"]


def test_with_the_patient_on_board_the_unit_is_kept(city):
    leg = local_router.find_path((13.035, 77.597), (12.985, 77.594))
    incident = {"incident_id": "INC-mon", "status": "patient_picked_up", "vehicle_required": "Ambulance",
                "location": {"lat": 13.035, "lng": 77.597},
                "citizen_view": {"route_coordinates": leg["coordinates"], "eta_minutes": 12}}
    incident_router.ACTIVE_INCIDENTS.append(incident)
    fleet.assign("INC-mon", "Ambulance", incident["location"])
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    _off, index = monitor.project(leg["coordinates"], unit["position"])
    city_state.close_road(monitoring.edges_to_close(leg["coordinates"], index, unit["position"]), "test closure")
    alert = monitor.tick([incident])[0][1]
    assert alert["backup"] is None and alert["recommendation"] == "reroute"
    assert "patient is on board" in alert["explanation"]


def test_dispatcher_actions_reroute_or_swap_the_unit(city):
    scene = (MANIPAL_HUB[0] + 0.004, MANIPAL_HUB[1] + 0.003)
    incident = call_to(scene, ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    city_state.close_road(monitoring.edges_to_close(route, index, unit["position"]), "test closure")
    alerts = monitor.tick([incident])
    asyncio.run(simulator.store_alerts(alerts))
    alert_id = alerts[0][1]["alert_id"]
    client = TestClient(app)

    swapped = client.post(f"/api/incidents/INC-mon/alerts/{alert_id}/backup")
    assert swapped.status_code == 200, swapped.text
    assert fleet.unit_for_incident("INC-mon")["unit_id"] == "AMB-02"
    assert fleet.get("AMB-01")["status"] == "available"
    assert client.post(f"/api/incidents/INC-mon/alerts/{alert_id}/dismiss").status_code == 409  # already decided
    log = client.get("/api/monitoring/log").json()["decisions"]
    assert any("backup AMB-02" in d["text"] for d in log)


def test_accepting_a_reroute_replaces_the_leg(city):
    incident = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    city_state.close_road(monitoring.edges_to_close(route, index, unit["position"]), "test closure")
    alerts = monitor.tick([incident])
    asyncio.run(simulator.store_alerts(alerts))
    alert = alerts[0][1]
    accepted = TestClient(app).post(f"/api/incidents/INC-mon/alerts/{alert['alert_id']}/accept", json={"route_index": 0})
    assert accepted.status_code == 200, accepted.text
    assert incident["citizen_view"]["phase1_route_coordinates"] == alert["routes"][0]["coordinates"]


def test_a_unit_with_no_way_round_gets_a_backup(city):
    # Close everything ahead on the route, so no road leads to the scene.
    scene = (MANIPAL_HUB[0] + 0.004, MANIPAL_HUB[1] + 0.003)
    incident = call_to(scene, ASTER_HUB)
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    stretch = local_router.find_path((route[index + 3]["lat"], route[index + 3]["lng"]),
                                     (route[-1]["lat"], route[-1]["lng"]))
    goal = local_router.nearest_node(route[-1]["lat"], route[-1]["lng"])
    into_goal = [e for e in range(len(local_router.edge_to)) if local_router.edge_to[e] == goal]
    city_state.close_road(into_goal, "test: scene cut off")
    alert = monitor.tick([incident])[0][1]
    assert alert["routes"] == [] and alert["new_eta_s"] is None
    assert alert["recommendation"] in {"dispatch_backup", "hold"}
    assert "cannot get through" in alert["explanation"]



def test_a_simulated_unit_stops_before_a_closed_road(city):
    incident = call_to((13.000, 77.570), ASTER_HUB) if local_router.find_path(ASTER_HUB, (13.000, 77.570)) else None
    if incident is None:
        pytest.skip("no route for this check")
    drive(incident)
    unit = fleet.unit_for_incident("INC-mon")
    route = incident["citizen_view"]["phase1_route_coordinates"]
    _off, index = monitor.project(route, unit["position"])
    edges = monitoring.edges_to_close(route, index, unit["position"])
    city_state.close_road(edges, "test closure")
    closed = city_state.closure_overlay()
    for _ in range(200):
        simulator.step([incident])
    pos = fleet.unit_for_incident("INC-mon")["position"]
    nearest = min(monitor.point_segment_m(pos, *c["coordinates"])[0] for c in closed)
    assert nearest > 5, "the unit drove onto the closed road"
    end = route[-1]
    assert monitor._dist(pos, end) > 50, "the unit drove through the closure to the scene"
