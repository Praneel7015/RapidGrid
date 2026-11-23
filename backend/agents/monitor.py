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
DISMISS_QUIET_S = 60.0
DISMISS_FURTHER_M = 500.0


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


def _box(route: list[dict], within: float) -> tuple[float, float, float, float]:
    """The route's bounding box grown by `within` metres (lat/lng degrees)."""
    lats = [p["lat"] for p in route]
    lngs = [p["lng"] for p in route]
    dlat = within / 111_320.0
    dlng = within / (111_320.0 * max(0.1, math.cos(math.radians(sum(lats) / len(lats)))))
    return min(lats) - dlat, max(lats) + dlat, min(lngs) - dlng, max(lngs) + dlng


def _segments_cross(a: dict, b: dict, c: dict, d: dict) -> bool:
    """Do segments ab and cd intersect (planar, near this latitude)?"""
    lat0 = a["lat"]
    (ax, ay), (bx, by), (cx, cy), (dx, dy) = (_xy(p, lat0) for p in (a, b, c, d))

    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)

    o1, o2 = orient(ax, ay, bx, by, cx, cy), orient(ax, ay, bx, by, dx, dy)
    o3, o4 = orient(cx, cy, dx, dy, ax, ay), orient(cx, cy, dx, dy, bx, by)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and o1 != 0 and o3 != 0


def segment_distance_m(a: dict, b: dict, c: dict, d: dict) -> float:
    """Distance in metres between segments ab and cd (0 where they cross)."""
    if _segments_cross(a, b, c, d):
        return 0.0
    return min(point_segment_m(a, c, d)[0], point_segment_m(b, c, d)[0],
               point_segment_m(c, a, b)[0], point_segment_m(d, a, b)[0])


def _near_route(segment: list[dict], route: list[dict], within: float, box=None) -> bool:
    """Is any part of this polyline within `within` metres of the route? A
    closed edge that crosses the route between two of its points counts."""
    if box is not None:
        # Cheap rejection on bounds: the segment's box must overlap the
        # route's grown box (a crossing edge can have no point inside it).
        lats = [p["lat"] for p in segment]
        lngs = [p["lng"] for p in segment]
        if max(lats) < box[0] or min(lats) > box[1] or max(lngs) < box[2] or min(lngs) > box[3]:
            return False
    if len(segment) == 1:
        return project(route, segment[0])[0] <= within
    dlat = within / 111_320.0
    dlng = dlat / max(0.1, math.cos(math.radians(route[0]["lat"])))
    for p, q in zip(segment, segment[1:]):
        lo_lat, hi_lat = min(p["lat"], q["lat"]) - dlat, max(p["lat"], q["lat"]) + dlat
        lo_lng, hi_lng = min(p["lng"], q["lng"]) - dlng, max(p["lng"], q["lng"]) + dlng
        for a, b in zip(route, route[1:]):
            if (max(a["lat"], b["lat"]) < lo_lat or min(a["lat"], b["lat"]) > hi_lat
                    or max(a["lng"], b["lng"]) < lo_lng or min(a["lng"], b["lng"]) > hi_lng):
                continue  # bounds too far apart to be within `within`
            if segment_distance_m(p, q, a, b) <= within:
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
    last_fix_at: Optional[float] = None   # a fix is counted once, not every tick
    alert_city_version: tuple = ()        # the city state the open alert was about
    # After dispatch dismissed a deviation: no new deviation alert until the
    # unit is back on route, is much further off, or this time has passed.
    quiet_until: float = 0.0
    quiet_off_m: float = 0.0


_watches: dict[str, Watch] = {}
_last_off: dict[str, float] = {}  # unit_id -> metres off its route at the last tick
decision_log: deque = deque(maxlen=LOG_SIZE)


def reset() -> None:
    _watches.clear()
    _last_off.clear()
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


