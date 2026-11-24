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


def along(route: list[dict], point: dict) -> float:
    """Distance along the polyline of the point on it nearest to `point`."""
    from agents.monitor import point_segment_m

    best, best_at, travelled = float("inf"), 0.0, 0.0
    for a, b in zip(route, route[1:]):
        seg = _distance(a, b)
        d, t = point_segment_m(point, a, b)
        if d < best:
            best, best_at = d, travelled + seg * t
        travelled += seg
    return best_at


def length(route: list[dict]) -> float:
    return sum(_distance(a, b) for a, b in zip(route, route[1:]))


# unit_id -> (leg key, metres travelled, start point if the unit began the
# leg away from its first point)
_progress: dict[str, tuple[str, float, Optional[dict]]] = {}
# unit_id -> (leg key it was set on, detour polyline, metres travelled). A
# demo "wrong turn": the unit follows the detour instead of its leg until the
# leg changes (for example, the dispatcher accepts a reroute).
_detours: dict[str, tuple[str, list[dict], float]] = {}


def route_key(route: list[dict]) -> str:
    """Identity of a route's geometry: any change (a reroute, even one with
    the same number of points) gives a new key."""
    import hashlib

    text = ";".join(f"{p['lat']:.6f},{p['lng']:.6f}" for p in route)
    return hashlib.blake2s(text.encode(), digest_size=8).hexdigest()


def leg_key(incident: dict) -> Optional[str]:
    key, route, _eta = fleet_leg(incident)
    return f"{incident['incident_id']}:{key}:{route_key(route)}" if key and len(route) >= 2 else None


def has_detour(unit_id: str) -> bool:
    return unit_id in _detours


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


def fleet_leg(incident: dict) -> tuple[Optional[str], list[dict], Optional[float]]:
    """The leg the call's fleet unit drives. On a police, fire or rescue
    call, the fleet unit is that vehicle: it drives to the scene and stays
    there. The hospital leg belongs to the backup ambulance that picked the
    patient up, not to the fire engine."""
    leg = _leg(incident)
    if leg[0] == "to_hospital" and (incident.get("vehicle_required") or "Ambulance") not in ("Ambulance", "Medical"):
        return None, [], None
    return leg


def step(incidents: list[dict], dt_s: float = TICK_S) -> int:
    """Advance every simulated unit by one tick. Returns how many moved."""
    moved = 0
    by_id = {i["incident_id"]: i for i in incidents}
    for unit in fleet.units():
        incident = by_id.get(unit.get("incident_id") or "")
        if incident is None or fleet.phone_tracking(unit["unit_id"]):
            continue
        key, route, eta_s = fleet_leg(incident)
        if key is None or len(route) < 2:
            continue
        leg_key = f"{incident['incident_id']}:{key}:{route_key(route)}"
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
        # A phone that stopped sending: simulation takes over from its last
        # real fix, not from where the simulation had left the unit.
        handover = unit["position"].get("source") == "gps"
        if handover and previous_key == leg_key:
            # Carry on from the last real fix: its distance along the leg,
            # on the leg as already driven (not back to its first point).
            here = unit["position"]
            driven = [start, *route] if start is not None else route
            travelled = along(driven, here)
        elif previous_key != leg_key or handover:
            travelled = 0.0
            here = unit["position"]
            start = None
            if _distance(here, route[0]) > 50:
                from agents.monitor import project

                if project(route, here)[0] <= 50:
                    # Already on the leg (a phone tracked it from dispatch,
                    # then went quiet): carry on from that point of it.
                    travelled = along(route, here)
                else:
                    # Off the leg (a backup taking over from elsewhere):
                    # drive from where it is, not from a jump to the first
                    # point. Fixed for the whole leg.
                    start = {"lat": here["lat"], "lng": here["lng"]}
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
        # A segment starting just past the tick's end can hold a closure
        # whose stopping point (15 m before it) is inside the tick.
        if travelled + seg >= start_m and travelled <= end_m + 15.0:
            first = None
            for c in closed:
                p, q = c["coordinates"]
                (dp, tp), (dq, tq) = point_segment_m(p, a, b), point_segment_m(q, a, b)
                if dp < 8 and dq < 8:
                    # Where along this segment the closure begins and ends.
                    lo, hi = travelled + seg * min(tp, tq), travelled + seg * max(tp, tq)
                    # Reached this tick, and not entirely behind the unit
                    # (a unit already on a road that just closed stops too).
                    if lo - 15.0 <= end_m and hi >= start_m - 15.0:
                        first = lo if first is None else min(first, lo)
            if first is not None:
                return max(0.0, first - 15.0)
        if travelled > end_m + 15.0:
            break
        travelled += seg
    return None


def reset() -> None:
    _progress.clear()
    _detours.clear()


