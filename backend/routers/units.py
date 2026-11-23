"""Fleet and live tracking API.

- ``GET  /api/units``: the whole fleet with positions (dispatch only).
- ``GET  /api/units/{unit_id}``: one unit (that unit's crew, or dispatch).
- ``POST /api/units/{unit_id}/position``: a GPS fix from that unit's own
  signed-in crew. The unit comes from the session, so nobody else can move it.
- ``GET  /api/incidents/{incident_id}/track``: the unit on a call, its trail
  and the planned route, for the caller and staff screens. No crew details.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import fleet
from agents import monitor, simulator
from staff import require as require_staff

router = APIRouter(tags=["Fleet"])

# Fixes outside Bengaluru's service area are refused (same box as reports).
_MAX_ACCURACY_M = 5000.0


class PositionFix(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy_m: Optional[float] = Field(default=None, ge=0, le=_MAX_ACCURACY_M, allow_inf_nan=False)
    heading: Optional[float] = Field(default=None, ge=0, lt=360, allow_inf_nan=False)


def _public(unit: dict) -> dict:
    """What tracking screens may show: no crew names or phone numbers."""
    return {k: unit[k] for k in ("unit_id", "type", "station", "status", "incident_id", "position")}


@router.get("/api/units")
async def list_units(request: Request = None):
    require_staff(request, "dispatcher")
    return {"units": [_public(u) for u in fleet.units()]}


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
    from routers.incident import in_service_area

    if not in_service_area(fix.lat, fix.lng):
        raise HTTPException(status_code=422, detail="Position is outside the service area")
    try:
        stored = fleet.record_position(unit_id, fix.lat, fix.lng, source="gps",
                                       accuracy_m=fix.accuracy_m, heading=fix.heading)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown unit") from None
    except ValueError:
        raise HTTPException(status_code=429, detail="Too many position updates",
                            headers={"Retry-After": "1"}) from None
    return {"unit_id": unit_id, "position": stored}


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