def live_status(incident: dict, unit: Optional[dict]) -> Optional[dict]:
    """What the agent sees for this call right now: how far along the leg
    the unit is, how far off the route, and the modelled road time still to
    go (the leg's ETA scaled by the distance left, the same rule the alerts
    use). None when no unit is driving a leg."""
    leg, route, eta_s = _leg(incident)
    if unit is None or leg is None or len(route) < 2:
        return None
    pos = unit.get("position") or {}
    if "lat" not in pos or "lng" not in pos:
        return None
    point = {"lat": pos["lat"], "lng": pos["lng"]}
    off, index = project(route, point)
    _d, t = point_segment_m(point, route[index], route[index + 1])
    total = polyline_length(route) or 1.0
    done = polyline_length(route[: index + 1]) + _dist(route[index], route[index + 1]) * t
    remaining_m = max(0.0, total - done)
    watch = _watches.get(unit["unit_id"])
    return {
        "leg": leg,
        "off_route_m": round(off, 1),
        "progress": round(min(1.0, done / total), 3),
        "remaining_m": round(remaining_m),
        "remaining_s": round(eta_s * remaining_m / total) if eta_s else None,
        "fix_source": pos.get("source"),
        "fix_age_s": round(max(0.0, time.time() - pos["at"]), 1) if pos.get("at") else None,
        "watching": watch is not None,
        "open_alert": bool(watch and watch.alert_id),
        "provenance": "modelled",
    }


# --- analysis ---------------------------------------------------------------------

CONNECT_SPEED_MPS = 8.0  # about 30 km/h for the short hop to a route point


def _closed_between(points: list[dict], within: float = 10.0) -> bool:
    """Is a closed road on this short stretch?"""
    if not city_state.ready or not city_state.closed_edges:
        return False
    return any(_near_route(seg["coordinates"], points, within) for seg in city_state.closure_overlay())


def _disruptions_near(route: list[dict], within: float) -> dict:
    """Closures, accidents and the worst congestion on (or near) a polyline."""
    found = {"closed": [], "incident": [], "congested": None}
    if not city_state.ready or len(route) < 2:
        return found
    box = _box(route, within)
    for seg in city_state.closure_overlay():
        if _near_route(seg["coordinates"], route, within, box):
            found["closed"].append(seg)
    for seg in city_state.congestion_overlay(limit=600):
        if not _near_route(seg["coordinates"], route, within, box):
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
    if not counts:
        return "an unnamed road"
    # A named road wins over an unnamed one ("Trunk Link") when both appear.
    named = {r: n for r, n in counts.items() if _named(r)}
    pick = named or counts
    return readable_road(max(pick, key=pick.get))


def find_cause(remaining: list[dict], position: dict, trigger: str) -> dict:
    on_route = _disruptions_near(remaining, NEAR_ROUTE_M)
    if on_route["closed"]:
        road = main_road(on_route["closed"])
        return {"kind": "roadblock", "road": road, "likely": False,
                "text": f"road closed ahead on {road} ({len(on_route['closed'])} "
                        f"segment{'s' if len(on_route['closed']) != 1 else ''})"}
    if on_route["incident"]:
        road = main_road(on_route["incident"])
        return {"kind": "accident", "road": road, "likely": False,
                "text": f"accident reported on {road}"}
    nearby = _disruptions_near([position, position], CAUSE_RADIUS_M) if trigger == "deviation" else on_route
    if nearby["closed"]:
        seg = nearby["closed"][0]
        return {"kind": "roadblock", "road": readable_road(seg["road"]), "likely": True,
                "text": f"likely the closure on {readable_road(seg['road'])} nearby"}
    if nearby["incident"]:
        seg = nearby["incident"][0]
        return {"kind": "accident", "road": readable_road(seg["road"]), "likely": True,
                "text": f"likely the accident on {readable_road(seg['road'])} nearby"}
    worst = on_route["congested"] or nearby["congested"]
    if worst is not None:
        causes = city_state.explain(worst["edge_id"]).get("causes", [])
        dominant = max(causes, key=lambda c: c["multiplier"], default=None)
        why = _CAUSE_WORDS.get(dominant["cause"], dominant["cause"]) if dominant else "congestion"
        return {"kind": "congestion", "road": readable_road(worst["road"]), "likely": trigger == "deviation",
                "text": f"heavy congestion on {readable_road(worst['road'])} (x{worst['multiplier']:.2f}, from {why})"}
    return {"kind": "unexplained", "road": None, "likely": False,
            "text": "the unit left the planned route; no closure, accident or congestion found nearby"}


