"""
Incident Router for live Unified React App.
Matches the event-driven async flow described in the trace.
"""
from __future__ import annotations

import uuid
import asyncio
import logging
import re
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Response, status, HTTPException
from pydantic import BaseModel

from models.schemas import (
    Coordinate,
    AccessibilityRequest,
    AccessibilityInputModality,
    FusionRequest,
    RouteRequest,
)
from agents.accessibility import accessibility_agent
from agents.decision_fusion import fusion_engine
from agents.route_optimization import route_agent, find_nearest_hub

logger = logging.getLogger("geoagentic.incident")

router = APIRouter(prefix="/api/incidents", tags=["Incidents"])

# In-memory queue of active incidents
ACTIVE_INCIDENTS: List[Dict[str, Any]] = []
# asyncio lock — all mutations to ACTIVE_INCIDENTS must be done under this lock
# to prevent concurrent pipeline tasks from racing on append / clear / search.
_INCIDENTS_LOCK: asyncio.Lock = asyncio.Lock()

import json
from pathlib import Path

from storage import data_path, load_collection, save_collection

_LEGACY_DB_FILE = Path(__file__).parent.parent / "data" / "incidents_db.json"
DB_FILE = _LEGACY_DB_FILE


def persistence_path() -> Path:
    # Keep explicit per-test / legacy path overrides isolated from real state.
    return DB_FILE.with_suffix(".sqlite3") if DB_FILE != _LEGACY_DB_FILE else data_path()


def save_db():
    # Let write failures surface instead of claiming an unpersisted transition
    # succeeded. SQLite commits the snapshot atomically.
    save_collection("incidents", ACTIVE_INCIDENTS, database=persistence_path())


def load_db():
    data = load_collection("incidents", None, database=persistence_path())
    if data is None:
        # One-time migration; the legacy JSON remains available for rollback.
        legacy = DB_FILE if DB_FILE != _LEGACY_DB_FILE else persistence_path().parent / "incidents_db.json"
        data = json.loads(legacy.read_text(encoding="utf-8")) if legacy.exists() else []
    if not isinstance(data, list):
        raise RuntimeError("Stored incidents must be a list")
    ACTIVE_INCIDENTS.clear()
    ACTIVE_INCIDENTS.extend(data)
    # An in-process task cannot survive a restart. Fail interrupted reports
    # visibly so a citizen can retry instead of polling forever.
    for incident in ACTIVE_INCIDENTS:
        if incident.get("status") == "processing":
            incident["status"] = "failed"
            incident["error"] = "Processing was interrupted by a server restart. Please submit again."
    save_db()

load_db()


class IncidentReportPayload(BaseModel):
    citizen_text: str
    citizen_name: Optional[str] = None
    citizen_phone: Optional[str] = None
    vehicle_required: Optional[str] = "Ambulance"
    include_ambulance_backup: Optional[bool] = False
    location: Coordinate
    input_modality: str = "text"
    timestamp: Optional[str] = None

class ApprovePayload(BaseModel):
    dispatcher_id: str
    approved_hospital: str
    approved_route: str

# Demo click-to-call numbers (no live hospital/unit directory feed).
DEMO_HOSPITAL_PHONES = {
    "Aster CMI Hospital (Hebbal)": "+91 80 4342 0107",
    "Manipal Hospital (Old Airport Road)": "+91 80 2502 4444",
    "Fortis Hospital (Bannerghatta Road)": "+91 80 6621 4444",
    "Sri Jayadeva Institute of Cardiovascular Sciences": "+91 80 2297 7200",
    "NIMHANS": "+91 80 2699 5000",
    "St. John's Medical College Hospital": "+91 80 2206 5000",
    "Victoria Hospital (BMCRI)": "+91 80 2670 1150",
}
DEMO_UNIT_PHONE = "+91 98001 10004"


