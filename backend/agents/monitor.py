"""The monitoring agent: watches every unit on a call and advises the control room.

A rule-based agent, not a language model. Every tick it runs one
observe → decide → act loop per moving unit, and every decision is logged
with its reasons. A dispatcher approves every action it proposes.

Observe
    The unit's latest fix, projected onto its planned leg (station to scene,
    or scene to hospital), with a progress index that only moves forward;
    and the city model (closures, accidents, congestion by cause).
Detect (objective 2)
    *Deviation*: more than ``OFF_ROUTE_M`` plus the fix's accuracy off the
    planned leg for ``OFF_ROUTE_FIXES`` fixes in a row.
    *Blocked ahead*: a closure or accident on the remaining planned route,
    which will force a deviation.
Explain the cause (objective 3)
    Closures and accidents on the remaining route, then within
    ``CAUSE_RADIUS_M`` of the unit; otherwise the most congested segment on
    the route with the city model's dominant cause (displaced traffic, rush
    hour, rain). If nothing is found: "left the route; no disruption found".
Predict (objective 4)
    Planned remaining time (the leg's planned ETA scaled by the distance
    left) against the best route from where the unit is now, under current
    conditions: the delay and the new ETA.
Reroute (objective 5)
    The best route from the unit's position, and up to two alternatives
    (the previous routes' roads penalised; near-duplicates dropped). The
    major intersections on the recommended route are listed as a
    green-corridor request for traffic police (advice; no signal control).
Advise (objective 6)
    On the way to the scene, compare the delayed ETA with the nearest free
    unit of the same type; recommend a backup when it would arrive
    meaningfully sooner. With the patient on board, always keep the unit.
    The result is one plain-English paragraph.
Act
    One open alert per unit (no repeats while it stands), shown to the
    dispatcher with Accept reroute, Dispatch backup and Dismiss.

Times are modelled road minutes. The on-stage simulator moves units faster
than real time (``RAPIDGRID_SIM_SPEED``); ETAs are not scaled by it.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import fleet
from agents.city_state import city_state
from agents.local_router import local_router

logger = logging.getLogger("geoagentic.monitor")

OFF_ROUTE_M = 75.0
OFF_ROUTE_FIXES = 3
MAX_ACCURACY_M = 50.0         # worse fixes are not used for deviation checks
NEAR_ROUTE_M = 35.0           # a disruption this close to the route is "on" it
CAUSE_RADIUS_M = 500.0
HEAVY_CONGESTION = 1.5
BACKUP_MIN_GAIN_S = 180.0     # a backup must save at least 3 minutes ...
BACKUP_MIN_GAIN_FRACTION = 0.2  # ... and at least 20%
ALT_PENALTY = 1.8
ALT_MAX_OVERLAP = 0.7
LOG_SIZE = 200


# --- geometry -------------------------------------------------------------------

def _xy(p: dict, lat0: float) -> tuple[float, float]:
    k = 111_320.0
    return (p["lng"] * k * math.cos(math.radians(lat0)), p["lat"] * k)


def point_segment_m(p: dict, a: dict, b: dict) -> tuple[float, float]:
    """Distance from p to segment ab in metres, and the position along it (0-1)."""
    lat0 = p["lat"]
    px, py = _xy(p, lat0)
    ax, ay = _xy(a, lat0)
    bx, by = _xy(b, lat0)
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), t


def _dist(a: dict, b: dict) -> float:
    return point_segment_m(a, b, b)[0]


def project(route: list[dict], point: dict, start_index: int = 0) -> tuple[float, int]:
    """(metres off the route, index of the nearest segment at or after start_index-2)."""
    best = (float("inf"), start_index)
    for i in range(max(0, start_index - 2), len(route) - 1):
        d, _t = point_segment_m(point, route[i], route[i + 1])
        if d < best[0]:
            best = (d, i)
    return best


def polyline_length(route: list[dict]) -> float:
    return sum(_dist(a, b) for a, b in zip(route, route[1:]))


def _near_route(segment: list[dict], route: list[dict], within: float) -> bool:
    for p in segment:
        if project(route, p)[0] <= within:
            return True
    return False


# --- state ------------------------------------------------------------------------

@dataclass
class Watch:
    leg_key: str = ""
    progress: int = 0
    off_count: int = 0
    city_version: tuple = ()
    alert_id: Optional[str] = None
    started_near_station: bool = True


_watches: dict[str, Watch] = {}
decision_log: deque = deque(maxlen=LOG_SIZE)


def reset() -> None:
    _watches.clear()
    decision_log.clear()


def _log(incident_id: str, unit_id: str, kind: str, text: str, **extra) -> dict:
    entry = {"at": time.time(), "incident_id": incident_id, "unit_id": unit_id,
             "kind": kind, "text": text, **extra}
    decision_log.appendleft(entry)
    return entry


def _city_version() -> tuple:
    return (len(city_state.closed_edges), len(city_state.incident_edges), city_state.hour,
            city_state.weather, len(city_state.interventions))


def _leg(incident: dict) -> tuple[Optional[str], list[dict], Optional[float]]:
    view = incident.get("citizen_view") or {}
    if incident.get("status") == "dispatched":
        eta = view.get("phase1_eta_minutes")
        return "to_scene", view.get("phase1_route_coordinates") or [], eta * 60 if eta else None
    if incident.get("status") == "patient_picked_up":
        eta = view.get("eta_minutes")
        return "to_hospital", view.get("route_coordinates") or [], eta * 60 if eta else None
    return None, [], None


# --- analysis ---------------------------------------------------------------------

def _disruptions_near(route: list[dict], within: float) -> dict:
    """Closures, accidents and the worst congestion on (or near) a polyline."""
    found = {"closed": [], "incident": [], "congested": None}
    if not city_state.ready or len(route) < 2:
        return found
    for seg in city_state.closure_overlay():
        if _near_route(seg["coordinates"], route, within):
            found["closed"].append(seg)
    for seg in city_state.congestion_overlay(limit=600):
        if not _near_route(seg["coordinates"], route, within):
            continue
        if seg["edge_id"] in city_state.incident_edges:
            found["incident"].append(seg)
        elif seg["multiplier"] >= HEAVY_CONGESTION and (
            found["congested"] is None or seg["multiplier"] > found["congested"]["multiplier"]
        ):
            found["congested"] = seg
    return found


_CAUSE_WORDS = {
    "displacement": "traffic displaced from a nearby closure",
    "time_of_day": "rush-hour traffic",
    "weather": "the weather",
    "incident": "an accident beside the road",
}


def main_road(segments: list[dict]) -> str:
    """The road most of these segments belong to."""
    counts: dict[str, int] = {}
    for seg in segments:
        counts[seg["road"]] = counts.get(seg["road"], 0) + 1
    return max(counts, key=counts.get) if counts else "an unnamed road"


def find_cause(remaining: list[dict], position: dict, trigger: str) -> dict:
    on_route = _disruptions_near(remaining, NEAR_ROUTE_M)
    if on_route["closed"]:
        road = main_road(on_route["closed"])
        return {"kind": "roadblock", "road": road, "likely": False,
                "text": f"road closed ahead on {road} ({len(on_route['closed'])} segments)"}
    if on_route["incident"]:
        road = main_road(on_route["incident"])
        return {"kind": "accident", "road": road, "likely": False,
                "text": f"accident reported on {road}"}
    nearby = _disruptions_near([position, position], CAUSE_RADIUS_M) if trigger == "deviation" else on_route
    if nearby["closed"]:
        seg = nearby["closed"][0]
        return {"kind": "roadblock", "road": seg["road"], "likely": True,
                "text": f"likely the closure on {seg['road']} nearby"}
    if nearby["incident"]:
        seg = nearby["incident"][0]
        return {"kind": "accident", "road": seg["road"], "likely": True,
                "text": f"likely the accident on {seg['road']} nearby"}
    worst = on_route["congested"] or nearby["congested"]
    if worst is not None:
        causes = city_state.explain(worst["edge_id"]).get("causes", [])
        dominant = max(causes, key=lambda c: c["multiplier"], default=None)
        why = _CAUSE_WORDS.get(dominant["cause"], dominant["cause"]) if dominant else "congestion"
        return {"kind": "congestion", "road": worst["road"], "likely": trigger == "deviation",
                "text": f"heavy congestion on {worst['road']} (x{worst['multiplier']:.2f}, from {why})"}
    return {"kind": "unexplained", "road": None, "likely": False,
            "text": "the unit left the planned route; no closure, accident or congestion found nearby"}


def _route_summary(path: dict, label: str) -> dict:
    return {"label": label, "coordinates": path["coordinates"], "eta_s": path["travel_time_s"],
            "distance_m": path["distance_m"], "roads": path["roads"][:4], "edge_ids": path["edge_ids"]}


def alternatives(origin: dict, destination: dict, fallbacks: list[dict] = ()) -> list[dict]:
    """The best route and up to two that differ from it by at least 30%.

    If no route leaves the unit's exact point (it snapped onto a closed
    one-way stretch, say), try from the route points just behind it: the
    junction it last passed."""
    d = (destination["lat"], destination["lng"])
    best, o = None, None
    for start in [origin, *fallbacks]:
        o = (start["lat"], start["lng"])
        best = local_router.find_path(o, d)
        if best:
            break
    if not best:
        return []
    routes = [_route_summary(best, "Recommended")]
    penalty: dict[int, float] = {}
    for label in ("Alternative A", "Alternative B"):
        for r in routes:
            for e in r["edge_ids"]:
                penalty[e] = ALT_PENALTY
        alt = local_router.find_path(o, d, penalty=penalty)
        if not alt:
            break
        shared = set(alt["edge_ids"]) & set(best["edge_ids"])
        shared_len = sum(local_router.edge_len[e] for e in shared)
        if alt["distance_m"] and shared_len / alt["distance_m"] > ALT_MAX_OVERLAP:
            continue
        routes.append(_route_summary(alt, label))
    return routes


def corridor(route: dict, limit: int = 6) -> list[dict]:
    """Junctions on a route where it turns onto another road: the points to
    hold for a green corridor. Expected arrival in minutes from now."""
    coords, edges = route["coordinates"], route["edge_ids"]
    out, elapsed = [], 0.0
    previous = None
    for i, e in enumerate(edges):
        name = local_router.road_names[local_router.edge_name[e]]
        if previous is not None and name != previous and i < len(coords):
            out.append({"lat": coords[i]["lat"], "lng": coords[i]["lng"], "road": name,
                        "eta_min": round(elapsed / 60, 1)})
            if len(out) >= limit:
                break
        elapsed += local_router._edge_cost(e)
        previous = name
    return out


def best_backup(unit: dict, destination: dict) -> Optional[dict]:
    """The nearest free unit of the same type and its route time to the destination."""
    free = [u for u in fleet.units()
            if u["type"] == unit["type"] and u["status"] == "available" and u["unit_id"] != unit["unit_id"]]
    free.sort(key=lambda u: _dist(u["position"], destination))
    best = None
    for candidate in free[:3]:
        path = local_router.find_path((candidate["position"]["lat"], candidate["position"]["lng"]),
                                      (destination["lat"], destination["lng"]))
        if path and (best is None or path["travel_time_s"] < best["eta_s"]):
            best = {"unit_id": candidate["unit_id"], "station": candidate["station"],
                    "eta_s": path["travel_time_s"], "route": _route_summary(path, "Backup")}
    return best


def _minutes(seconds: Optional[float]) -> str:
    return "unknown" if seconds is None else f"{seconds / 60:.1f} min"


def analyse(incident: dict, unit: dict, leg: str, route: list[dict], eta_s: Optional[float],
            progress: int, trigger: str) -> Optional[dict]:
    position = {"lat": unit["position"]["lat"], "lng": unit["position"]["lng"]}
    remaining = [position] + route[progress + 1:]
    destination = route[-1]
    # Where to search from. On its route, the unit is on a known carriageway:
    # start from the route's own road nodes (its position between them can
    # snap to the opposite carriageway of a divided road, which leads
    # nowhere). Off its route, start from where it actually is.
    behind = list(reversed(route[max(0, progress - 4):progress + 1]))
    if trigger == "deviation":
        routes = alternatives(position, destination, behind)
    else:
        ahead = route[progress + 1:progress + 2]
        starts = behind[:1] + ahead + behind[1:] + [position]
        routes = alternatives(starts[0], destination, starts[1:])
    total = polyline_length(route) or 1.0
    planned_remaining = eta_s * polyline_length(remaining) / total if eta_s else None
    # No route at all from where the unit is (say, a closed one-way flyover):
    # it cannot get there, so the only useful advice is a backup.
    new_eta = routes[0]["eta_s"] if routes else None
    delay = new_eta - planned_remaining if (planned_remaining is not None and new_eta is not None) else None
    cause = find_cause(remaining, position, trigger)

    backup = best_backup(unit, destination) if leg == "to_scene" else None
    if backup and new_eta is None:
        recommend_backup = True
        gain = None
    else:
        gain = (new_eta - backup["eta_s"]) if backup else None
        recommend_backup = bool(backup) and gain >= BACKUP_MIN_GAIN_S and gain / new_eta >= BACKUP_MIN_GAIN_FRACTION
    recommendation = "dispatch_backup" if recommend_backup else ("reroute" if routes else "hold")
    green = corridor(routes[0]) if routes else []

    where = "to the scene" if leg == "to_scene" else "to the hospital"
    first = (f"{unit['unit_id']} left its planned route" if trigger == "deviation"
             else f"{unit['unit_id']}'s planned route is blocked ahead")
    lines = [
        f"{first} on the way {where}.",
        f"Cause: {cause['text']}.",
        (f"Delay: {'+' if delay >= 0 else ''}{delay / 60:.1f} min; new ETA {_minutes(new_eta)} from now."
         if delay is not None else
         (f"New ETA {_minutes(new_eta)} from now." if new_eta is not None
          else "No road route from the unit's position: it cannot get through.")),
    ]
    if routes:
        lines.append(f"Recommended route: via {', '.join(routes[0]['roads'][:2]) or 'local roads'}"
                     + (f" ({len(routes) - 1} alternative{'s' if len(routes) > 2 else ''} shown)."
                        if len(routes) > 1 else "."))
    if leg == "to_hospital":
        lines.append("Backup: not useful; the patient is on board. Keep this unit on the new route.")
    elif recommend_backup and gain is None:
        lines.append(f"Backup: dispatch {backup['unit_id']} from {backup['station']}; it arrives in "
                     f"{_minutes(backup['eta_s'])}.")
    elif recommend_backup:
        lines.append(f"Backup: dispatch {backup['unit_id']} from {backup['station']}; it arrives in "
                     f"{_minutes(backup['eta_s'])}, {gain / 60:.1f} min sooner than {unit['unit_id']}.")
    elif backup:
        lines.append(f"Backup: not needed; the nearest free unit ({backup['unit_id']}) needs "
                     f"{_minutes(backup['eta_s'])}. Keep {unit['unit_id']} on the new route.")
    elif routes:
        lines.append(f"Backup: no free {unit['type'].lower()} available. Keep {unit['unit_id']} on the new route.")
    else:
        lines.append(f"Backup: no free {unit['type'].lower()} available. Hold {unit['unit_id']} and clear the road.")
    if green:
        lines.append(f"Green-corridor request: {len(green)} junctions on the new route, first in "
                     f"{green[0]['eta_min']:.1f} min (advice for traffic police; no live signal control).")

    return {
        "alert_id": uuid.uuid4().hex[:12],
        "at": time.time(),
        "status": "open",
        "unit_id": unit["unit_id"],
        "leg": leg,
        "trigger": trigger,
        "position": position,
        "cause": cause,
        "planned_remaining_s": planned_remaining,
        "new_eta_s": new_eta,
        "delay_s": delay,
        "routes": [{k: v for k, v in r.items() if k != "edge_ids"} for r in routes],
        "backup": ({k: v for k, v in backup.items() if k != "route"}
                   | {"route": {k: v for k, v in backup["route"].items() if k != "edge_ids"}}) if backup else None,
        "recommendation": recommendation,
        "corridor": green,
        "explanation": " ".join(lines),
        "provenance": "modelled",
    }


# --- the loop ---------------------------------------------------------------------

def tick(incidents: list[dict]) -> list[tuple[str, dict]]:
    """One observe-decide pass over every unit on a call.

    Returns (incident_id, alert) pairs for new alerts; the caller stores them.
    Pure computation, safe to run off the event loop.
    """
    new_alerts: list[tuple[str, dict]] = []
    by_id = {i["incident_id"]: i for i in incidents}
    version = _city_version()
    for unit in fleet.units():
        incident = by_id.get(unit.get("incident_id") or "")
        if incident is None:
            _watches.pop(unit["unit_id"], None)
            continue
        leg, route, eta_s = _leg(incident)
        if leg is None or len(route) < 2:
            continue
        watch = _watches.setdefault(unit["unit_id"], Watch())
        leg_key = f"{incident['incident_id']}:{leg}:{len(route)}:{route[0]['lat']:.5f}"
        if watch.leg_key != leg_key:
            # A new leg (or an accepted reroute): start watching it afresh.
            watch.leg_key, watch.progress, watch.off_count, watch.alert_id = leg_key, 0, 0, None
            watch.city_version = ()
            pos = unit["position"]
            watch.started_near_station = (pos.get("source") != "gps"
                                          or _dist(pos, route[0]) <= 300)
        pos = unit["position"]
        point = {"lat": pos["lat"], "lng": pos["lng"]}
        off, index = project(route, point, watch.progress)
        accuracy = pos.get("accuracy_m") or 0.0
        usable = accuracy <= MAX_ACCURACY_M and watch.started_near_station
        if off <= OFF_ROUTE_M + accuracy:
            watch.progress = max(watch.progress, index)
            watch.off_count = 0
        elif usable:
            watch.off_count += 1

        trigger = None
        if watch.off_count >= OFF_ROUTE_FIXES:
            trigger = "deviation"
        elif watch.city_version != version:
            watch.city_version = version
            ahead = _disruptions_near(route[watch.progress:], NEAR_ROUTE_M)
            if ahead["closed"] or ahead["incident"]:
                trigger = "blocked_ahead"
        if trigger is None or watch.alert_id is not None:
            continue
        alert = analyse(incident, unit, leg, route, eta_s, watch.progress, trigger)
        if alert is None:
            continue
        watch.alert_id = alert["alert_id"]
        watch.city_version = version
        _log(incident["incident_id"], unit["unit_id"], trigger, alert["explanation"],
             alert_id=alert["alert_id"], recommendation=alert["recommendation"])
        logger.info("Monitoring alert %s for %s: %s", alert["alert_id"], unit["unit_id"], trigger)
        new_alerts.append((incident["incident_id"], alert))
    return new_alerts


def clear_alert(unit_id: str) -> None:
    """The dispatcher acted on (or dismissed) the unit's alert; watch again."""
    watch = _watches.get(unit_id)
    if watch:
        watch.alert_id = None
        watch.off_count = 0
        watch.city_version = _city_version()