def _route_summary(path: dict, label: str) -> dict:
    return {"label": label, "coordinates": path["coordinates"], "eta_s": path["travel_time_s"],
            "distance_m": path["distance_m"], "roads": [r for r in path["roads"] if _named(r)][:4],
            "edge_ids": path["edge_ids"]}


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
    routes[0]["_from"] = start  # which start it left from (removed before publishing)
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
        routes.append(_route_summary(alt, label) | {"_from": start})
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
            out.append({"lat": coords[i]["lat"], "lng": coords[i]["lng"], "road": readable_road(name),
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


def _route_key(route: list[dict]) -> str:
    from agents.simulator import route_key

    return route_key(route)


def _trace(unit, off_m, trigger, cause, planned_s, new_eta, delay, routes, backup, recommendation, gain,
           routes_ms, cause_ms, backup_ms, total_ms) -> list[dict]:
    """The agent's loop for this alert, one step at a time, with the numbers
    each step used and how long it took (answers "what did it actually do?")."""
    pos = unit["position"]
    source = {"gps": "live GPS", "simulated": "simulated position"}.get(pos.get("source"), "station")
    steps = [
        {"step": "Observe", "text": f"{unit['unit_id']}: {source}, {off_m:.0f} m from its planned route."},
        {"step": "Detect", "text": (
            f"{OFF_ROUTE_FIXES} position fixes in a row more than {OFF_ROUTE_M:.0f} m off the route."
            if trigger == "deviation" else
            f"A closure or accident within {NEAR_ROUTE_M:.0f} m of the route ahead.")},
        {"step": "Explain", "text": f"Cause: {cause['text']}" + (" (likely, not certain)." if cause.get("likely") else "."),
         "ms": round(cause_ms)},
        {"step": "Predict", "text": (
            f"Planned time left {_minutes(planned_s)}; best route now {_minutes(new_eta)}"
            + (f" ({'+' if delay >= 0 else ''}{delay / 60:.1f} min)." if delay is not None else "."))},
        {"step": "Options", "text": (
            "; ".join(f"{r['label']} {_minutes(r['eta_s'])}" for r in routes) or "No road route from here.")
            + (f" Backup {backup['unit_id']} {_minutes(backup['eta_s'])}." if backup else " No backup available."),
         "ms": round(routes_ms + backup_ms)},
        {"step": "Advise", "text": {
            "dispatch_backup": (f"Send the backup: it saves {gain / 60:.1f} min (rule: at least "
                                f"{BACKUP_MIN_GAIN_S / 60:.0f} min and {BACKUP_MIN_GAIN_FRACTION:.0%})."
                                if gain is not None else "Send the backup: this unit cannot get through."),
            "reroute": "Keep this unit on the recommended route"
                       + (f" (a backup would save only {max(0.0, gain) / 60:.1f} min)." if gain is not None else "."),
            "hold": "Hold this unit and clear the road: no route and no backup.",
        }[recommendation]},
    ]
    steps.append({"step": "Total", "text": f"Worked out in {total_ms / 1000:.2f} s.", "ms": round(total_ms)})
    return steps


def _delay_line(delay: float, new_eta: Optional[float]) -> str:
    """The delay against the plan, worded honestly in either direction."""
    if delay >= 30:
        return f"Delay: +{delay / 60:.1f} min; new ETA {_minutes(new_eta)} from now."
    if delay <= -30:
        return (f"No delay: the new route is {-delay / 60:.1f} min faster than the plan; "
                f"new ETA {_minutes(new_eta)} from now.")
    return f"No delay against the plan; new ETA {_minutes(new_eta)} from now."


# OpenStreetMap roads with no name or number get their class as a name
# ("Trunk Link", "Tertiary") when the graph is built; say what they are.
_ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}


def readable_road(name: Optional[str]) -> str:
    if not name:
        return "an unnamed road"
    words = name.lower().split()
    if words and words[0] in _ROAD_CLASSES and set(words[1:]) <= {"link"}:
        if "link" in words:
            return "an unnamed slip road"
        return "an unnamed main road" if words[0] in {"motorway", "trunk", "primary"} else "an unnamed side road"
    return name


def _named(name: Optional[str]) -> bool:
    return readable_road(name) == name


def _minutes(seconds: Optional[float]) -> str:
    return "unknown" if seconds is None else f"{seconds / 60:.1f} min"


def analyse(incident: dict, unit: dict, leg: str, route: list[dict], eta_s: Optional[float],
            progress: int, trigger: str) -> Optional[dict]:
    started = time.perf_counter()
    position = {"lat": unit["position"]["lat"], "lng": unit["position"]["lng"]}
    off_now = project(route, position)[0]
    remaining = [position] + route[progress + 1:]
    destination = route[-1]
    # Where to search from. On its route, the unit is on a known carriageway:
    # start from the route's own road nodes (its position between them can
    # snap to the opposite carriageway of a divided road, which leads
    # nowhere). Off its route, start from where it actually is.
    behind = list(reversed(route[max(0, progress - 4):progress + 1]))
    hop_corridor = None
    if trigger == "deviation":
        routes = alternatives(position, destination, behind)
    else:
        ahead = route[progress + 1:progress + 2]
        # The next route point is a start only if the unit can drive there:
        # not when the closure lies between it and the unit, or a path from
        # the far side would look usable when it is not.
        if ahead and _closed_between([position, ahead[0]]):
            ahead = []
        starts = behind[:1] + ahead + behind[1:] + [position]
        routes = alternatives(starts[0], destination, starts[1:])
        if routes and ahead and routes[0].get("_from") is ahead[0]:
            # Include the drive to that point in the route and its ETA (the
            # route's points then lead one ahead of its edges, so the
            # green-corridor junctions are worked out before this).
            hop = _dist(position, ahead[0])
            hop_corridor = corridor(routes[0])
            for r in routes:
                r["coordinates"] = [position] + list(r["coordinates"])
                r["distance_m"] = r["distance_m"] + hop
                r["eta_s"] = r["eta_s"] + hop / CONNECT_SPEED_MPS
            for j in hop_corridor:
                j["eta_min"] = round(j["eta_min"] + hop / CONNECT_SPEED_MPS / 60, 1)
    routes_ms = (time.perf_counter() - started) * 1000
    total = polyline_length(route) or 1.0
    planned_remaining = eta_s * polyline_length(remaining) / total if eta_s else None
    # No route at all from where the unit is (say, a closed one-way flyover):
    # it cannot get there, so the only useful advice is a backup.
    new_eta = routes[0]["eta_s"] if routes else None
    delay = new_eta - planned_remaining if (planned_remaining is not None and new_eta is not None) else None
    mark = time.perf_counter()
    cause = find_cause(remaining, position, trigger)
    cause_ms = (time.perf_counter() - mark) * 1000

    mark = time.perf_counter()
    backup = best_backup(unit, destination) if leg == "to_scene" else None
    backup_ms = (time.perf_counter() - mark) * 1000
    if backup and new_eta is None:
        recommend_backup = True
        gain = None
    else:
        gain = (new_eta - backup["eta_s"]) if backup else None
        recommend_backup = bool(backup) and gain >= BACKUP_MIN_GAIN_S and gain / new_eta >= BACKUP_MIN_GAIN_FRACTION
    recommendation = "dispatch_backup" if recommend_backup else ("reroute" if routes else "hold")
    green = hop_corridor if hop_corridor is not None else (corridor(routes[0]) if routes else [])

    where = "to the scene" if leg == "to_scene" else "to the hospital"
    first = (f"{unit['unit_id']} left its planned route" if trigger == "deviation"
             else f"{unit['unit_id']}'s planned route is blocked ahead")
    lines = [
        f"{first} on the way {where}.",
        f"Cause: {cause['text']}.",
        (_delay_line(delay, new_eta)
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
        lines.append(f"Green-corridor request: {len(green)} junction{'s' if len(green) != 1 else ''} on the new route, first in "
                     f"{green[0]['eta_min']:.1f} min (advice for traffic police; no live signal control).")

    return {
        "alert_id": uuid.uuid4().hex[:12],
        "at": time.time(),
        "status": "open",
        "unit_id": unit["unit_id"],
        "leg": leg,
        "destination": {"lat": destination["lat"], "lng": destination["lng"]},
        "route_key": _route_key(route),  # the route this was worked out on
        "trigger": trigger,
        "position": position,
        "cause": cause,
        "planned_remaining_s": planned_remaining,
        "new_eta_s": new_eta,
        "delay_s": delay,
        "routes": [{k: v for k, v in r.items() if k not in ("edge_ids", "_from")} for r in routes],
        "backup": ({k: v for k, v in backup.items() if k != "route"}
                   | {"route": {k: v for k, v in backup["route"].items() if k != "edge_ids"}}) if backup else None,
        "recommendation": recommendation,
        "corridor": green,
        "explanation": " ".join(lines),
        "trace": _trace(unit, off_now, trigger, cause, planned_remaining, new_eta, delay, routes, backup,
                        recommendation, gain, routes_ms, cause_ms, backup_ms,
                        (time.perf_counter() - started) * 1000),
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
            _last_off.pop(unit["unit_id"], None)
            continue
        leg, route, eta_s = _leg(incident)
        if leg is None or len(route) < 2:
            continue
        watch = _watches.setdefault(unit["unit_id"], Watch())
        from agents.simulator import route_key

        leg_key = f"{incident['incident_id']}:{leg}:{route_key(route)}"
        if watch.leg_key != leg_key:
            # A new leg (or an accepted reroute): start watching it afresh.
            new_leg = watch.leg_key.rsplit(":", 1)[0] != leg_key.rsplit(":", 1)[0]
            handled = watch.city_version
            watch.leg_key, watch.progress, watch.off_count, watch.alert_id = leg_key, 0, 0, None
            watch.alert_city_version = ()
            watch.quiet_until = 0.0  # a dismissal was for the old route
            # A reroute of the same leg (dispatch accepted a route around the
            # closure) keeps the city state already handled: the closure it
            # was routed around must not raise the same alert again. A new
            # leg, or a city change after this, is checked afresh.
            watch.city_version = () if new_leg else handled
            if new_leg:
                # Only a new call or leg decides whether a phone unit is
                # watched for deviations; a reroute of the same leg keeps it.
                pos = unit["position"]
                watch.started_near_station = (pos.get("source") != "gps"
                                              or _dist(pos, route[0]) <= 300)
        pos = unit["position"]
        point = {"lat": pos["lat"], "lng": pos["lng"]}
        off, index = project(route, point, watch.progress)
        _last_off[unit["unit_id"]] = off
        accuracy = pos.get("accuracy_m") or 0.0
        usable = accuracy <= MAX_ACCURACY_M and watch.started_near_station
        fresh = pos.get("at") is None or pos.get("at") != watch.last_fix_at
        watch.last_fix_at = pos.get("at")
        if off <= OFF_ROUTE_M + accuracy:
            watch.progress = max(watch.progress, index)
            watch.off_count = 0
            watch.quiet_until = 0.0  # back on route: a new deviation counts again
        elif usable and fresh:
            # Three distinct fixes, not one fix seen on three ticks (a phone
            # that sent one bad fix and then went quiet).
            watch.off_count += 1

        if watch.alert_id is not None:
            # One open alert per unit. The city state is not marked as seen
            # meanwhile, so a new closure is still checked once it is handled.
            continue
        trigger = None
        quiet = time.time() < watch.quiet_until and off < watch.quiet_off_m + DISMISS_FURTHER_M
        if watch.off_count >= OFF_ROUTE_FIXES and not quiet:
            trigger = "deviation"
        elif watch.city_version != version:
            watch.city_version = version
            ahead = _disruptions_near(route[watch.progress:], NEAR_ROUTE_M)
            if ahead["closed"] or ahead["incident"]:
                trigger = "blocked_ahead"
        if trigger is None:
            continue
        alert = analyse(incident, unit, leg, route, eta_s, watch.progress, trigger)
        if alert is None:
            continue
        watch.alert_id = alert["alert_id"]
        watch.city_version = watch.alert_city_version = version
        _log(incident["incident_id"], unit["unit_id"], trigger, alert["explanation"],
             alert_id=alert["alert_id"], recommendation=alert["recommendation"])
        logger.info("Monitoring alert %s for %s: %s", alert["alert_id"], unit["unit_id"], trigger)
        new_alerts.append((incident["incident_id"], alert))
    return new_alerts


def clear_alert(unit_id: str, dismissed_deviation: bool = False) -> None:
    """The dispatcher acted on (or dismissed) the unit's alert; watch again.

    dismissed_deviation: dispatch saw the unit off its route and chose to
    leave it. The same deviation then stays quiet for DISMISS_QUIET_S, unless
    the unit gets DISMISS_FURTHER_M further off (or rejoins the route)."""
    watch = _watches.get(unit_id)
    if watch and dismissed_deviation:
        watch.quiet_until = time.time() + DISMISS_QUIET_S
        watch.quiet_off_m = _last_off.get(unit_id, 0.0)
    if watch:
        watch.alert_id = None
        watch.off_count = 0
        # Handled: the city state the alert was about. If the city changed
        # since (a second closure), the next tick checks the route again.
        watch.city_version = watch.alert_city_version or _city_version()
