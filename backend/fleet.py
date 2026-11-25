"""The fleet: units, their status, assignment and live positions.

Units come from the contacts directory (ids, types, stations; never crew phone
numbers here). Each unit is ``available`` at its station or ``assigned`` to one
incident. Status and assignment are stored on every change, so a restart keeps
them. Positions and trails are kept in memory only: a GPS fix every 2 s must
not mean a database write every 2 s, and an old trail is not worth keeping
across a restart.

A position comes from one of three sources, always recorded with the fix:
``station`` (parked), ``gps`` (the crew's phone or tracker) or ``simulated``
(the demo simulator, time-compressed and labelled as such on screen).

A unit whose phone has sent a fix on its current call is a real vehicle for
the rest of that call: the simulator never moves it, even when its phone
goes quiet (a locked screen, a tunnel). Screens then show how old its last
fix is instead of inventing movement. When the call ends it stays where it
last was, not back at its station.
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
GPS_FRESH_S = 15.0          # a phone fix older than this counts as "signal lost"
MAX_FIX_AGE_S = 120.0       # a fix captured longer ago than this is not current
MAX_CLOCK_AHEAD_S = 30.0    # a phone clock this far ahead is clamped to now
SAVE_REAL_POSITION_S = 30.0  # a real vehicle's position is stored this often

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


def _park(u: dict) -> None:
    """A unit leaving its call: a real vehicle stays where its phone last put
    it; a simulated one goes back to its station."""
    if u.get("gps_incident") is None or u["position"].get("source") != "gps":
        u["position"] = {**u["station_location"], "at": time.time(), "source": "station", "accuracy_m": None}
    u["gps_incident"] = None


def _station_location(unit_type: str, station: str) -> Optional[dict]:
    from agents.route_optimization import EMERGENCY_HUBS

    for hubs in (EMERGENCY_HUBS.get(unit_type, []), *EMERGENCY_HUBS.values()):
        for hub in hubs:
            if hub["name"] == station:
                return {"lat": hub["lat"], "lng": hub["lng"]}
    return None


def _save(strict: bool = False) -> None:
    """Store assignments. strict: raise on failure, so a caller that gave a
    unit to a call can undo it instead of reporting a change that a restart
    would lose (the unit could then be sent to a second call)."""
    if not _loaded:
        return  # never overwrite stored assignments with an unloaded fleet
    snapshot = {}
    for uid, u in _units.items():
        snapshot[uid] = {k: u.get(k) for k in ("status", "incident_id", "gps_incident")}
        snapshot[uid]["on_duty"] = u.get("on_duty", True)
        if u.get("gps_incident") and u["position"].get("source") == "gps":
            # A real vehicle's last place, so a restart does not show it at
            # its station (saved at most every SAVE_REAL_POSITION_S).
            snapshot[uid]["position"] = {k: u["position"].get(k) for k in
                                         ("lat", "lng", "at", "source", "accuracy_m", "heading", "captured_at")}
    try:
        storage.save_collection(_COLLECTION, snapshot)
    except Exception:
        logger.exception("Could not store fleet state")
        if strict:
            raise


def load(attempts: int = 3) -> None:
    """(Re)build the fleet from the directory, keeping stored assignments."""
    global _loaded, _load_failed_at
    with _lock:
        stored = None
        for attempt in range(attempts):
            try:
                stored = storage.load_collection(_COLLECTION, {}) or {}
                break
            except Exception:
                logger.exception("Could not read fleet state (attempt %d of %d)", attempt + 1, attempts)
                if attempt + 1 < attempts:
                    time.sleep(0.2)
        if stored is None:
            # Not an empty fleet: unknown assignments. Marking every unit
            # free would let one go to two calls, so the fleet stays unloaded
            # (no unit is handed out) and the next use tries the read again.
            logger.error("Fleet state unreadable; units are unavailable until it can be read")
            _load_failed_at = time.monotonic()
            return
        _load_failed_at = None
        units: dict[str, dict] = {}
        for entry in contacts.directory().units:
            station = _station_location(entry.type, entry.station)
            if station is None:
                logger.warning("Unit %s: unknown station; skipped", entry.unit_id)
                continue
            previous = _units.get(entry.unit_id) or {}
            saved = stored.get(entry.unit_id) or {}
            real = bool(saved.get("gps_incident")) and saved.get("gps_incident") == saved.get("incident_id")
            units[entry.unit_id] = {
                "unit_id": entry.unit_id,
                "type": entry.type,
                "station": entry.station,
                "station_location": station,
                "status": saved.get("status", "available"),
                "incident_id": saved.get("incident_id"),
                # A real vehicle stays real across a restart (the simulator
                # must not start driving it before its phone's next fix).
                "gps_incident": saved.get("gps_incident") if real else None,
                # Off duty: shown on the map, never dispatched.
                "on_duty": saved.get("on_duty", True) is not False,
                "position": previous.get("position") or (
                    saved["position"] if real and isinstance(saved.get("position"), dict)
                    and {"lat", "lng", "at"} <= set(saved["position"]) else None
                ) or {**station, "at": time.time(), "source": "station", "accuracy_m": None},
            }
            _trails.setdefault(entry.unit_id, deque(maxlen=TRAIL_POINTS))
        _units.clear()
        _units.update(units)
        _loaded = True


_load_failed_at = None  # monotonic time of the last failed read, if it failed
RELOAD_EVERY_S = 10.0


# Set at startup (main.py): the ids of calls a unit is driving. After a
# delayed load (the startup read failed), units stored against calls that
# were cleared meanwhile are freed before any unit is handed out.
active_calls = None


def _ensure() -> None:
    if _loaded:
        return
    if _load_failed_at is None:
        load()
        return
    if time.monotonic() - _load_failed_at >= RELOAD_EVERY_S:
        # After a failed read: one quick retry at most every RELOAD_EVERY_S,
        # so requests never wait out repeated retries while storage is down.
        # Load and reconcile under one hold of the (re-entrant) lock, so no
        # unit is handed out between them and then freed by the reconcile.
        with _lock:
            # Re-check under the lock: another thread may have recovered
            # (and reconciled) while this one waited; never repeat that.
            if _loaded or _load_failed_at is None or time.monotonic() - _load_failed_at < RELOAD_EVERY_S:
                return
            load(attempts=1)
            if _loaded and active_calls is not None:
                try:
                    reconcile(active_calls())
                except Exception:
                    logger.exception("Could not reconcile the fleet after a delayed load")


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


def assign(incident_id: str, unit_type: str, near: dict, prefer_station: Optional[str] = None,
           exclude: Optional[set] = None) -> Optional[dict]:
    """Give the call a free unit: its planned station's first, else the nearest
    free unit of that type. Returns the unit, or None when none is free."""
    _ensure()
    with _lock:
        current = next((u for u in _units.values() if u["incident_id"] == incident_id), None)
        if current is not None:
            return dict(current)
        free = [u for u in _units.values() if u["type"] == unit_type and u["status"] == "available"
                and u.get("on_duty", True) and u["unit_id"] not in (exclude or ())]
        if not free:
            return None
        preferred = [u for u in free if prefer_station and u["station"] == prefer_station]
        chosen = preferred[0] if preferred else min(free, key=lambda u: _haversine(u["position"], near))
        chosen["status"] = "assigned"
        chosen["incident_id"] = incident_id
        try:
            _save(strict=True)
        except Exception:
            chosen["status"], chosen["incident_id"] = "available", None
            raise
        _trails[chosen["unit_id"]].clear()
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
                u.update(status="available", incident_id=None)
                _park(u)
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
        old = [u for u in _units.values() if u["incident_id"] == incident_id]
        before = {u["unit_id"]: (u["status"], u["incident_id"]) for u in (*old, new)}
        for u in old:
            u.update(status="available", incident_id=None)
        new["status"] = "assigned"
        new["incident_id"] = incident_id
        try:
            _save(strict=True)
        except Exception:
            for uid, (status_, incident) in before.items():
                _units[uid].update(status=status_, incident_id=incident)
            raise
        for u in old:  # only once stored: back at its station (or where it is)
            _park(u)
            _trails[u["unit_id"]].clear()
        _trails[new_unit_id].clear()
        return dict(new)


def put_back(unit_id: str, position: dict) -> None:
    """Restore a unit's last position (undoing a swap that could not be stored)."""
    _ensure()
    with _lock:
        if unit_id in _units:
            _units[unit_id]["position"] = dict(position)


