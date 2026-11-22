"""Driver assignments and validated pickup/transport transitions."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from staff import require as require_staff
from pydantic import BaseModel, Field

from models.schemas import Coordinate, RouteRequest
from routers.incident import (
    ACTIVE_INCIDENTS, _INCIDENTS_LOCK, commit, required_units, route_agent, snapshot, unit_statuses,
)

router = APIRouter(prefix="/api/driver", tags=["Driver Queue"])


class ClaimPayload(BaseModel):
    incident_id: str
    driver_id: str = Field(default="drv-11", min_length=1, max_length=100)
    unit_type: Literal["primary", "ambulance"] = "primary"


class HospitalChangePayload(BaseModel):
    incident_id: str
    hospital_name: str


def _incident(incident_id: str) -> dict:
    incident = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


def _assignments(incident: dict) -> dict:
    """Read old single-unit claims without assigning the backup to that driver."""
    assignments = dict(incident.get("unit_assignments") or {})
    if incident.get("driver_claimed") and incident.get("assigned_driver"):
        assignments.setdefault("primary", incident["assigned_driver"])
    return assignments


def _require_driver(incident: dict, driver_id: str, unit_type: str) -> None:
    if unit_type not in required_units(incident):
        raise HTTPException(status_code=409, detail="No backup ambulance is assigned")
    assigned = _assignments(incident).get(unit_type)
    if assigned and assigned != driver_id:
        raise HTTPException(status_code=409, detail="This unit is assigned to another driver")


def _claim(incident: dict, driver_id: str, unit_type: str) -> None:
    assignments = _assignments(incident)
    assignments[unit_type] = driver_id
    incident["unit_assignments"] = assignments
    if unit_type == "primary":
        incident["assigned_driver"] = driver_id
        incident["driver_claimed"] = True


async def _route(origin: dict, destination: dict) -> dict:
    """Use a real provider or offline path; never invent distance or travel time."""
    try:
        result = await asyncio.to_thread(
            route_agent.compute_route,
            RouteRequest(origin=Coordinate(**origin), destination=Coordinate(**destination)),
        )
        if result.confidence <= 0 or len(result.coordinates) < 2:
            raise ValueError("No usable route")
        return {
            "route_coordinates": [{"lat": c.lat, "lng": c.lng} for c in result.coordinates],
            "eta_minutes": max(1, int(result.eta // 60)),
            "distance_meters": result.distance,
            "route_degraded": False,
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Routing is unavailable; assignment has not changed") from exc


@router.get("/active")
async def get_active_dispatches():
    # A driver must not act on a report before a dispatcher approves it.
    from routers.hospital import destination_on_divert

    async with _INCIDENTS_LOCK:
        return {"dispatches": snapshot(_active_dispatches(destination_on_divert))}


def _active_dispatches(destination_on_divert) -> list[dict]:
    active = []
    for incident in ACTIVE_INCIDENTS:
        if incident.get("status") not in {"dispatched", "patient_picked_up"}:
            continue
        view = incident.get("citizen_view") or {}
        states = unit_statuses(incident)
        plan = incident.get("action_plan") or {}
        hospital = plan.get("recommended_hospital") or {}
        active.append({
            **incident,
            "unit_statuses": states,
            "unit_assignments": _assignments(incident),
            "driver_stage": "hospital" if incident["status"] == "patient_picked_up" else "pickup",
            "hospital_name": view.get("hospital_name") or hospital.get("name"),
            "hospital_location": view.get("hospital_location"),
            "origin": view.get("origin") or incident.get("location"),
            "eta_minutes": view.get("eta_minutes"),
            "distance_meters": view.get("distance_meters"),
            "route_coordinates": view.get("route_coordinates") or [],
            "hospital": hospital,
            "citizen_view": view,
            "action_plan": plan,
            # The destination may stop accepting after approval; the driver must see it.
            "destination_on_divert": destination_on_divert(incident),
        })
    return list(reversed(active))


@router.post("/claim")
async def claim_dispatch(payload: ClaimPayload, request: Request = None):
    require_staff(request, "unit")
    async with _INCIDENTS_LOCK:
        incident = _incident(payload.incident_id)
        if incident.get("status") not in {"dispatched", "patient_picked_up"}:
            raise HTTPException(status_code=409, detail="Dispatcher approval is required before claiming a call")
        _require_driver(incident, payload.driver_id, payload.unit_type)
        if unit_statuses(incident)[payload.unit_type] == "arrived":
            raise HTTPException(status_code=409, detail="This unit has already completed its assignment")
        _claim(incident, payload.driver_id, payload.unit_type)
        # Claiming again must never rewind a case already being transported.
        await commit(incident)
        return {"status": "claimed", "incident_id": payload.incident_id, "driver_id": payload.driver_id,
                "unit_type": payload.unit_type, "unit_assignments": incident["unit_assignments"],
                "unit_statuses": unit_statuses(incident)}


@router.post("/arrived_pickup")
async def arrived_pickup(payload: ClaimPayload, request: Request = None):
    require_staff(request, "unit")
    async with _INCIDENTS_LOCK:
        incident = _incident(payload.incident_id)
        _require_driver(incident, payload.driver_id, payload.unit_type)
        states = unit_statuses(incident)
        medical = payload.unit_type == "ambulance" or (incident.get("vehicle_required") or "Ambulance") in {"Ambulance", "Medical"}
        if not medical:
            raise HTTPException(status_code=409, detail="A nonmedical unit confirms arrival at the scene instead of patient pickup")
        if incident.get("status") not in {"dispatched", "patient_picked_up"}:
            raise HTTPException(status_code=409, detail="Only a dispatched call can be picked up")
        if states[payload.unit_type] == "patient_picked_up":
            return {"status": incident["status"], "unit_statuses": states,
                    "citizen_view": incident.get("citizen_view", {})}
        if states[payload.unit_type] != "dispatched":
            raise HTTPException(status_code=409, detail="This unit has already completed its assignment")
        view = dict(incident.get("citizen_view") or {})
        origin = view.get("origin") or incident.get("location")
        destination = view.get("hospital_location")
        if not origin or not destination:
            raise HTTPException(status_code=409, detail="Approved patient and hospital locations are required")
    route = await _route(origin, destination)
    async with _INCIDENTS_LOCK:
        incident = _incident(payload.incident_id)
        _require_driver(incident, payload.driver_id, payload.unit_type)
        states = unit_statuses(incident)
        if incident.get("status") not in {"dispatched", "patient_picked_up"} or states[payload.unit_type] != "dispatched":
            raise HTTPException(status_code=409, detail="The incident changed while routing; refresh the call")
        view = dict(incident.get("citizen_view") or {})
        # If dispatch changed the destination while routing, do not store an obsolete path.
        if view.get("hospital_location") != destination:
            raise HTTPException(status_code=409, detail="The destination changed; retry pickup confirmation")
        view.update(route)
        states[payload.unit_type] = "patient_picked_up"
        view["unit_statuses"] = states
        view.update({"status": "patient_picked_up", "message": f"Patient picked up. Transporting to {view.get('hospital_name', 'Hospital ER')}."})
        incident.update({"driver_stage": "hospital", "status": "patient_picked_up", "citizen_view": view,
                         "unit_statuses": states})
        _claim(incident, payload.driver_id, payload.unit_type)
        await commit(incident)
        from routers.hospital import destination_on_divert
        return {"status": "patient_picked_up", "citizen_view": view, "unit_statuses": states,
                "unit_assignments": incident["unit_assignments"],
                "destination_on_divert": destination_on_divert(incident)}


@router.post("/change_hospital")
async def change_hospital(payload: HospitalChangePayload, request: Request = None):
    require_staff(request, "unit")
    from agents.hospital_intelligence import hospital_agent
    from routers.hospital import _resolve

    async with _INCIDENTS_LOCK:
        incident = _incident(payload.incident_id)
        if incident.get("status") != "patient_picked_up":
            raise HTTPException(status_code=409, detail="A destination can be changed only during transport")
        candidates = (incident.get("action_plan") or {}).get("all_hospitals") or []
        hospital = next((h for h in candidates if payload.hospital_name in {h.get("name"), h.get("hospital_id")}), None)
        if not hospital or not hospital.get("location"):
            raise HTTPException(status_code=422, detail="Choose a hospital from the incident's reviewed destinations")
        canonical = _resolve(str(hospital.get("hospital_id", ""))) or _resolve(hospital["name"])
        if canonical in hospital_agent.diverted:
            raise HTTPException(status_code=409, detail="That hospital is on divert; select another destination")
        view = dict(incident.get("citizen_view") or {})
        origin = view.get("origin") or incident.get("location")
        old_destination = view.get("hospital_location")
    route = await _route(origin, hospital["location"])
    async with _INCIDENTS_LOCK:
        incident = _incident(payload.incident_id)
        if incident.get("status") != "patient_picked_up" or (incident.get("citizen_view") or {}).get("hospital_location") != old_destination:
            raise HTTPException(status_code=409, detail="The incident changed while routing; refresh the call")
        if canonical in hospital_agent.diverted:
            raise HTTPException(status_code=409, detail="The hospital went on divert; select another destination")
        from routers.incident import _hospital_phone
        view = dict(incident.get("citizen_view") or {})
        view.update(route)
        view.update({"hospital_name": hospital["name"], "hospital_location": hospital["location"],
                     "hospital_phone": _hospital_phone(hospital["name"]),
                     "message": f"Rerouted by driver to {hospital['name']}."})
        incident.update({"hospital_name": hospital["name"], "approved_hospital_id": canonical or hospital.get("hospital_id"),
                         "hospital_phone": view["hospital_phone"], "citizen_view": view})
        await commit(incident)
        from routers.hospital import destination_on_divert
        return {"status": "rerouted", "citizen_view": view, "hospital_name": hospital["name"],
                "hospital_phone": view["hospital_phone"],
                "destination_on_divert": destination_on_divert(incident)}
