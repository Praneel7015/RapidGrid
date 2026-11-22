"""The fleet: units, their status, assignment and live positions.

Units come from the contacts directory (ids, types, stations; never crew phone
numbers here). Each unit is ``available`` at its station or ``assigned`` to one
incident. Status and assignment are stored on every change, so a restart keeps
them. Positions and trails are kept in memory only: a GPS fix every 2 s must
not mean a database write every 2 s, and an old trail is not worth keeping
across a restart.

A position comes from one of three sources, always recorded with the fix:
``station`` (parked), ``gps`` (the crew's phone) or ``simulated`` (the demo
simulator, time-compressed and labelled as such on screen).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Optional

import contacts
import storage

logger = logging.getLogger("geoagentic.fleet")

_COLLECTION = "fleet:state"
TRAIL_POINTS = 300          # about 10 minutes at one fix every 2 s
MIN_FIX_INTERVAL_S = 1.0    # a unit may not post faster than this
GPS_FRESH_S = 15.0          # a phone fix newer than this beats the simulator

_lock = threading.RLock()
_units: dict[str, dict] = {}
_trails: dict[str, deque] = {}
_loaded = False


def _haversine(a: dict, b: dict) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lng"] - a["lng"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _station_location(unit_type: str, station: str) -> Optional[dict]:
    from agents.route_optimization import EMERGENCY_HUBS

    for hubs in (EMERGENCY_HUBS.get(unit_type, []), *EMERGENCY_HUBS.values()):
        for hub in hubs:
            if hub["name"] == station:
                return {"lat": hub["lat"], "lng": hub["lng"]}
    return None


def _save() -> None:
    snapshot = {uid: {k: u[k] for k in ("status", "incident_id")} for uid, u in _units.items()}
    try:
        storage.save_collection(_COLLECTION, snapshot)
    except Exception:
        logger.exception("Could not store fleet state")


def load() -> None:
    """(Re)build the fleet from the directory, keeping stored assignments."""
    global _loaded
    with _lock:
        try:
            stored = storage.load_collection(_COLLECTION, {}) or {}
        except Exception:
            logger.exception("Could not read fleet state; starting all units available")
            stored = {}
        units: dict[str, dict] = {}
        for entry in contacts.directory().units:
            station = _station_location(entry.type, entry.station)
            if station is None:
                logger.warning("Unit %s: unknown station; skipped", entry.unit_id)
                continue
            previous = _units.get(entry.unit_id) or {}
            saved = stored.get(entry.unit_id) or {}
            units[entry.unit_id] = {
                "unit_id": entry.unit_id,
                "type": entry.type,
                "station": entry.station,
                "station_location": station,
                "status": saved.get("status", "available"),
                "incident_id": saved.get("incident_id"),
                "position": previous.get("position") or {**station, "at": time.time(), "source": "station",
                                                         "accuracy_m": None},
            }
            _trails.setdefault(entry.unit_id, deque(maxlen=TRAIL_POINTS))
        _units.clear()
        _units.update(units)
        _loaded = True


def _ensure() -> None:
    if not _loaded:
        load()


def units() -> list[dict]:
    _ensure()
    with _lock:
        return [dict(u, position=dict(u["position"])) for u in _units.values()]


def get(unit_id: str) -> Optional[dict]:
    _ensure()
    with _lock:
        u = _units.get(unit_id)
        return dict(u, position=dict(u["position"])) if u else None


def unit_for_incident(incident_id: str) -> Optional[dict]:
    _ensure()
    with _lock:
        for u in _units.values():
            if u["incident_id"] == incident_id:
                return dict(u, position=dict(u["position"]))
    return None


def assign(incident_id: str, unit_type: str, near: dict, prefer_station: Optional[str] = None) -> Optional[dict]:
    """Give the call a free unit: its planned station's first, else the nearest
    free unit of that type. Returns the unit, or None when none is free."""
    _ensure()
    with _lock:
        current = next((u for u in _units.values() if u["incident_id"] == incident_id), None)
        if current is not None:
            return dict(current)
        free = [u for u in _units.values() if u["type"] == unit_type and u["status"] == "available"]
        if not free:
            return None
        preferred = [u for u in free if prefer_station and u["station"] == prefer_station]
        chosen = preferred[0] if preferred else min(free, key=lambda u: _haversine(u["position"], near))
        chosen["status"] = "assigned"
        chosen["incident_id"] = incident_id
        _trails[chosen["unit_id"]].clear()
        _save()
        logger.info("Unit %s assigned to %s", chosen["unit_id"], incident_id)
        return dict(chosen)


def reconcile(active_incident_ids: set) -> list[str]:
    """Free units whose call no longer exists or has finished (after a
    restart, or a clear that happened while the fleet was not loaded)."""
    _ensure()
    freed = []
    with _lock:
        for u in _units.values():
            if u["incident_id"] and u["incident_id"] not in active_incident_ids:
                freed.append(u["unit_id"])
        if freed:
            for uid in freed:
                u = _units[uid]
                u.update(status="available", incident_id=None,
                         position={**u["station_location"], "at": time.time(), "source": "station",
                                   "accuracy_m": None})
                _trails[uid].clear()
            _save()
    if freed:
        logger.info("Freed %d unit(s) with no active call", len(freed))
    return freed


def swap(incident_id: str, new_unit_id: str) -> dict:
    """Hand the call to another free unit in one step: the new unit is checked
    first, and the old one is released only if the new one is taken. Raises
    ValueError (nothing changed) when the new unit cannot take the call."""
    _ensure()
    with _lock:
        new = _units.get(new_unit_id)
        if new is None or new["status"] != "available":
            raise ValueError("unit not free")
        for u in _units.values():
            if u["incident_id"] == incident_id:
                u.update(status="available", incident_id=None,
                         position={**u["station_location"], "at": time.time(), "source": "station",
                                   "accuracy_m": None})
                _trails[u["unit_id"]].clear()
        new["status"] = "assigned"
        new["incident_id"] = incident_id
        _trails[new_unit_id].clear()
        _save()
        return dict(new)


def release(incident_id: str) -> None:
    """Free every unit on this call and park it back at its station."""
    _ensure()
    with _lock:
        changed = False
        for u in _units.values():
            if u["incident_id"] == incident_id:
                u.update(status="available", incident_id=None,
                         position={**u["station_location"], "at": time.time(), "source": "station",
                                   "accuracy_m": None})
                _trails[u["unit_id"]].clear()
                changed = True
        if changed:
            _save()


def record_position(unit_id: str, lat: float, lng: float, *, source: str,
                    accuracy_m: Optional[float] = None, heading: Optional[float] = None,
                    at: Optional[float] = None) -> dict:
    """Store one fix. Raises KeyError for an unknown unit and ValueError when a
    unit posts faster than MIN_FIX_INTERVAL_S."""
    _ensure()
    now = at or time.time()
    with _lock:
        u = _units[unit_id]
        last = u["position"]
        if source == "gps" and last.get("source") == "gps" and now - last.get("at", 0) < MIN_FIX_INTERVAL_S:
            raise ValueError("too many fixes")
        # A recent phone fix wins over the simulator for that unit.
        if source == "simulated" and last.get("source") == "gps" and now - last.get("at", 0) < GPS_FRESH_S:
            return dict(last)
        fix = {"lat": lat, "lng": lng, "at": now, "source": source,
               "accuracy_m": accuracy_m, "heading": heading}
        u["position"] = fix
        _trails[unit_id].append({"lat": lat, "lng": lng, "at": now, "source": source})
        return dict(fix)


def trail(unit_id: str) -> list[dict]:
    _ensure()
    with _lock:
        return list(_trails.get(unit_id, ()))


def phone_tracking(unit_id: str) -> bool:
    """True while the unit's phone is sending fixes."""
    u = get(unit_id)
    return bool(u) and u["position"].get("source") == "gps" and time.time() - u["position"].get("at", 0) < GPS_FRESH_S


def reset() -> None:
    """Tests and the demo reset: every unit available at its station."""
    with _lock:
        for u in _units.values():
            u.update(status="available", incident_id=None,
                     position={**u["station_location"], "at": time.time(), "source": "station", "accuracy_m": None})
        for t in _trails.values():
            t.clear()
        _save()