def release(incident_id: str) -> None:
    """Free every unit on this call and park it back at its station."""
    _ensure()
    with _lock:
        changed = False
        for u in _units.values():
            if u["incident_id"] == incident_id:
                u.update(status="available", incident_id=None)
                _park(u)
                _trails[u["unit_id"]].clear()
                changed = True
        if changed:
            _save()


class StaleFix(Exception):
    """A fix older than the unit's last one (delivered out of order) or
    captured too long ago to be its position now. Not an error for the
    phone: it is simply not used."""


def record_position(unit_id: str, lat: float, lng: float, *, source: str,
                    accuracy_m: Optional[float] = None, heading: Optional[float] = None,
                    at: Optional[float] = None, captured_at: Optional[float] = None,
                    speed_mps: Optional[float] = None) -> dict:
    """Store one fix. Raises KeyError for an unknown unit, ValueError when a
    unit posts faster than MIN_FIX_INTERVAL_S, and StaleFix for a fix that
    is out of order or too old. `captured_at` is when the phone took the fix
    (its own clock); without it, the time it arrived is used."""
    _ensure()
    now = at or time.time()
    if source == "gps" and captured_at is not None:
        if captured_at < now - MAX_FIX_AGE_S:
            raise StaleFix("captured too long ago")
        # A phone clock running ahead cannot put a fix in the future.
        captured_at = min(captured_at, now) if captured_at <= now + MAX_CLOCK_AHEAD_S else now
    with _lock:
        u = _units[unit_id]
        last = u["position"]
        if source == "gps" and last.get("source") == "gps":
            if captured_at is not None and captured_at <= last.get("captured_at", 0):
                raise StaleFix("older than the last fix")
            if now - last.get("at", 0) < MIN_FIX_INTERVAL_S:
                raise ValueError("too many fixes")
        if source == "simulated" and real_vehicle(u):
            # A real vehicle on this call: never invent its movement.
            return dict(last)
        fix = {"lat": lat, "lng": lng, "at": now, "source": source,
               "accuracy_m": accuracy_m, "heading": heading}
        if source == "gps":
            fix["captured_at"] = captured_at if captured_at is not None else now
            if speed_mps is not None:
                fix["speed_mps"] = speed_mps
        u["position"] = fix
        if source == "gps" and u.get("incident_id"):
            first = u.get("gps_incident") != u["incident_id"]
            u["gps_incident"] = u["incident_id"]
            if first or now - u.get("saved_at", 0) >= SAVE_REAL_POSITION_S:
                # So a restart still knows it is real, and roughly where.
                u["saved_at"] = now
                _save()
        _trails[unit_id].append({"lat": lat, "lng": lng, "at": now, "source": source})
        return dict(fix)


