"""Fleet and live tracking API.

- ``GET  /api/units``: the whole fleet with positions (dispatch only).
- ``GET  /api/units/{unit_id}``: one unit (that unit's crew, or dispatch).
- ``POST /api/units/{unit_id}/position``: a GPS fix from that unit's own
  signed-in crew. The unit comes from the session, so nobody else can move it.
- ``GET  /api/incidents/{incident_id}/track``: the unit on a call, its trail
  and the planned route, for the caller and staff screens. No crew details.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import fleet
from agents import monitor, simulator
from staff import require as require_staff

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Fleet"])

# Fixes outside Bengaluru's service area are refused (same box as reports).
_MAX_ACCURACY_M = 5000.0


class PositionFix(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy_m: Optional[float] = Field(default=None, ge=0, le=_MAX_ACCURACY_M, allow_inf_nan=False)
    heading: Optional[float] = Field(default=None, ge=0, lt=360, allow_inf_nan=False)
    # When the phone took the fix (epoch milliseconds, as the browser's
    # GeolocationPosition.timestamp gives it). Orders fixes that arrive out
    # of order and drops ones delayed in a queue.
    captured_at_ms: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    speed_mps: Optional[float] = Field(default=None, ge=0, le=120, allow_inf_nan=False)


_STALE_NOTES = {
    "captured too long ago": "captured too long ago",
    "older than the last fix": "older than the last fix",
}


def store_fix(unit_id: str, lat: float, lng: float, *, accuracy_m=None, heading=None,
              captured_at=None, speed_mps=None) -> dict:
    """Validate and store one real GPS fix (phone screen or tracker app).
    Returns the response body; raises HTTPException for a bad fix."""
    from routers.incident import in_service_area

    if not in_service_area(lat, lng):
        raise HTTPException(status_code=422, detail="Position is outside the service area")
    try:
        stored = fleet.record_position(unit_id, lat, lng, source="gps", accuracy_m=accuracy_m,
                                       heading=heading, captured_at=captured_at, speed_mps=speed_mps)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown unit") from None
    except fleet.StaleFix as reason:
        # Not the phone's fault (a delayed or reordered upload): accepted,
        # not used, so it does not retry it. Only fixed notes reach the client;
        # the exception text is logged here instead.
        logger.info("Fix for unit %s not used: %s", unit_id, reason)
        return {"unit_id": unit_id, "ignored": _STALE_NOTES.get(str(reason), "not used")}
    except ValueError:
        raise HTTPException(status_code=429, detail="Too many position updates",
                            headers={"Retry-After": "1"}) from None
    return {"unit_id": unit_id, "position": stored}


def _public(unit: dict) -> dict:
    """What tracking screens may show: no crew names or phone numbers."""
    return {**{k: unit[k] for k in ("unit_id", "type", "station", "status", "incident_id", "position")},
            "on_duty": unit.get("on_duty", True)}


class DutyChange(BaseModel):
    on_duty: bool


@router.get("/api/units")
async def list_units(request: Request = None):
    require_staff(request, "dispatcher")
    return {"units": [_public(u) for u in fleet.units()]}


@router.get("/api/stations")
async def list_stations():
    """Every emergency station (ambulance, fire, police, rescue) for the map.
    Public facts: names and places, nothing about crews."""
    from agents.route_optimization import EMERGENCY_HUBS

    return {"stations": [{"id": hub["id"], "name": hub["name"], "type": unit_type,
                          "lat": hub["lat"], "lng": hub["lng"]}
                         for unit_type, hubs in EMERGENCY_HUBS.items() for hub in hubs]}


@router.post("/api/units/{unit_id}/duty")
async def set_duty(unit_id: str, change: DutyChange, request: Request = None):
    """On or off duty: the unit's own crew, or dispatch. An off-duty unit
    stays on the map and is never dispatched."""
    import security
    import staff

    if not change.on_duty and not staff.enabled():
        # Open demo: a visitor may put a unit on duty, never take one off.
        if request is None or not security.authenticated(request):
            raise HTTPException(status_code=401, detail="Taking a unit off duty needs the operator sign-in")
    else:
        try:
            require_staff(request, "unit", unit_id)
        except HTTPException:
            require_staff(request, "dispatcher")
    try:
        unit = fleet.set_duty(unit_id, change.on_duty)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown unit") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="This unit is on a call; it can go off duty once the call ends") from None
    return _public(unit)


@router.get("/api/units/{unit_id}")
async def get_unit(unit_id: str, request: Request = None):
    unit = fleet.get(unit_id)
    try:
        require_staff(request, "unit", unit_id)
    except HTTPException:
        require_staff(request, "dispatcher")
    if unit is None:
        raise HTTPException(status_code=404, detail="Unknown unit")
    return {**_public(unit), "trail": fleet.trail(unit_id)}


@router.post("/api/units/{unit_id}/position")
async def post_position(unit_id: str, fix: PositionFix, request: Request = None):
    require_staff(request, "unit", unit_id)
    captured = fix.captured_at_ms / 1000.0 if fix.captured_at_ms is not None else None
    return store_fix(unit_id, fix.lat, fix.lng, accuracy_m=fix.accuracy_m, heading=fix.heading,
                     captured_at=captured, speed_mps=fix.speed_mps)


@router.get("/api/incidents/{incident_id}/track")
async def track_incident(incident_id: str):
    from routers.incident import get_incident

    incident = await get_incident(incident_id)  # 404 when unknown
    view = incident.get("citizen_view") or {}
    unit = fleet.unit_for_incident(incident_id)
    return {
        "incident_id": incident_id,
        "unit": _public(unit) if unit else None,
        "trail": fleet.trail(unit["unit_id"]) if unit else [],
        "planned": {
            "to_scene": view.get("phase1_route_coordinates") or [],
            "to_hospital": view.get("route_coordinates") or [],
        },
        "stage": incident.get("driver_stage"),
        "status": incident.get("status"),
        "sim_speed": simulator.sim_speed(),
        # One live remaining time for every screen, from where the unit is.
        "live": monitor.live_status(incident, unit),
    }
