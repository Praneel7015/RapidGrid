"""Hospital recommendations, receiving queues and persisted operator overrides."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from staff import require as require_staff
from pydantic import BaseModel, Field

from agents.hospital_intelligence import NoHospitalCandidatesError, hospital_agent
from models.schemas import HospitalRankingResponse, HospitalRequest
from routers.incident import ACTIVE_INCIDENTS, _INCIDENTS_LOCK, persistence_path, snapshot, unit_statuses
from logsafe import log_safe
from storage import load_collection, save_collection

logger = logging.getLogger("geoagentic.router.hospital")
router = APIRouter(prefix="/api/hospital", tags=["Hospital Intelligence"])


class HospitalResourceUpdate(BaseModel):
    """Legacy icu_available toggle controls whether a facility accepts new cases."""
    # Omitted or null leaves the accepting/divert state unchanged.
    icu_available: bool | None = None
    beds_available: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)


def _resolve(identifier: str) -> str | None:
    """Resolve exact identifiers/names without matching another hospital's tokens."""
    profiles = hospital_agent._load_capabilities()
    if identifier in profiles:
        return identifier
    needle = identifier.strip().casefold()
    for key, profile in profiles.items():
        if profile["name"].casefold() == needle:
            return key
    for hospital in hospital_agent._hospitals:
        if str(hospital.get("id")) == identifier and hospital.get("curated_id") in profiles:
            return hospital["curated_id"]
    return None


def destination_on_divert(incident: dict) -> bool:
    """True when the hospital this case is heading to has stopped accepting."""
    assigned = incident.get("approved_hospital_id")
    key = _resolve(str(assigned)) if assigned else None
    if key is None:
        name = (incident.get("citizen_view") or {}).get("hospital_name")
        key = _resolve(name) if name else None
    return key is not None and key in hospital_agent.diverted


def restore_capacity() -> None:
    """Reapply persisted diversion overrides during application startup."""
    overrides = load_collection("hospital_capacity", {}, database=persistence_path())
    for key, override in overrides.items():
        if override.get("icu_available") is not None:
            hospital_agent.set_divert(key, on_divert=not override["icu_available"])
        if override.get("beds_available") is not None:
            hospital_agent.set_reported_beds(key, override["beds_available"])


def _details(key: str) -> dict:
    profile = hospital_agent._load_capabilities()[key]
    override = load_collection("hospital_capacity", {}, database=persistence_path()).get(key, {})
    accepting = override.get("icu_available", key not in hospital_agent.diverted)
    return {
        "hospital_id": key,
        "name": profile["name"],
        "location": {"lat": profile.get("lat"), "lng": profile.get("lng")},
        "capabilities": profile["capabilities"],
        "beds_total": profile.get("beds_total"),
        "beds_available": override.get("beds_available"),
        "icu_available": accepting,
        "accepting": accepting,
        "on_divert": not accepting,
        "availability_provenance": "operator-reported demo override" if override else "demo default; no hospital feed",
        "note": override.get("note"),
    }


@router.post("/rank", response_model=HospitalRankingResponse)
async def rank_hospitals(request: HospitalRequest, http: Request = None) -> HospitalRankingResponse:
    require_staff(http, "dispatcher")
    try:
        return await hospital_agent.publish_ranking(request)
    except NoHospitalCandidatesError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/list")
async def list_hospitals():
    hospitals = [_details(key) for key in hospital_agent._load_capabilities()]
    return {"hospitals": hospitals, "count": len(hospitals)}