def trail(unit_id: str) -> list[dict]:
    _ensure()
    with _lock:
        return list(_trails.get(unit_id, ()))


def set_duty(unit_id: str, on_duty: bool) -> dict:
    """Put a unit on or off duty. Raises KeyError for an unknown unit and
    ValueError for a unit on a call (it finishes the call first)."""
    _ensure()
    with _lock:
        u = _units[unit_id]
        if not on_duty and u["status"] != "available":
            raise ValueError("on a call")
        before = u.get("on_duty", True)
        u["on_duty"] = bool(on_duty)
        try:
            _save(strict=True)
        except Exception:
            u["on_duty"] = before
            raise
        return dict(u, position=dict(u["position"]))


def real_vehicle(u: Optional[dict]) -> bool:
    """True when this unit's phone has sent a fix on its current call: it is
    a real vehicle until the call ends, whether or not its phone is sending
    right now."""
    return bool(u) and u.get("incident_id") is not None and u.get("gps_incident") == u.get("incident_id")


def phone_tracking(unit_id: str) -> bool:
    """True while the unit's phone is sending fixes."""
    u = get(unit_id)
    return bool(u) and u["position"].get("source") == "gps" and time.time() - u["position"].get("at", 0) < GPS_FRESH_S


def reset() -> None:
    """Tests and the demo reset: every unit available at its station."""
    with _lock:
        if not _loaded:
            # Assignments unknown (unreadable): saving would overwrite the
            # stored ones with "all free". Leave them for the next load.
            return
        for u in _units.values():
            u.update(status="available", incident_id=None, gps_incident=None, on_duty=True,
                     position={**u["station_location"], "at": time.time(), "source": "station", "accuracy_m": None})
        for t in _trails.values():
            t.clear()
        _save()