def _resolve_modality(raw: str) -> AccessibilityInputModality:
    try:
        return AccessibilityInputModality(str(raw or "text").strip().lower())
    except ValueError:
        return AccessibilityInputModality.TEXT


def _hospital_phone(name: str | None) -> str | None:
    """Only expose a demo directory number for an exact normalized name."""
    if not name:
        return None

    def normalize(value: str) -> str:
        value = re.sub("['’]", "", value.lower())
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    requested = normalize(name)
    for key, phone in DEMO_HOSPITAL_PHONES.items():
        if normalize(key) == requested:
            return phone
    return None


def _mark_failed(incident_id: str, exc: Exception) -> None:
    # NOTE: This is called from an async except-block; it does NOT acquire
    # _INCIDENTS_LOCK because doing so from a sync function inside an async
    # task would require run_coroutine_threadsafe. The caller (run_pipeline)
    # is the only writer at this point — the task already owns the logical
    # "pipeline slot" for this incident_id — so direct mutation is safe here.
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == incident_id and inc.get("status") == "processing":
            inc["status"] = "failed"
            inc["error"] = str(exc) or "Pipeline failed"
            save_db()
            break


async def run_pipeline(incident_id: str, payload: IncidentReportPayload):
    """Background task simulating the event-driven bus pipeline."""
    try:
        modality = _resolve_modality(payload.input_modality)
        access_req = AccessibilityRequest(
            raw_input=payload.citizen_text,
            location=payload.location,
            modality=modality,
        )
        # Awaitable work happens OUTSIDE the lock so we don't hold it across
        # I/O-bound agent calls.
        access_res = await accessibility_agent.process_input(access_req)

        async with _INCIDENTS_LOCK:
            found = False
            for inc in ACTIVE_INCIDENTS:
                if inc["incident_id"] == incident_id and inc.get("status") == "processing":
                    inc["emergency_type"] = access_res.incident_report.incident_type.value
                    inc["severity"] = access_res.incident_report.severity_estimate
                    inc["details"] = access_res.incident_report.extracted_details
                    inc["input_modality"] = modality.value
                    found = True
                    break
            # If the incident was cleared/deleted before this point, abort cleanly
            # instead of silently doing nothing (which previously left no failure
            # marker and no citizen_view, causing a permanent 404 on /approve).
            if not found:
                logger.warning("Pipeline: incident %s no longer present after accessibility step — aborting", incident_id)
                return

        fusion_req = FusionRequest(
            incident_id=incident_id,
            incident_location=payload.location,
            emergency_type=access_res.incident_report.incident_type,
            severity=access_res.incident_report.severity_estimate,
            vehicle_location=payload.location,
            accessibility_needs=access_res.citizen_accessibility_profile.model_dump()
        )

        fusion_res = await fusion_engine.fuse(fusion_req)

        # Compute phase-1 route (hub → patient) outside the lock.
        hosp = fusion_res.recommended_hospital
        veh_type = payload.vehicle_required or "Ambulance"
        nearest_hub = find_nearest_hub(veh_type, payload.location.lat, payload.location.lng)
        unit_phone = nearest_hub.get("phone") or DEMO_UNIT_PHONE
        hospital_phone = _hospital_phone(hosp.name)

        try:
            p1_route = await asyncio.to_thread(route_agent.compute_route, RouteRequest(
                origin=Coordinate(lat=nearest_hub["lat"], lng=nearest_hub["lng"]),
                destination=payload.location
            ))
            if p1_route.confidence <= 0 or len(p1_route.coordinates) < 2:
                raise ValueError("No usable hub-to-patient route")
            p1_coords = [{"lat": c.lat, "lng": c.lng} for c in p1_route.coordinates]
            p1_eta = max(1, int(p1_route.eta // 60))
        except Exception:
            p1_coords = []
            p1_eta = None

        route_coords = [
            {"lat": c.lat, "lng": c.lng}
            for c in fusion_res.recommended_route.coordinates
        ]

        # Build the full citizen_view dict before acquiring the lock so the
        # incident is never visible in the partially-written state where
        # action_plan is set but citizen_view is still absent.
        new_citizen_view = {
            "message": f"Dispatching from {nearest_hub['name']}. Destination Hospital: {hosp.name}.",
            "eta_minutes": int(fusion_res.recommended_route.eta // 60),
            "hospital_name": hosp.name,
            "hospital_location": {"lat": hosp.location.lat, "lng": hosp.location.lng},
            "hospital_phone": hospital_phone,
            "unit_phone": unit_phone,
            "origin": {"lat": payload.location.lat, "lng": payload.location.lng},
            "route_coordinates": route_coords,
            "phase1_hub": nearest_hub,
            "phase1_route_coordinates": p1_coords,
            "phase1_eta_minutes": p1_eta,
            "phase1_route_degraded": p1_eta is None,
            "distance_meters": fusion_res.recommended_route.distance,
            "emergency_type": access_res.incident_report.incident_type.value,
            "severity": access_res.incident_report.severity_estimate,
            "vehicle_required": payload.vehicle_required or "Ambulance",
            "include_ambulance_backup": bool(payload.include_ambulance_backup),
        }

        async with _INCIDENTS_LOCK:
            found = False
            for inc in ACTIVE_INCIDENTS:
                if inc["incident_id"] == incident_id and inc.get("status") == "processing":
                    # Write action_plan, citizen_view, and status atomically
                    # inside the lock so no reader ever sees action_plan without
                    # citizen_view, or a "awaiting_dispatcher_approval" status
                    # with neither field populated.
                    inc["action_plan"] = fusion_res.model_dump(mode="json")
                    inc["unit_phone"] = unit_phone
                    inc["hospital_phone"] = hospital_phone
                    inc["citizen_view"] = new_citizen_view
                    inc["status"] = "awaiting_dispatcher_approval"
                    found = True
                    save_db()
                    break
            if not found:
                logger.warning("Pipeline: incident %s no longer present after fusion step — aborting", incident_id)
    except Exception as exc:
        logger.exception("Pipeline failed for %s", incident_id)
        _mark_failed(incident_id, exc)

@router.post("")
async def report_incident(payload: IncidentReportPayload, response: Response):
    """Step 1 - Citizen raises emergency. Returns 202 immediately."""
    incident_id = f"INC-{uuid.uuid4().hex}"
    
    incident = {
        "incident_id": incident_id,
        "location": payload.location.model_dump(),
        "citizen_text": payload.citizen_text,
        "citizen_name": payload.citizen_name or "Anonymous Citizen",
        "citizen_phone": payload.citizen_phone or "Unregistered",
        "vehicle_required": payload.vehicle_required or "Ambulance",
        "include_ambulance_backup": bool(payload.include_ambulance_backup),
        "input_modality": _resolve_modality(payload.input_modality).value,
        "status": "processing",
        "action_plan": None
    }
    async with _INCIDENTS_LOCK:
        ACTIVE_INCIDENTS.append(incident)
        save_db()
    
    # Start background processing pipeline
    asyncio.create_task(run_pipeline(incident_id, payload))
    
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "incident_id": incident_id,
        "status": "processing",
        "poll_url": f"/api/incidents/{incident_id}"
    }

@router.get("/{incident_id}")
async def get_incident(incident_id: str):
    """Step 3 - Citizen screen polls."""
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == incident_id:
            return inc
    raise HTTPException(status_code=404, detail="Incident not found")

@router.get("/")
async def get_all_incidents():
    """For Dispatcher to see all active incidents."""
    # Ensure they have severity for sorting.
    # Acquire the lock because we mutate incident dicts in-place — a concurrent
    # run_pipeline task could be doing the same thing simultaneously.
    async with _INCIDENTS_LOCK:
        for inc in ACTIVE_INCIDENTS:
            if "severity" not in inc:
                inc["severity"] = 0.5
        return {"incidents": list(ACTIVE_INCIDENTS)}

@router.post("/{incident_id}/approve")
async def approve_incident(incident_id: str, payload: ApprovePayload):
    """Approve a reviewed destination without allowing terminal cases to rewind."""
    from routers.driver import _route
    from routers.hospital import _resolve
    from agents.hospital_intelligence import hospital_agent

    async with _INCIDENTS_LOCK:
        inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        if inc.get("status") != "awaiting_dispatcher_approval":
            raise HTTPException(status_code=409, detail="Only a report awaiting approval can be dispatched")
        plan = inc.get("action_plan") or {}
        candidates = plan.get("all_hospitals") or []
        target = next((h for h in candidates if payload.approved_hospital in {h.get("hospital_id"), h.get("name")}), None)
        recommended = plan.get("recommended_hospital") or {}
        if target is None and payload.approved_hospital in {recommended.get("hospital_id"), recommended.get("name")}:
            target = recommended
        if not target or not target.get("location"):
            raise HTTPException(status_code=422, detail="Choose a hospital from the reviewed plan")
        canonical = _resolve(str(target.get("hospital_id", ""))) or _resolve(target["name"])
        if canonical in hospital_agent.diverted:
            raise HTTPException(status_code=409, detail="That hospital is on divert; select another destination")
        origin = dict(inc["location"])
        destination = dict(target["location"])
    # Network I/O does not hold the incident lock or block the event loop.
    route = await _route(origin, destination)
    async with _INCIDENTS_LOCK:
        inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        if inc is None or inc.get("status") != "awaiting_dispatcher_approval":
            raise HTTPException(status_code=409, detail="The incident changed while routing; refresh the call")
        if canonical in hospital_agent.diverted:
            raise HTTPException(status_code=409, detail="The hospital went on divert; select another destination")
        view = dict(inc.get("citizen_view") or {})
        view.update(route)
        view.update({
            "status": "dispatched",
            "message": f"Help is being dispatched. Approved Hospital: {target['name']}.",
            "hospital_name": target["name"], "hospital_location": destination,
            "hospital_phone": _hospital_phone(target["name"]),
            "unit_phone": inc.get("unit_phone") or DEMO_UNIT_PHONE,
            "origin": origin, "emergency_type": inc.get("emergency_type", "general"),
            "severity": inc.get("severity", 0.5),
        })
        inc.update({"status": "dispatched", "driver_stage": "pickup", "dispatcher_id": payload.dispatcher_id,
                    "approved_hospital_id": canonical or target.get("hospital_id"),
                    "hospital_phone": view["hospital_phone"], "citizen_view": view})
        inc["unit_statuses"] = {unit: "dispatched" for unit in required_units(inc)}
        view["unit_statuses"] = inc["unit_statuses"]
        save_db()
        logger.info("PLAN_APPROVED for %s by %s: hospital=%s", incident_id, payload.dispatcher_id, target["name"])
        return inc


@router.post("/{incident_id}/reject")
async def reject_incident(incident_id: str, dispatcher_id: str = "DISPATCHER-01"):
    """Dispatcher rejects an incident plan — sets status to 'rejected' so it leaves the approval queue."""
    async with _INCIDENTS_LOCK:
        for inc in ACTIVE_INCIDENTS:
            if inc["incident_id"] == incident_id:
                if inc.get("status") not in ("awaiting_dispatcher_approval", "processing"):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot reject {incident_id}: status is '{inc.get('status')}'.",
                    )
                inc["status"] = "failed"
                inc["error"] = "Rejected by dispatcher"
                inc["dispatcher_id"] = dispatcher_id
                if "citizen_view" in inc:
                    inc["citizen_view"]["status"] = "failed"
                    inc["citizen_view"]["message"] = "Your request could not be dispatched. Please call 112."
                save_db()
                logger.info("REJECTED %s by %s", incident_id, dispatcher_id)
                return {"status": "rejected", "incident_id": incident_id}
    raise HTTPException(status_code=404, detail="Incident not found")


def required_units(incident: dict) -> set[str]:
    return {"primary", "ambulance"} if incident.get("include_ambulance_backup") else {"primary"}


def unit_statuses(incident: dict) -> dict:
    """Read legacy records while keeping primary and backup progress separate."""
    medical = (incident.get("vehicle_required") or "Ambulance") in {"Ambulance", "Medical"}
    primary = "patient_picked_up" if incident.get("status") == "patient_picked_up" and medical else "dispatched"
    defaults = {unit: "dispatched" for unit in required_units(incident)}
    defaults["primary"] = primary
    if incident.get("status") == "completed":
        defaults = {unit: "arrived" for unit in required_units(incident)}
    return {**defaults, **incident.get("unit_statuses", {})}


@router.post("/{incident_id}/arrived")
async def incident_arrived(incident_id: str, unit_type: str = "primary"):
    """Record each unit independently; finish only when all required units arrive."""
    if unit_type not in {"primary", "ambulance"}:
        raise HTTPException(status_code=422, detail="Unknown unit type")
    async with _INCIDENTS_LOCK:
        for inc in ACTIVE_INCIDENTS:
            if inc["incident_id"] != incident_id:
                continue
            if unit_type not in required_units(inc):
                raise HTTPException(status_code=409, detail="No backup ambulance is assigned")
            states = unit_statuses(inc)
            if inc.get("status") not in {"dispatched", "patient_picked_up", "completed"}:
                raise HTTPException(status_code=409, detail="Dispatcher approval is required before arrival")
            medical = unit_type == "ambulance" or (inc.get("vehicle_required") or "Ambulance") in {"Ambulance", "Medical"}
            if states[unit_type] != "arrived" and medical and states[unit_type] != "patient_picked_up":
                raise HTTPException(status_code=409, detail="Pickup must be confirmed before medical handover")
            states[unit_type] = "arrived"
            inc["unit_statuses"] = states
            complete = all(states.get(unit) == "arrived" for unit in required_units(inc))
            inc["status"] = "completed" if complete else ("patient_picked_up" if "patient_picked_up" in states.values() else "dispatched")
            inc["driver_stage"] = "completed" if complete else ("hospital" if inc["status"] == "patient_picked_up" else "pickup")
            if "citizen_view" in inc:
                inc["citizen_view"]["unit_statuses"] = states
                inc["citizen_view"]["status"] = inc["status"]
            save_db()
            return {"status": "arrived", "unit_type": unit_type, "unit_statuses": states, "incident_status": inc["status"]}
    raise HTTPException(status_code=404, detail="Incident not found")


@router.post("/clear")
async def clear_incidents():
    # The application middleware requires operator access for this bulk action,
    # including when the rest of the service runs in synthetic-data demo mode.
    async with _INCIDENTS_LOCK:
        ACTIVE_INCIDENTS.clear()
        save_db()
    # Also clear chat history so old messages don't surface on new incidents with the same ID.
    from routers.chat import manager
    manager.clear_history()
    return {"status": "cleared"}
@router.post("/{incident_id}/request_ambulance")
async def request_ambulance_backup(incident_id: str):
    async with _INCIDENTS_LOCK:
        for inc in ACTIVE_INCIDENTS:
            if inc["incident_id"] != incident_id:
                continue
            if inc.get("status") not in {"awaiting_dispatcher_approval", "dispatched", "patient_picked_up"}:
                raise HTTPException(status_code=409, detail="Backup can be added only to an active call")
            inc["include_ambulance_backup"] = True
            inc["unit_statuses"] = unit_statuses(inc)
            if "citizen_view" in inc:
                inc["citizen_view"]["include_ambulance_backup"] = True
                inc["citizen_view"]["unit_statuses"] = inc["unit_statuses"]
            save_db()
            return {"status": "ambulance_requested", "incident_id": incident_id}
    raise HTTPException(status_code=404, detail="Incident not found")