def _incoming(hospital_id: str) -> list[dict]:
    key = None if hospital_id == "all" else _resolve(hospital_id)
    if hospital_id != "all" and key is None:
        raise HTTPException(status_code=404, detail="Hospital not found")
    incoming = []
    for incident in ACTIVE_INCIDENTS:
        if incident.get("status") not in {"dispatched", "patient_picked_up"}:
            continue
        if (incident.get("vehicle_required") or "Ambulance") not in {"Ambulance", "Medical"} and not incident.get("include_ambulance_backup"):
            continue
        medical_unit = "primary" if (incident.get("vehicle_required") or "Ambulance") in {"Ambulance", "Medical"} else "ambulance"
        if unit_statuses(incident).get(medical_unit) == "arrived":
            continue
        view = incident.get("citizen_view") or {}
        recommendation = (incident.get("action_plan") or {}).get("recommended_hospital") or {}
        assigned_id = incident.get("approved_hospital_id") or recommendation.get("hospital_id")
        assigned_key = _resolve(str(assigned_id)) if assigned_id else None
        hospital_name = view.get("hospital_name") or recommendation.get("name")
        if assigned_key is None and hospital_name:
            assigned_key = _resolve(hospital_name)
        if hospital_id != "all" and assigned_key != key:
            continue
        incoming.append({
            "incident_id": incident["incident_id"], "status": incident["status"],
            "emergency_type": incident.get("emergency_type") or view.get("emergency_type") or "general",
            "severity": incident.get("severity", view.get("severity", 0.5)),
            "hospital_name": hospital_name, "eta_minutes": view.get("eta_minutes"),
            "patient_summary": incident.get("details") or incident.get("citizen_text"),
            "assigned_driver": (incident.get("unit_assignments") or {}).get(medical_unit) or (incident.get("assigned_driver") if medical_unit == "primary" else None),
            "destination_on_divert": destination_on_divert(incident),
        })
    return incoming


def _require_hospital_or_dispatch(request, hospital_id: str) -> None:
    """That hospital's own sign-in, or dispatch (A01)."""
    from fastapi import HTTPException as _H

    try:
        require_staff(request, "dispatcher")
        return
    except _H:
        pass
    require_staff(request, "hospital", _resolve(hospital_id) or hospital_id)


@router.get("/incoming/all")
async def get_all_incoming_cases(request: Request = None):
    require_staff(request, "dispatcher")  # every hospital's patients: dispatch only (A01)
    async with _INCIDENTS_LOCK:
        return {"incoming": snapshot(_incoming("all"))}


@router.get("/{hospital_id}/incoming")
async def get_incoming_cases(hospital_id: str, name: str | None = None, request: Request = None):
    # Names in the query string never broaden the requested facility scope.
    _require_hospital_or_dispatch(request, hospital_id)
    async with _INCIDENTS_LOCK:
        return {"incoming": snapshot(_incoming(hospital_id))}


@router.get("/{hospital_id}")
async def get_hospital(hospital_id: str):
    key = _resolve(hospital_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Hospital not found")
    return _details(key)


@router.patch("/{hospital_id}/resources")
async def update_hospital_resources(hospital_id: str, update: HospitalResourceUpdate, request: Request = None):
    who = require_staff(request, "hospital")
    if who[0] == "hospital" and (_resolve(who[1]) or who[1]) != (_resolve(hospital_id) or hospital_id):
        # Both sides in the same canonical form (curated id), so a name or an
        # alias of the signed-in hospital matches, and nothing else does.
        raise HTTPException(status_code=403, detail="This sign-in cannot do that")
    key = _resolve(hospital_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Choose a specific hospital before changing capacity")
    total = hospital_agent._load_capabilities()[key].get("beds_total")
    if update.beds_available is not None and total is not None and update.beds_available > total:
        raise HTTPException(status_code=422, detail="Available beds cannot exceed total beds")
    overrides = load_collection("hospital_capacity", {}, database=persistence_path())
    changes = update.model_dump(exclude_unset=True)
    if changes.get("icu_available") is None:
        changes.pop("icu_available", None)
    overrides[key] = {**overrides.get(key, {}), **changes}
    save_collection("hospital_capacity", overrides, database=persistence_path())
    if update.icu_available is not None:
        hospital_agent.set_divert(key, on_divert=not update.icu_available)
    if "beds_available" in update.model_fields_set:
        hospital_agent.set_reported_beds(key, update.beds_available)
    logger.info("Operator capacity override for %s: accepting=%s", log_safe(key), log_safe(update.icu_available))
    return {"status": "updated", **_details(key), "diverted_facilities": sorted(hospital_agent.diverted)}
