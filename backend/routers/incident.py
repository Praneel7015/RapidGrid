"""
Incident Router for live Unified React App.
Matches the event-driven async flow described in the trace.
"""
from __future__ import annotations

import uuid
import asyncio
import logging
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

DB_FILE = Path(__file__).parent.parent / "data" / "incidents_db.json"

def save_db():
    try:
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(ACTIVE_INCIDENTS, f, indent=2)
    except Exception as e:
        print("DB save error:", e)

def load_db():
    global ACTIVE_INCIDENTS
    # NOTE: load_db() is called once at module import (see below).  If
    # incidents_db.json contains incidents with status "failed", "processing",
    # or "dispatched" from a previous server run, those records will be restored
    # into ACTIVE_INCIDENTS and re-surfaced to citizens on the next server
    # restart.  This is intentional for "dispatched" incidents (they are still
    # live), but stale "failed" or "processing" incidents from a crashed run
    # will appear valid until they time out or are explicitly cleared.
    # To avoid ghost incidents after a crash, consider filtering on load:
    #   data = [i for i in data if i.get("status") not in {"failed", "processing"}]
    try:
        if DB_FILE.exists():
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    ACTIVE_INCIDENTS.clear()
                    ACTIVE_INCIDENTS.extend(data)
    except Exception as e:
        print("DB load error:", e)

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
    if not name:
        return None
    if name in DEMO_HOSPITAL_PHONES:
        return DEMO_HOSPITAL_PHONES[name]
    for key, phone in DEMO_HOSPITAL_PHONES.items():
        if key.lower() in name.lower() or name.lower() in key.lower():
            return phone
    return "+91 80 0000 0000"


