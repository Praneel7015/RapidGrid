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
import logging
import math
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import fleet
from agents import monitor, simulator
from agents.city_state import city_state
from agents.local_router import local_router
from logsafe import log_safe
from staff import require as require_staff

logger = logging.getLogger(__name__)

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
        new_destination = alert.get("new_destination")
        if new_destination:
            # A divert alert: the route leads to another hospital, so the
            # destination changes with it (and must still be open).
            from agents.hospital_intelligence import hospital_agent
            if new_destination.get("hospital_id") in hospital_agent.diverted:
                raise HTTPException(status_code=409, detail=f"{new_destination['name']} has gone on divert too; refresh")
            view = inc.setdefault("citizen_view", {})
            view.update({"hospital_name": new_destination["name"],
                         "hospital_location": dict(new_destination["location"]),
                         "message": f"Destination changed to {new_destination['name']}: the planned hospital went on divert."})
            inc.update({"hospital_name": new_destination["name"],
                        "approved_hospital_id": new_destination.get("hospital_id")})
            ranking = inc.get("pickup_ranking")
            if ranking:  # the crew's list marks the new hospital as current
                from routers.driver import _is_current, _summarise
                for h in ranking.get("hospitals", []):
                    h["current"] = _is_current(h, new_destination.get("hospital_id"), new_destination["name"])
                _summarise(ranking)
        _set_leg(inc, alert["leg"], route)
        alert.update(status="rerouted", decided_at=time.time(), decided_by=who[1], route_index=choice.route_index)
        await incident_router.commit(inc)
    monitor.clear_alert(alert["unit_id"])
    label = route["label"]
    named = "the recommended route" if label == "Recommended" else \
        (f"alternative route {label.split()[-1]}" if label.startswith("Alternative") else label.lower())
    if alert.get("new_destination"):
        named += f" to {alert['new_destination']['name']}"
    text = (f"Dispatcher accepted {named} for {alert['unit_id']}; "
            f"new ETA {route['eta_s'] / 60:.1f} min.")
    monitor._log(incident_id, alert["unit_id"], "decision", text, alert_id=alert_id)
    from routers.chat import post_system_message

    await post_system_message(incident_id, text, sender="Dispatch decision")
    return {"status": "rerouted", "route": route["label"]}


@router.post("/api/incidents/{incident_id}/alerts/{alert_id}/backup")
async def dispatch_backup(incident_id: str, alert_id: str, request: Request = None):
    who = require_staff(request, "dispatcher")
    from routers import incident as incident_router

    # The proposal's route is from where the backup was when the alert was
    # raised; it may have moved since. Route it again from where it is now,
    # off the event loop and outside the lock, before anything changes.
    async with incident_router._INCIDENTS_LOCK:
        inc, alert = await _with_alert(incident_id, alert_id)
        _same_leg(inc, alert)  # a stale alert gets the right answer before any routing
        if alert["leg"] != "to_scene" or not alert.get("backup"):
            raise HTTPException(status_code=409, detail="No backup unit was proposed for this alert")
        proposed = (alert.get("backup") or {}).get("unit_id")
        scene = dict(inc["location"]) if inc.get("location") else None
    here = (fleet.get(proposed) or {}).get("position") if proposed else None
    fresh = None
    if here and scene:
        path = await asyncio.to_thread(local_router.find_path, (here["lat"], here["lng"]),
                                       (scene["lat"], scene["lng"]))
        if not path:
            raise HTTPException(status_code=409, detail=(
                "The proposed backup has no road route to the scene from where it is now; "
                "the current unit stays assigned"))
        fresh = monitor._route_summary(path, "Backup")

    async with incident_router._INCIDENTS_LOCK:
        inc, alert = await _with_alert(incident_id, alert_id)
        _same_leg(inc, alert)
        backup = alert.get("backup")
        if alert["leg"] != "to_scene" or not backup or backup.get("unit_id") != proposed:
            raise HTTPException(status_code=409, detail="No backup unit was proposed for this alert")
        if fresh is not None:
            backup = {**backup, "route": {k: v for k, v in fresh.items() if k != "edge_ids"},
                      "eta_s": fresh["eta_s"]}
        candidate = fleet.get(backup["unit_id"])
        if candidate is None:
            raise HTTPException(status_code=409, detail="The proposed backup unit no longer exists")
        from routers.driver import units_on_calls
        if backup["unit_id"] in units_on_calls(incident_id):
            raise HTTPException(status_code=409, detail="The proposed backup unit is on another call")
        released_before = fleet.get(alert["unit_id"])
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
        try:
            await incident_router.commit(inc)
        except Exception:
            # The call record was restored; give the call back its first unit.
            try:
                fleet.swap(incident_id, released)
                if released_before:  # where it was, not back at its station
                    fleet.put_back(released, released_before["position"])
            except Exception:
                logger.exception("Could not hand %s back to %s after a failed save",
                                 log_safe(incident_id), log_safe(released))
            raise
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
    monitor.clear_alert(alert["unit_id"], dismissed_deviation=alert.get("trigger") == "deviation",
                        rebaseline=alert.get("trigger") == "slowing_ahead")
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


