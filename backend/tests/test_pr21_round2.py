"""Fixes from CodeRabbit's second review of the finals PR (#21)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
import storage
from agents import monitor, simulator
from agents.city_state import city_state
from routers import chat, incident
from tests.test_monitoring_agent import ASTER_HUB, call_to, city, drive  # noqa: F401  (fixture)
from tests.test_workflow_regressions import client, seed  # noqa: F401  (fixture)


def test_a_phone_tracked_from_dispatch_then_quiet_keeps_going_forward(city):
    incident_ = call_to((12.985, 77.594), ASTER_HUB)
    route = incident_["citizen_view"]["phase1_route_coordinates"]
    far = route[len(route) // 2]
    # Never simulated: the phone reported all along, then stopped.
    simulator._progress.pop("AMB-01", None)
    fleet.record_position("AMB-01", far["lat"], far["lng"], source="gps", accuracy_m=5)
    fleet._units["AMB-01"]["position"]["at"] = time.time() - 3600
    goal = route[-1]
    for _ in range(3):
        simulator.step([incident_])
    here = fleet.get("AMB-01")["position"]
    assert monitor._dist(here, goal) < monitor._dist(far, goal)


def test_a_closure_just_past_the_tick_still_stops_the_unit_in_time(city, monkeypatch):
    # Two segments: 0-500 m, then 500-1000 m; the closure starts at 505 m.
    route = [{"lat": 13.0, "lng": 77.0}, {"lat": 13.0, "lng": 77.00463}, {"lat": 13.0, "lng": 77.00926}]
    closure = [{"lat": 13.0, "lng": 77.00468}, {"lat": 13.0, "lng": 77.0050}]
    monkeypatch.setattr(city_state, "closure_overlay", lambda: [{"coordinates": closure}])
    monkeypatch.setattr(city_state, "closed_edges", {1})
    stop = simulator._closure_on(route, 450.0, 495.0)  # the tick ends before segment 2 begins
    assert stop is not None and stop <= 495.0


def test_a_reroute_restarts_simulated_progress_on_the_new_route(city):
    incident_ = call_to((12.985, 77.594), ASTER_HUB)
    drive(incident_, 4)
    assert simulator._progress["AMB-01"][1] > 0
    first_key = simulator._progress["AMB-01"][0]
    # Dispatch accepts a different route to the same scene from here.
    here = fleet.get("AMB-01")["position"]
    from agents.local_router import local_router
    new = local_router.find_path((here["lat"], here["lng"]), (12.985, 77.594), penalty={
        e: 50.0 for e in local_router.find_path((here["lat"], here["lng"]), (12.985, 77.594))["edge_ids"][:5]})
    incident_["citizen_view"]["phase1_route_coordinates"] = new["coordinates"]
    simulator.step([incident_])
    key, travelled, _start = simulator._progress["AMB-01"]
    assert key != first_key
    assert monitor.project(new["coordinates"], fleet.get("AMB-01")["position"])[0] < 30
    assert travelled < simulator.length(new["coordinates"]) / 2


def test_an_alert_for_a_destination_that_changed_is_not_published(client):
    report = seed("patient_picked_up")
    report["citizen_view"]["route_coordinates"] = [{"lat": 13.0, "lng": 77.0}, {"lat": 13.04, "lng": 77.59}]
    stale = {"alert_id": "S1", "status": "open", "leg": "to_hospital", "unit_id": "AMB-01",
             "destination": {"lat": 12.9, "lng": 77.5}, "explanation": "x"}
    asyncio.run(simulator.store_alerts([("INC-review", stale)]))
    assert not (report.get("monitoring") or {}).get("alerts")
    good = dict(stale, alert_id="S2", destination={"lat": 13.04, "lng": 77.59})
    asyncio.run(simulator.store_alerts([("INC-review", good)]))
    assert report["monitoring"]["alerts"][0]["alert_id"] == "S2"


def test_a_history_read_across_a_clear_neither_caches_nor_returns_purged_rows(client, monkeypatch):
    seed("dispatched")
    storage.append_chat("INC-review", [{"id": "m1", "message": "before purge"}], database=incident.persistence_path())
    real = storage.load_chat_migrating

    def read_then_cleared(*a, **k):
        rows = real(*a, **k)
        chat.manager.clear_history()  # a purge lands while this read is in flight
        return rows

    monkeypatch.setattr(storage, "load_chat_migrating", read_then_cleared)
    assert asyncio.run(chat.manager.load("INC-review")) == []
    assert "INC-review" not in chat.manager.chat_history


def test_a_socket_message_that_was_not_stored_closes_the_channel(client, monkeypatch):
    seed("dispatched")

    async def refused(*a, **k):
        return False

    monkeypatch.setattr(chat.manager, "broadcast", refused)
    with client.websocket_connect("/ws/chat/INC-review") as ws:
        ws.send_text('{"message": "hello", "sender_role": "citizen"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 1008


def test_startup_frees_a_unit_left_on_a_call_still_awaiting_approval(client, monkeypatch):
    asyncio.run(incident.commit(seed()))  # awaiting approval, stored
    fleet.assign("INC-review", "Ambulance", near={"lat": 13.035, "lng": 77.597})
    assert fleet.unit_for_incident("INC-review") is not None
    from main import app
    with TestClient(app):  # startup: load and reconcile
        assert fleet.unit_for_incident("INC-review") is None