def _stale_open_alerts(incident: dict) -> list[dict]:
    """Open alerts raised for a leg the call has left (or a finished call)."""
    from agents import monitor

    leg, _route, _eta = monitor._leg(incident)
    return [a for a in (incident.get("monitoring") or {}).get("alerts", [])
            if a.get("status") == "open" and a.get("leg") != leg]


async def supersede_stale_alerts() -> None:
    """Close open alerts for legs the call has left, so they cannot be acted on."""
    from routers import incident as incident_router

    async with incident_router._INCIDENTS_LOCK:
        for inc in incident_router.ACTIVE_INCIDENTS:
            stale = _stale_open_alerts(inc)
            for alert in stale:
                alert["status"] = "superseded"
            if stale:
                await incident_router.commit(inc)


def caller_safe_summary(alert: dict) -> str:
    """One line for everyone on the call: what happened and the new ETA."""
    what = "left its planned route" if alert.get("trigger") == "deviation" else "has a disruption on its route"
    cause = (alert.get("cause") or {}).get("text") or "cause being checked"
    eta = alert.get("new_eta_s")
    eta_text = f" New estimated arrival in {eta / 60:.0f} min." if eta else " Dispatch is finding a way through."
    return f"{alert.get('unit_id', 'The unit')} {what}: {cause}.{eta_text}"


async def store_alerts(alerts: list[tuple[str, dict]]) -> None:
    """Keep each incident's newest alerts (up to 10) with the incident."""
    from routers import incident as incident_router

    stored: list[tuple[str, dict]] = []
    async with incident_router._INCIDENTS_LOCK:
        for incident_id, alert in alerts:
            inc = next((i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
            if inc is None:
                continue
            # Computed off the lock: if the call's leg or destination changed
            # meanwhile (the crew picked another hospital), it is stale.
            leg, route, _eta = _leg(inc)
            where = alert.get("destination")
            if leg != alert.get("leg") or (where and route and (
                    abs(route[-1]["lat"] - where["lat"]) > 1e-6 or abs(route[-1]["lng"] - where["lng"]) > 1e-6)):
                continue
            # Nor if another unit now holds the call (a backup took over),
            # or its route changed: the advice was for that unit and route.
            holder = (fleet.unit_for_incident(incident_id) or {}).get("unit_id")
            if alert.get("unit_id") and holder != alert["unit_id"]:
                continue
            if alert.get("route_key") and route and route_key(route) != alert["route_key"]:
                continue
            monitoring = inc.setdefault("monitoring", {"alerts": []})
            monitoring["alerts"] = [alert] + monitoring.get("alerts", [])[:9]
            try:
                await incident_router.commit(inc)
            except Exception:  # one incident's save failing must not drop the others' alerts
                # The record was restored without it: do not announce an
                # alert that dispatch cannot see or act on.
                logger.exception("Could not save a monitoring alert")
                continue
            stored.append((incident_id, alert))
    # The monitor marked each of these units as having an open alert; one
    # that was not stored (stale, or its save failed) must not leave the
    # unit unwatched for the rest of the leg with nothing to act on.
    from agents import monitor

    kept = {id(a) for _i, a in stored}
    for _incident_id, alert in alerts:
        if id(alert) not in kept and alert.get("unit_id"):
            monitor.clear_alert(alert["unit_id"])
    if stored:
        # The channel includes the caller: post the facts (cause, new ETA),
        # not the dispatcher's advice (backup, hold), which stays on the
        # dispatch screen. In the background, with a time limit, so a slow
        # chat client never holds up the simulator tick.
        task = asyncio.create_task(_post_alerts(stored))
        _POSTING.add(task)
        task.add_done_callback(_POSTING.discard)


_POSTING: set = set()
TICK_LOCK = asyncio.Lock()
POST_TIMEOUT_S = 5.0


async def _post_alerts(stored: list[tuple[str, dict]]) -> None:
    from routers.chat import post_system_message

    for incident_id, alert in stored:
        try:
            await asyncio.wait_for(post_system_message(incident_id, caller_safe_summary(alert)), POST_TIMEOUT_S)
        except Exception:
            logger.warning("Posting a monitoring alert to the incident channel timed out or failed")


async def run() -> None:
    """Background loop, started with the app."""
    from routers import incident as incident_router

    logger.info("Unit simulator running: tick %.0f s, speed x%.0f", TICK_S, sim_speed())
    while True:
        try:
            async with TICK_LOCK:  # a demo reset waits for a tick in flight
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
        try:
            # Every call, finished ones included, and even after a failed
            # tick: an open alert on a completed call must close. Writes only
            # what is stale.
            async with TICK_LOCK:
                await supersede_stale_alerts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Closing stale alerts failed")
        await asyncio.sleep(TICK_S)