def edges_to_close(route: list[dict], start: int, position: dict,
                   keep_open: list[tuple[dict, dict]] = ()) -> list[int]:
    """Graph edges on a stretch of the route ahead (about 350 m long, starting
    300 m or more ahead) whose closure still leaves the unit a way round. On a
    one-way flyover with no exit, closing the next stretch would trap it, so
    later stretches are tried, up to about 3 km ahead.

    keep_open: other trips the call still needs (scene to hospital, say). A
    stretch is chosen only if each of them still has a way round too, so a
    staged closure can never leave the patient with no route to hospital."""
    destination = (route[-1]["lat"], route[-1]["lng"])
    origin = (position["lat"], position["lng"])

    def still_open(closed: set) -> bool:
        for a, b in keep_open:
            if not local_router.find_path((a["lat"], a["lng"]), (b["lat"], b["lng"]), also_blocked=closed):
                return False
        return True
    for begin in range(300, 3000, 350):
        a, b = _ahead(route, start, begin), _ahead(route, start, begin + 350)
        if b <= a:
            return []
        stretch = local_router.find_path((route[a]["lat"], route[a]["lng"]), (route[b]["lat"], route[b]["lng"]))
        if not stretch:
            continue
        # Tested as the closure will really be: blocked, not just costly
        # (a block also changes which road a point can start from).
        closed = set(stretch["edge_ids"])
        around = local_router.find_path(origin, destination, also_blocked=closed)
        if around and still_open(closed):
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
                if travelled > 1500:  # only the first 1.5 km counts
                    break
                furthest = max(furthest, monitor.project(route, q)[0])
            if furthest >= 200:
                return path["coordinates"]
    return []


