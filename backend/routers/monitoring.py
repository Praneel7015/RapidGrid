"""Control-room API for the monitoring agent.

- ``GET  /api/monitoring/log``: the agent's recent decisions, newest first.
- ``POST /api/incidents/{id}/alerts/{alert_id}/accept``: follow a proposed route.
- ``POST /api/incidents/{id}/alerts/{alert_id}/backup``: send the proposed
  backup unit instead (the first unit is released back to its station).
- ``POST /api/incidents/{id}/alerts/{alert_id}/dismiss``.
- ``POST /api/monitoring/demo/{id}``: stage a disruption for a live demo:
  close the road ahead of the unit, report an accident ahead, or make the
  simulated unit take a wrong turn.

Everything here is dispatch-only: the agent proposes, a person decides.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import fleet
from agents import monitor, simulator
from agents.city_state import city_state
from agents.local_router import local_router
from staff import require as require_staff

router = APIRouter(tags=["Monitoring agent"])


class AcceptRoute(BaseModel):
    route_index: int = Field(default=0, ge=0, le=2)


class DemoAction(BaseModel):
    action: Literal["close_road_ahead", "accident_ahead", "wrong_turn"]


async def _with_alert(incident_id: str, alert_id: str):
    """(incident, alert) for an open alert, under the incident lock's rules."""
    from routers import incident as incident_router

    inc = next((i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
    if inc is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    alert = next((a for a in (inc.get("monitoring") or {}).get("alerts", []) if a["alert_id"] == alert_id), None)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert["status"] != "open":
        raise HTTPException(status_code=409, detail=f"This alert was already {alert['status']}")
    return inc, alert


def _same_leg(inc: dict, alert: dict) -> None:
    """An alert for a leg the unit has finished (say, raised on the way to the
    scene, acted on after pickup) must not change the current leg."""
    current, _route, _eta = monitor._leg(inc)
    if current != alert["leg"]:
        raise HTTPException(status_code=409, detail="This alert is for an earlier leg of the call")


def _set_leg(inc: dict, leg: str, route: dict) -> None:
    view = inc.setdefault("citizen_view", {})
    minutes = max(1, math.ceil(route["eta_s"] / 60))
    if leg == "to_scene":
        view["phase1_route_coordinates"] = route["coordinates"]
        view["phase1_eta_minutes"] = minutes
    else:
        view["route_coordinates"] = route["coordinates"]
        view["eta_minutes"] = minutes
        view["distance_meters"] = route["distance_m"]


@router.get("/api/monitoring/log")
async def decision_log(request: Request = None, limit: int = 50):
    require_staff(request, "dispatcher")
    return {"decisions": list(monitor.decision_log)[: max(1, min(limit, monitor.LOG_SIZE))]}


@router.post("/api/incidents/{incident_id}/alerts/{alert_id}/accept")
async def accept_reroute(incident_id: str, alert_id: str, choice: AcceptRoute, request: Request = None):
    who = require_staff(request, "dispatcher")
    from routers import incident as incident_router

    async with incident_router._INCIDENTS_LOCK:
        inc, alert = await _with_alert(incident_id, alert_id)
        _same_leg(inc, alert)
        if choice.route_index >= len(alert["routes"]):
            raise HTTPException(status_code=422, detail="No such route")
        route = alert["routes"][choice.route_index]
        _set_leg(inc, alert["leg"], route)
        alert.update(status="rerouted", decided_at=time.time(), decided_by=who[1], route_index=choice.route_index)
        await incident_router.commit(inc)
    monitor.clear_alert(alert["unit_id"])
    text = (f"Dispatcher accepted {route['label'].lower()} for {alert['unit_id']}; "
            f"new ETA {route['eta_s'] / 60:.1f} min.")
    monitor._log(incident_id, alert["unit_id"], "decision", text, alert_id=alert_id)
    from routers.chat import post_system_message

    await post_system_message(incident_id, text, sender="Dispatch decision")
    return {"status": "rerouted", "route": route["label"]}


@router.post("/api/incidents/{incident_id}/alerts/{alert_id}/backup")
async def dispatch_backup(incident_id: str, alert_id: str, request: Request = None):
    who = require_staff(request, "dispatcher")
    from routers import incident as incident_router

    async with incident_router._INCIDENTS_LOCK:
        inc, alert = await _with_alert(incident_id, alert_id)
        _same_leg(inc, alert)
        backup = alert.get("backup")
        if alert["leg"] != "to_scene" or not backup:
            raise HTTPException(status_code=409, detail="No backup unit was proposed for this alert")
        candidate = fleet.get(backup["unit_id"])
        if candidate is None:
            raise HTTPException(status_code=409, detail="The proposed backup unit no longer exists")
        from routers.driver import units_on_calls
        if backup["unit_id"] in units_on_calls(incident_id):
            raise HTTPException(status_code=409, detail="The proposed backup unit is on another call")
        try:
            # One step: the old unit is released only if the backup is taken.
            unit = fleet.swap(incident_id, backup["unit_id"])
        except ValueError:
            raise HTTPException(status_code=409, detail="The proposed backup unit is no longer free") from None
        view = inc.setdefault("citizen_view", {})
        view["phase1_hub"] = {"name": candidate["station"], **candidate["station_location"]}
        _set_leg(inc, "to_scene", backup["route"])
        inc["assigned_unit"] = view["assigned_unit"] = unit["unit_id"]
        # The released unit's claim goes with it: it no longer acts for, or
        # sees the contacts of, this call, and the backup can claim it.
        released = alert["unit_id"]
        inc["unit_assignments"] = {k: v for k, v in (inc.get("unit_assignments") or {}).items() if v != released}
        if inc.get("assigned_driver") == released:
            inc.pop("assigned_driver", None)
            inc["driver_claimed"] = False
        alert.update(status="backup_dispatched", decided_at=time.time(), decided_by=who[1])
        await incident_router.commit(inc)
    monitor.clear_alert(alert["unit_id"])
    text = (f"Dispatcher sent backup {backup['unit_id']} from {backup['station']}; "
            f"{alert['unit_id']} released.")
    monitor._log(incident_id, backup["unit_id"], "decision", text, alert_id=alert_id)
    from routers.chat import post_system_message

    await post_system_message(incident_id, text, sender="Dispatch decision")
    return {"status": "backup_dispatched", "unit_id": backup["unit_id"]}


@router.post("/api/incidents/{incident_id}/alerts/{alert_id}/dismiss")
async def dismiss_alert(incident_id: str, alert_id: str, request: Request = None):
    who = require_staff(request, "dispatcher")
    from routers import incident as incident_router

    async with incident_router._INCIDENTS_LOCK:
        inc, alert = await _with_alert(incident_id, alert_id)
        alert.update(status="dismissed", decided_at=time.time(), decided_by=who[1])
        await incident_router.commit(inc)
    monitor.clear_alert(alert["unit_id"])
    monitor._log(incident_id, alert["unit_id"], "decision", "Dispatcher dismissed the alert.", alert_id=alert_id)
    return {"status": "dismissed"}


# --- demo controls --------------------------------------------------------------------

def _ahead(route: list[dict], start: int, metres: float) -> int:
    """Index of the first vertex at least `metres` further along the route."""
    travelled = 0.0
    for i in range(start, len(route) - 1):
        travelled += monitor._dist(route[i], route[i + 1])
        if travelled >= metres:
            return i + 1
    return len(route) - 1


def edges_to_close(route: list[dict], start: int, position: dict) -> list[int]:
    """Graph edges on a stretch of the route ahead (about 350 m long, starting
    300 m or more ahead) whose closure still leaves the unit a way round. On a
    one-way flyover with no exit, closing the next stretch would trap it, so
    later stretches are tried, up to about 3 km ahead."""
    destination = (route[-1]["lat"], route[-1]["lng"])
    origin = (position["lat"], position["lng"])
    for begin in range(300, 3000, 350):
        a, b = _ahead(route, start, begin), _ahead(route, start, begin + 350)
        if b <= a:
            return []
        stretch = local_router.find_path((route[a]["lat"], route[a]["lng"]), (route[b]["lat"], route[b]["lng"]))
        if not stretch:
            continue
        avoid = {e: 1e6 for e in stretch["edge_ids"]}
        around = local_router.find_path(origin, destination, penalty=avoid)
        if around and not set(around["edge_ids"]) & set(stretch["edge_ids"]):
            return stretch["edge_ids"]
    return []


def wrong_turn_path(route: list[dict], index: int, position: dict) -> list[dict]:
    """A detour that really leaves the route: try both sides of the road at
    two distances and keep the first path that gets at least 200 m off the
    planned route within its first 1.5 km."""
    a, b = route[index], route[min(index + 1, len(route) - 1)]
    dx, dy = b["lng"] - a["lng"], b["lat"] - a["lat"]
    norm = math.hypot(dx, dy) or 1.0
    # Make the route's own roads very expensive, so the "driver" turns off at
    # the next junction instead of carrying on along the main road (which,
    # at rush hour, is otherwise the quickest way anywhere nearby).
    avoid: dict[int, float] = {}
    for p in route[index:]:
        node = local_router.nearest_node(p["lat"], p["lng"])
        if node is None:
            continue
        for slot in range(local_router._adj_start[node], local_router._adj_start[node + 1]):
            avoid[local_router._adj_edges[slot]] = 50.0
    for reach in (0.006, 0.011):
        for side in (1, -1):
            target = (position["lat"] + side * (dx / norm) * reach, position["lng"] - side * (dy / norm) * reach)
            path = local_router.find_path((position["lat"], position["lng"]), target, penalty=avoid)
            if not path:
                continue
            travelled, furthest = 0.0, 0.0
            for p, q in zip(path["coordinates"], path["coordinates"][1:]):
                travelled += monitor._dist(p, q)
                furthest = max(furthest, monitor.project(route, q)[0])
                if travelled > 1500:
                    break
            if furthest >= 200:
                return path["coordinates"]
    return []


@router.post("/api/monitoring/demo/{incident_id}")
async def stage_disruption(incident_id: str, demo: DemoAction, request: Request = None):
    require_staff(request, "dispatcher")
    from routers import incident as incident_router

    async with incident_router._INCIDENTS_LOCK:
        inc = next((i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        snapshot = incident_router.snapshot(inc) if inc else None
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    unit = fleet.unit_for_incident(incident_id)
    leg, route, _eta = monitor._leg(snapshot)
    if unit is None or leg is None or len(route) < 2:
        raise HTTPException(status_code=409, detail="No unit is driving on this call")
    if not city_state.ready:
        city_state.attach(local_router)
    position = unit["position"]
    _off, index = monitor.project(route, position)

    if demo.action == "wrong_turn":
        path = await asyncio.to_thread(wrong_turn_path, route, index, position)
        if not path:
            raise HTTPException(status_code=409, detail="No side road found for a wrong turn here")
        simulator.set_detour(unit["unit_id"], snapshot, path)
        monitor._log(incident_id, unit["unit_id"], "demo", f"Demo: {unit['unit_id']} takes a wrong turn.")
        return {"status": "staged", "action": demo.action}

    edges = await asyncio.to_thread(edges_to_close, route, index, position)
    if not edges:
        raise HTTPException(status_code=409, detail="The unit is too close to its destination to stage this")
    road = monitor.main_road([{"road": local_router.road_names[local_router.edge_name[e]]} for e in edges])
    if demo.action == "close_road_ahead":
        result = city_state.close_road(edges, label=f"Demo: {road} closed")
    else:
        result = city_state.report_incident(edges, label=f"Demo: accident on {road}")
    monitor._log(incident_id, unit["unit_id"], "demo",
                 f"Demo: {'closed' if demo.action == 'close_road_ahead' else 'accident on'} {road} ahead of "
                 f"{unit['unit_id']}.")
    return {"status": "staged", "action": demo.action, "road": road, "segments": len(edges),
            "detail": {k: v for k, v in result.items() if k in ("closed", "displaced", "obstructed")}}