def _mark_failed(incident_id: str, exc: Exception) -> None:
    # NOTE: This is called from an async except-block; it does NOT acquire
    # _INCIDENTS_LOCK because doing so from a sync function inside an async
    # task would require run_coroutine_threadsafe. The caller (run_pipeline)
    # is the only writer at this point — the task already owns the logical
    # "pipeline slot" for this incident_id — so direct mutation is safe here.
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == incident_id:
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
                if inc["incident_id"] == incident_id:
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
            p1_route = route_agent.compute_route(RouteRequest(
                origin=Coordinate(lat=nearest_hub["lat"], lng=nearest_hub["lng"]),
                destination=payload.location
            ))
            p1_coords = [{"lat": c.lat, "lng": c.lng} for c in p1_route.coordinates]
            p1_eta = max(1, int(p1_route.eta // 60))
        except Exception:
            p1_coords = [
                {"lat": nearest_hub["lat"], "lng": nearest_hub["lng"]},
                {"lat": payload.location.lat, "lng": payload.location.lng},
            ]
            p1_eta = 5

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
            "distance_meters": fusion_res.recommended_route.distance,
            "emergency_type": access_res.incident_report.incident_type.value,
            "severity": access_res.incident_report.severity_estimate,
            "vehicle_required": payload.vehicle_required or "Ambulance",
            "include_ambulance_backup": bool(payload.include_ambulance_backup),
        }

        async with _INCIDENTS_LOCK:
            found = False
            for inc in ACTIVE_INCIDENTS:
                if inc["incident_id"] == incident_id:
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
    incident_id = f"INC-{uuid.uuid4().hex[:4]}"
    
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
    """Step 4 - Dispatcher approves the AI plan, or overrides the hospital."""

    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] != incident_id:
            continue

        action_plan = inc.get("action_plan") or {}
        all_hospitals = action_plan.get("all_hospitals") or []

        # Resolve the approved hospital by id or name.
        target_hosp = None
        for h in all_hospitals:
            h_dict = h if isinstance(h, dict) else h.model_dump()
            if payload.approved_hospital in (
                h_dict.get("hospital_id"), h_dict.get("name")
            ):
                target_hosp = h_dict
                break

        # BUG: the dispatcher UI sends sentinel ids ('HOSP-DEFAULT', 'HOSP-AUTO')
        # whenever the plan is still loading, and the old code then dereferenced
        # target_hosp=None -> HTTP 500. Fall back to the AI recommendation.
        if target_hosp is None:
            recommended = action_plan.get("recommended_hospital")
            if isinstance(recommended, dict):
                target_hosp = recommended
                logger.warning(
                    "Approve for %s: '%s' not in candidate list — falling back "
                    "to the recommended hospital '%s'.",
                    incident_id, payload.approved_hospital,
                    target_hosp.get("name"),
                )
            else:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot approve {incident_id}: no action plan is ready "
                        f"yet and '{payload.approved_hospital}' matched no "
                        f"candidate hospital."
                    ),
                )

        inc["status"] = "dispatched"
        inc["assigned_driver"] = "drv-11"
        inc["dispatcher_id"] = payload.dispatcher_id

        hosp_loc = target_hosp.get("location") or {}
        hosp_lat = hosp_loc.get("lat") or 12.9716
        hosp_lng = hosp_loc.get("lng") or 77.5946

        cit_loc = inc.get("location") or {}
        cit_lat = cit_loc.get("lat") or 12.9756
        cit_lng = cit_loc.get("lng") or 77.6068

        # Recompute Phase 2 (citizen -> approved hospital).
        route_ok = True
        try:
            new_route = route_agent.compute_route(RouteRequest(
                origin=Coordinate(lat=cit_lat, lng=cit_lng),
                destination=Coordinate(lat=hosp_lat, lng=hosp_lng),
            ))
            route_ok = new_route.confidence > 0 and len(new_route.coordinates) >= 2
            new_coords = [{"lat": c.lat, "lng": c.lng} for c in new_route.coordinates]
            eta_mins = max(1, int(new_route.eta // 60))
            dist_meters = new_route.distance
        except Exception:
            logger.exception("Re-route failed for %s", incident_id)
            route_ok = False
            new_coords = [
                {"lat": cit_lat, "lng": cit_lng},
                {"lat": hosp_lat, "lng": hosp_lng},
            ]
            eta_mins = 0
            dist_meters = 0.0

        # MERGE into citizen_view rather than replacing it. The old code
        # rebuilt the dict from scratch and silently dropped phase1_hub,
        # phase1_route_coordinates, phase1_eta_minutes, vehicle_required and
        # include_ambulance_backup — so approving wiped the 2-phase route off
        # the citizen's map at the exact moment it mattered most.
        hospital_phone = _hospital_phone(target_hosp.get("name"))
        unit_phone = inc.get("unit_phone") or DEMO_UNIT_PHONE
        citizen_view = dict(inc.get("citizen_view") or {})
        citizen_view.update({
            "message": f"Help is being dispatched. Approved Hospital: {target_hosp.get('name')}.",
            "eta_minutes": eta_mins,
            "hospital_name": target_hosp.get("name"),
            "hospital_location": {"lat": hosp_lat, "lng": hosp_lng},
            "hospital_phone": hospital_phone,
            "unit_phone": unit_phone,
            "origin": {"lat": cit_lat, "lng": cit_lng},
            "route_coordinates": new_coords,
            "distance_meters": dist_meters,
            "emergency_type": inc.get("emergency_type", "General"),
            "severity": inc.get("severity", 0.5),
            "route_degraded": not route_ok,
        })
        inc["hospital_phone"] = hospital_phone
        inc["unit_phone"] = unit_phone
        inc["citizen_view"] = citizen_view

        # Persist AFTER the view is built, not before.
        save_db()

        logger.info(
            "PLAN_APPROVED for %s by %s: hospital=%s, ETA=%dmin%s",
            incident_id, payload.dispatcher_id, target_hosp.get("name"),
            eta_mins, "" if route_ok else " (ROUTE DEGRADED)",
        )
        return inc

    raise HTTPException(status_code=404, detail="Incident not found")

@router.post("/{incident_id}/arrived")
async def incident_arrived(incident_id: str, unit_type: str = "primary"):
    """Step 5 - Driver arrives for specific unit (primary vs ambulance)."""
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == incident_id:
            unit_statuses = inc.get("unit_statuses") or {"primary": "dispatched", "ambulance": "dispatched"}
            unit_statuses[unit_type] = "arrived"
            inc["unit_statuses"] = unit_statuses
            
            # If primary unit arrived or both arrived, set main incident status to completed
            if unit_statuses.get("primary") == "arrived":
                inc["status"] = "completed"
            
            if "citizen_view" in inc:
                inc["citizen_view"]["unit_statuses"] = unit_statuses
                if inc["status"] == "completed":
                    inc["citizen_view"]["status"] = "completed"
                    
            save_db()
            return {"status": "arrived", "unit_type": unit_type, "unit_statuses": unit_statuses, "incident_status": inc["status"]}
    raise HTTPException(status_code=404, detail="Incident not found")


@router.post("/clear")
async def clear_incidents():
    # SECURITY NOTE: This endpoint has NO authentication guard.  Any caller
    # with network access can wipe all live incidents.  In production this
    # must be protected by at minimum an internal-network restriction or an
    # API-key / operator-role check.  Auth is out of scope for this sprint —
    # do NOT ship this endpoint to a public-facing deployment without adding it.
    async with _INCIDENTS_LOCK:
        ACTIVE_INCIDENTS.clear()
        save_db()
    return {"status": "cleared"}
@router.post("/{incident_id}/request_ambulance")
async def request_ambulance_backup(incident_id: str):
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == incident_id:
            inc["include_ambulance_backup"] = True
            if "citizen_view" in inc:
                inc["citizen_view"]["include_ambulance_backup"] = True
            save_db()
            return {"status": "ambulance_requested", "incident_id": incident_id}
    raise HTTPException(status_code=404, detail="Incident not found")