@router.post("/api/monitoring/demo/{incident_id}")
async def stage_disruption(incident_id: str, demo: DemoAction, request: Request = None):
    require_staff(request, "dispatcher")
    from routers import incident as incident_router

    generation = RESET_GENERATION
    async with incident_router._INCIDENTS_LOCK:
        inc = next((i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        snapshot = incident_router.snapshot(inc) if inc else None
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    unit = fleet.unit_for_incident(incident_id)
    leg, route, _eta = monitor._leg(snapshot)
    if unit is None or leg is None or len(route) < 2:
        raise HTTPException(status_code=409, detail="No unit is driving on this call")
    # One disruption at a time: stacking them before dispatch has acted
    # gives chained alerts that no audience can follow.
    if any(a.get("status") == "open" for a in (snapshot.get("monitoring") or {}).get("alerts", [])):
        raise HTTPException(status_code=409, detail="Handle the open alert first")
    if simulator.has_detour(unit["unit_id"]):
        raise HTTPException(status_code=409, detail="The unit is already off its route (a wrong turn is in progress)")
    if not city_state.ready:
        city_state.attach(local_router)
    position = unit["position"]
    _off, index = monitor.project(route, position)

    async def unchanged() -> None:
        # The search ran without the lock: the crew may have picked up the
        # patient, or a backup taken over, meanwhile. Stage nothing then.
        _still_current(generation)
        async with incident_router._INCIDENTS_LOCK:
            now = next((i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
            now_leg, now_route, _ = monitor._leg(now) if now else (None, [], None)
            now_unit = fleet.unit_for_incident(incident_id)
            if (now is None or now_leg != leg or now_route != route
                    or (now_unit or {}).get("unit_id") != unit["unit_id"]):
                raise HTTPException(status_code=409, detail="The call moved on while staging; nothing was staged")

    if demo.action == "wrong_turn":
        path = await asyncio.to_thread(wrong_turn_path, route, index, position)
        await unchanged()
        if not path:
            raise HTTPException(status_code=409, detail="No side road found for a wrong turn here")
        simulator.set_detour(unit["unit_id"], snapshot, path)
        monitor._log(incident_id, unit["unit_id"], "demo", f"Demo: {unit['unit_id']} takes a wrong turn.")
        return {"status": "staged", "action": demo.action}

    # On the way to the scene, the trip on to hospital must survive too.
    view = snapshot.get("citizen_view") or {}
    keep_open = []
    if leg == "to_scene" and view.get("hospital_location") and snapshot.get("location"):
        keep_open.append((dict(snapshot["location"]), dict(view["hospital_location"])))
    edges = await asyncio.to_thread(edges_to_close, route, index, position, keep_open)
    await unchanged()
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


# Bumped by every reset: a demo staging request that started before a reset
# must not close roads or log decisions for a call that no longer exists.
RESET_GENERATION = 0


def _still_current(generation: int) -> None:
    if generation != RESET_GENERATION:
        raise HTTPException(status_code=409, detail="The demo was reset; nothing was staged")


async def reset_event_state() -> dict:
    """Every call, message, agent decision, closure, weather change, cached
    route and hospital override cleared; every unit available at its
    station. Citizen accounts stay. Waits for a simulator tick in flight."""
    from agents.hospital_intelligence import hospital_agent
    from routers import incident as incident_router
    from routers.chat import manager
    from storage import save_collection

    global RESET_GENERATION
    async with simulator.TICK_LOCK:
        RESET_GENERATION += 1
        for task in list(simulator._POSTING):
            task.cancel()
        async with incident_router._INCIDENTS_LOCK:
            cleared = len(incident_router.ACTIVE_INCIDENTS)
            incident_router.ACTIVE_INCIDENTS.clear()
            incident_router.save_db()
        fleet.reset()
        simulator.reset()
        monitor.reset()
        await manager.clear_all()
        if not city_state.ready:
            city_state.attach(local_router)
        city_state.weather = "Clear"
        city_state.interventions.clear()
        city_state.clear()
        # A known clock after reset: Bengaluru's hour now (not the hour the
        # server happened to boot at, nor one left over from a rehearsal).
        from agents.city_state import city_hour
        city_state.set_hour(city_hour())
        incident_router.route_agent._cache.clear()
        incident_router._reports.clear()
        save_collection("hospital_capacity", {}, database=incident_router.persistence_path())
        for key in list(hospital_agent.diverted):
            hospital_agent.set_divert(key, on_divert=False)
        for key in list(hospital_agent._reported_beds):
            hospital_agent.set_reported_beds(key, None)
    return {"incidents_cleared": cleared, "units": len(fleet.units())}


@router.post("/api/monitoring/reset")
async def reset_for_demo(request: Request = None):
    """Dispatch: start the demo from a clean slate (run before presenting).
    In the open demo (no staff PINs) only the operator may reset, so a
    visitor cannot wipe the stage."""
    import security
    import staff

    if staff.enabled():
        who = require_staff(request, "dispatcher")
    elif request is not None and security.authenticated(request):
        who = ("operator", "*")
    else:
        raise HTTPException(status_code=401, detail="Resetting needs the dispatcher (or operator) sign-in")
    result = await reset_event_state()
    monitor._log("-", "-", "demo", "Demo reset: calls, messages, closures and diverts cleared; units at stations.")
    import logging
    logging.getLogger("geoagentic.monitoring").info("Demo reset by %s", who[0])
    return {"status": "reset", **result}
