"""Simulated GPS for units without a phone sending fixes.

Every tick, each assigned unit on an active call moves along its current leg
(station to scene, then scene to hospital) at the leg's modelled average
speed, multiplied by ``SIM_SPEED`` so a 15-minute drive plays out in a few
minutes on stage. Fixes are recorded with ``source="simulated"`` and the
screens label them, with the speed-up, so nothing passes for real tracking.

A unit whose phone has sent a fix recently is left alone: real GPS wins.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Optional

import fleet

logger = logging.getLogger("geoagentic.simulator")

TICK_S = 2.0
_DEFAULT_SPEED_MPS = 8.33  # 30 km/h when a leg has no ETA


def sim_speed() -> float:
    try:
        return max(1.0, min(30.0, float(os.getenv("RAPIDGRID_SIM_SPEED", "6"))))
    except ValueError:
        return 6.0


def _distance(a: dict, b: dict) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lng"] - a["lng"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _bearing(a: dict, b: dict) -> float:
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dl = math.radians(b["lng"] - a["lng"])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def point_along(route: list[dict], distance_m: float) -> tuple[dict, float, bool]:
    """(point, heading, reached_end) at distance_m along the polyline."""
    if len(route) < 2:
        return dict(route[0]) if route else {}, 0.0, True
    travelled = 0.0
    for a, b in zip(route, route[1:]):
        seg = _distance(a, b)
        if travelled + seg >= distance_m and seg > 0:
            f = (distance_m - travelled) / seg
            point = {"lat": a["lat"] + (b["lat"] - a["lat"]) * f, "lng": a["lng"] + (b["lng"] - a["lng"]) * f}
            return point, _bearing(a, b), False
        travelled += seg
    return dict(route[-1]), _bearing(route[-2], route[-1]), True


def length(route: list[dict]) -> float:
    return sum(_distance(a, b) for a, b in zip(route, route[1:]))


# unit_id -> (leg key, metres travelled, start point if the unit began the
# leg away from its first point)
_progress: dict[str, tuple[str, float, Optional[dict]]] = {}
# unit_id -> (leg key it was set on, detour polyline, metres travelled). A
# demo "wrong turn": the unit follows the detour instead of its leg until the
# leg changes (for example, the dispatcher accepts a reroute).
_detours: dict[str, tuple[str, list[dict], float]] = {}


def leg_key(incident: dict) -> Optional[str]:
    key, route, _eta = _leg(incident)
    return f"{incident['incident_id']}:{key}:{len(route)}" if key and len(route) >= 2 else None


def set_detour(unit_id: str, incident: dict, detour: list[dict]) -> None:
    key = leg_key(incident)
    if key and len(detour) >= 2:
        _detours[unit_id] = (key, detour, 0.0)


def _leg(incident: dict) -> tuple[Optional[str], list[dict], Optional[float]]:
    """The leg the unit is driving now: its key, polyline and ETA in seconds."""
    view = incident.get("citizen_view") or {}
    if incident.get("status") == "dispatched":
        eta = view.get("phase1_eta_minutes")
        return "to_scene", view.get("phase1_route_coordinates") or [], eta * 60 if eta else None
    if incident.get("status") == "patient_picked_up":
        eta = view.get("eta_minutes")
        return "to_hospital", view.get("route_coordinates") or [], eta * 60 if eta else None
    return None, [], None


def step(incidents: list[dict], dt_s: float = TICK_S) -> int:
    """Advance every simulated unit by one tick. Returns how many moved."""
    moved = 0
    by_id = {i["incident_id"]: i for i in incidents}
    for unit in fleet.units():
        incident = by_id.get(unit.get("incident_id") or "")
        if incident is None or fleet.phone_tracking(unit["unit_id"]):
            continue
        key, route, eta_s = _leg(incident)
        if key is None or len(route) < 2:
            continue
        leg_key = f"{incident['incident_id']}:{key}:{len(route)}"
        detour = _detours.get(unit["unit_id"])
        if detour is not None and detour[0] != leg_key:
            _detours.pop(unit["unit_id"], None)  # the leg changed: back to normal
            detour = None
        if detour is not None:
            _key, path, done = detour
            done = min(length(path), done + _DEFAULT_SPEED_MPS * dt_s * sim_speed())
            _detours[unit["unit_id"]] = (_key, path, done)
            point, heading, _end = point_along(path, done)
            fleet.record_position(unit["unit_id"], point["lat"], point["lng"], source="simulated", heading=heading)
            moved += 1
            continue
        previous_key, travelled, start = _progress.get(unit["unit_id"], (None, 0.0, None))
        if previous_key != leg_key:
            travelled = 0.0
            # A unit that is not at the leg's start (a backup taking over, or
            # a phone that stopped sending) drives from where it is, not
            # from a jump back to the first point. Fixed for the whole leg.
            here = unit["position"]
            start = {"lat": here["lat"], "lng": here["lng"]} if _distance(here, route[0]) > 50 else None
        if start is not None:
            route = [start] + route
        total = length(route)
        speed = (total / eta_s) if eta_s and total else _DEFAULT_SPEED_MPS
        target = min(total, travelled + speed * dt_s * sim_speed())
        # A unit cannot drive through a closed road: it stops before it and
        # waits (for a reroute, or the road to reopen).
        stop = _closure_on(route, travelled, target)
        travelled = max(travelled, stop) if stop is not None else target
        _progress[unit["unit_id"]] = (leg_key, travelled, start)
        point, heading, _end = point_along(route, travelled)
        fleet.record_position(unit["unit_id"], point["lat"], point["lng"], source="simulated", heading=heading)
        moved += 1
    return moved


def _closure_on(route: list[dict], start_m: float, end_m: float) -> Optional[float]:
    """Distance along the route (a little before it) of the first closed
    segment between start_m and end_m, or None."""
    from agents.city_state import city_state
    from agents.monitor import point_segment_m

    if not city_state.ready or not city_state.closed_edges:
        return None
    closed = city_state.closure_overlay()
    travelled = 0.0
    for a, b in zip(route, route[1:]):
        seg = _distance(a, b)
        if travelled + seg >= start_m and travelled <= end_m:
            for c in closed:
                p, q = c["coordinates"]
                if point_segment_m(p, a, b)[0] < 8 and point_segment_m(q, a, b)[0] < 8:
                    return max(0.0, travelled - 15.0)
        if travelled > end_m:
            break
        travelled += seg
    return None


def reset() -> None:
    _progress.clear()
    _detours.clear()


async def store_alerts(alerts: list[tuple[str, dict]]) -> None:
    """Keep each incident's newest alerts (up to 10) with the incident."""
    from routers import incident as incident_router

    async with incident_router._INCIDENTS_LOCK:
        for incident_id, alert in alerts:
            inc = next((i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
            if inc is None:
                continue
            monitoring = inc.setdefault("monitoring", {"alerts": []})
            monitoring["alerts"] = [alert] + monitoring.get("alerts", [])[:9]
            await incident_router.commit(inc)


async def run() -> None:
    """Background loop, started with the app."""
    from routers import incident as incident_router

    logger.info("Unit simulator running: tick %.0f s, speed x%.0f", TICK_S, sim_speed())
    while True:
        try:
            async with incident_router._INCIDENTS_LOCK:
                active = [incident_router.snapshot(i) for i in incident_router.ACTIVE_INCIDENTS
                          if i.get("status") in ("dispatched", "patient_picked_up")]
            step(active)
            from agents import monitor

            alerts = await asyncio.to_thread(monitor.tick, active)
            if alerts:
                await store_alerts(alerts)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Simulator tick failed")
        await asyncio.sleep(TICK_S)
