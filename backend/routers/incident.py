"""
Incident Router for live Unified React App.
Matches the event-driven async flow described in the trace.
"""
from __future__ import annotations

import copy
import os
import uuid
import asyncio
import logging
import re
from typing import List, Dict, Any, Literal, Optional
from fastapi import APIRouter, Query, Response, status, HTTPException
from pydantic import BaseModel, Field

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

from logsafe import log_safe
import storage
from storage import data_path, load_collection, save_collection

_LEGACY_DB_FILE = Path(__file__).parent.parent / "data" / "incidents_db.json"
DB_FILE = _LEGACY_DB_FILE

# Finished calls (completed or failed) beyond this many are dropped, oldest
# first, so polls and memory stay bounded however long the service runs.
KEEP_FINISHED = int(os.environ.get("RAPIDGRID_KEEP_FINISHED", "100"))
_FINISHED = {"completed", "failed"}

# incident_id -> the JSON last written successfully. A failed write restores the
# in-memory record from here, so memory never runs ahead of disk.
_PERSISTED: Dict[str, str] = {}
_PIPELINE_TASKS: set = set()


def persistence_path() -> Path:
    # Keep explicit per-test / legacy path overrides isolated from real state.
    return DB_FILE.with_suffix(".sqlite3") if DB_FILE != _LEGACY_DB_FILE else data_path()


def _dump(incident: dict) -> str:
    return json.dumps(incident, ensure_ascii=False)


def _restore(incident: dict) -> None:
    """Put an incident back to its last stored state after a failed write."""
    stored = _PERSISTED.get(incident.get("incident_id"))
    if stored is None:
        # Never stored: it must not linger in memory as if it had been accepted.
        ACTIVE_INCIDENTS[:] = [i for i in ACTIVE_INCIDENTS if i is not incident]
        return
    incident.clear()
    incident.update(json.loads(stored))


async def commit(incident: dict) -> None:
    """Store one changed incident off the event loop; on failure undo the change.

    Call it while holding _INCIDENTS_LOCK, after mutating ``incident``. If the
    write fails the record is restored and the error propagates, so the client
    sees a failure and state that matches what is on disk.
    """
    payload = _dump(incident)
    database = persistence_path()
    try:
        await asyncio.to_thread(storage.write_incident, incident["incident_id"], payload, database=database)
    except Exception:
        _restore(incident)
        raise
    _PERSISTED[incident["incident_id"]] = payload


def save_db(incident: Optional[dict] = None):
    """Synchronous write: one incident, or with no argument the whole set.

    Request handlers use ``commit``. This remains for startup, bulk clear and
    the pipeline's failure marker, which run outside a request's await chain.
    """
    database = persistence_path()
    if incident is not None:
        payload = _dump(incident)
        storage.write_incident(incident["incident_id"], payload, database=database)
        _PERSISTED[incident["incident_id"]] = payload
        return
    payloads = {i["incident_id"]: _dump(i) for i in ACTIVE_INCIDENTS}
    storage.replace_incidents(payloads, database=database)
    _PERSISTED.clear()
    _PERSISTED.update(payloads)


def _legacy_records(database: Path):
    """Records from the earlier single-snapshot layouts, or None if there are none."""
    snapshot = load_collection("incidents", None, database=database)
    if snapshot is not None:
        return snapshot
    legacy = DB_FILE if DB_FILE != _LEGACY_DB_FILE else database.parent / "incidents_db.json"
    return json.loads(legacy.read_text(encoding="utf-8")) if legacy.exists() else None


_MIGRATED_MARKER = "migration:incident_rows"


def load_db():
    database = persistence_path()
    data = [json.loads(payload) for payload in storage.load_incident_payloads(database=database)]
    migrated = load_collection(_MIGRATED_MARKER, False, database=database)
    if not data and not migrated:
        # One-time migration; the legacy snapshot and JSON stay in place for
        # rollback. The marker stops them coming back once incidents have been
        # cleared or pruned.
        data = _legacy_records(database) or []
    if not isinstance(data, list) or any(not isinstance(i, dict) or not i.get("incident_id") for i in data):
        raise RuntimeError("Stored incidents must be a list of records with an incident_id")
    ACTIVE_INCIDENTS.clear()
    ACTIVE_INCIDENTS.extend(data)
    # An in-process task cannot survive a restart. Fail interrupted reports
    # visibly so a citizen can retry instead of polling forever.
    for incident in ACTIVE_INCIDENTS:
        if incident.get("status") == "processing":
            incident["status"] = "failed"
            incident["error"] = "Processing was interrupted by a server restart. Please submit again."
    save_db()
    if not migrated:
        save_collection(_MIGRATED_MARKER, True, database=database)


def prune_finished() -> list[str]:
    """Drop the oldest finished calls beyond KEEP_FINISHED. Returns the removed ids."""
    finished = [i for i in ACTIVE_INCIDENTS if i.get("status") in _FINISHED]
    excess = finished[: max(0, len(finished) - KEEP_FINISHED)]
    if not excess:
        return []
    removed = [i["incident_id"] for i in excess]
    storage.delete_incidents(removed, database=persistence_path())
    gone = set(removed)
    ACTIVE_INCIDENTS[:] = [i for i in ACTIVE_INCIDENTS if i["incident_id"] not in gone]
    for incident_id in removed:
        _PERSISTED.pop(incident_id, None)
    return removed


load_db()


MEDICAL_UNITS = {"Ambulance", "Medical"}
VehicleType = Literal["Ambulance", "Medical", "Fire Engine", "Police Cruiser", "Disaster Rescue"]

# The offline road graph covers this box; a small margin admits callers on its
# edge. Reports from elsewhere cannot be routed, so they are refused up front
# instead of reaching the dispatcher as a plan with no route.
_AREA_MARGIN_DEG = 0.05


def _service_area() -> dict:
    from agents.local_router import local_router
    box = (local_router.meta or {}).get("bbox") or {"south": 12.8, "west": 77.45, "north": 13.1, "east": 77.78}
    return {"south": box["south"] - _AREA_MARGIN_DEG, "north": box["north"] + _AREA_MARGIN_DEG,
            "west": box["west"] - _AREA_MARGIN_DEG, "east": box["east"] + _AREA_MARGIN_DEG}


def in_service_area(lat: float, lng: float) -> bool:
    box = _service_area()
    return box["south"] <= lat <= box["north"] and box["west"] <= lng <= box["east"]


class IncidentReportPayload(BaseModel):
    citizen_text: str = Field(min_length=1, max_length=2000)
    citizen_name: Optional[str] = Field(default=None, max_length=100)
    citizen_phone: Optional[str] = Field(default=None, max_length=32)
    vehicle_required: Optional[VehicleType] = "Ambulance"
    include_ambulance_backup: Optional[bool] = False
    location: Coordinate
    input_modality: str = Field(default="text", max_length=32)
    timestamp: Optional[str] = Field(default=None, max_length=64)

def wants_backup(payload: IncidentReportPayload) -> bool:
    """A backup ambulance accompanies a fire, police or rescue unit, never an ambulance."""
    return bool(payload.include_ambulance_backup) and (payload.vehicle_required or "Ambulance") not in MEDICAL_UNITS


class ApprovePayload(BaseModel):
    dispatcher_id: str = Field(min_length=1, max_length=100)
    approved_hospital: str = Field(min_length=1, max_length=200)
    approved_route: str = Field(max_length=200)

# Demo click-to-call numbers (no live hospital/unit directory feed).
DEMO_HOSPITAL_PHONES = {
    "Aster CMI Hospital (Hebbal)": "+91 80 4342 0107",
    "Manipal Hospital (Old Airport Road)": "+91 80 2502 4444",
    "Fortis Hospital (Bannerghatta Road)": "+91 80 6621 4444",
    "Sri Jayadeva Institute of Cardiovascular Sciences and Research": "+91 80 2297 7200",
    "NIMHANS (National Institute of Mental Health and Neurosciences)": "+91 80 2699 5000",
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


# Waits between attempts to store a pipeline failure. The last value repeats.
_FAIL_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)


async def _mark_failed(incident_id: str, exc: Exception) -> None:
    """Record a pipeline failure, retrying until the write succeeds.

    Each attempt takes the lock, so it decides after any pending write (for
    example a rejection) has committed or been rolled back, and only a record
    still "processing" is changed. No other task would finish the report, so a
    failed write is retried with backoff rather than left "processing"; a
    restart also marks it failed.
    """
    attempt = 0
    while True:
        async with _INCIDENTS_LOCK:
            inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
            if inc is None or inc.get("status") != "processing":
                return
            inc["status"] = "failed"
            inc["error"] = str(exc) or "Pipeline failed"
            try:
                await commit(inc)
                return
            except Exception:
                logger.exception("Could not store the failure of %s; will retry", log_safe(incident_id))
        delay = _FAIL_RETRY_DELAYS[min(attempt, len(_FAIL_RETRY_DELAYS) - 1)]
        attempt += 1
        await asyncio.sleep(delay)


def snapshot(value):
    """A deep copy taken under _INCIDENTS_LOCK, so a response never carries a
    change whose write is still pending or later rolled back."""
    return copy.deepcopy(value)


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
                    inc["triage_notices"] = access_res.incident_report.triage_notices
                    inc["input_modality"] = modality.value
                    found = True
                    # Store now, so a later rollback cannot drop the triage result.
                    await commit(inc)
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
        plan_route = fusion_res.recommended_route
        route_degraded = plan_route.confidence <= 0 or plan_route.eta <= 0 or len(route_coords) < 2
        new_citizen_view = {
            "message": f"Dispatching from {nearest_hub['name']}. Destination Hospital: {hosp.name}.",
            # No usable route means no ETA to show; approval re-routes and refuses if it still fails.
            "eta_minutes": None if route_degraded else max(1, int(plan_route.eta // 60)),
            "route_degraded": route_degraded,
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
            "include_ambulance_backup": wants_backup(payload),
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
                    await commit(inc)
                    break
            if not found:
                logger.warning("Pipeline: incident %s no longer present after fusion step — aborting", incident_id)
    except Exception as exc:
        logger.exception("Pipeline failed for %s", incident_id)
        await _mark_failed(incident_id, exc)

@router.post("")
async def report_incident(payload: IncidentReportPayload, response: Response):
    """Step 1 - Citizen raises emergency. Returns 202 immediately."""
    if not payload.citizen_text.strip():
        raise HTTPException(status_code=422, detail="Describe the emergency")
    if not in_service_area(payload.location.lat, payload.location.lng):
        raise HTTPException(
            status_code=422,
            detail="That location is outside the Bengaluru area this service covers. Call 112.",
        )
    incident_id = f"INC-{uuid.uuid4().hex}"
    
    incident = {
        "incident_id": incident_id,
        "location": payload.location.model_dump(),
        "citizen_text": payload.citizen_text,
        "citizen_name": payload.citizen_name or "Anonymous Citizen",
        "citizen_phone": payload.citizen_phone or "Unregistered",
        "vehicle_required": payload.vehicle_required or "Ambulance",
        "include_ambulance_backup": wants_backup(payload),
        "input_modality": _resolve_modality(payload.input_modality).value,
        "status": "processing",
        "action_plan": None
    }
    async with _INCIDENTS_LOCK:
        ACTIVE_INCIDENTS.append(incident)
        await commit(incident)  # on failure the record is removed and the caller gets an error
        try:
            removed = await asyncio.to_thread(prune_finished)
        except Exception:
            # Housekeeping only: the report is stored and must still be processed.
            logger.exception("Pruning finished incidents failed")
            removed = []
    if removed:
        from routers.chat import manager
        manager.forget(removed)
    
    # Start background processing pipeline
    # Keep a reference: the event loop holds tasks weakly, and a collected task
    # would leave the report in "processing" for good.
    task = asyncio.create_task(run_pipeline(incident_id, payload))
    _PIPELINE_TASKS.add(task)
    task.add_done_callback(_PIPELINE_TASKS.discard)
    
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "incident_id": incident_id,
        "status": "processing",
        "poll_url": f"/api/incidents/{incident_id}"
    }

@router.get("/{incident_id}")
async def get_incident(incident_id: str):
    """Step 3 - Citizen screen polls."""
    async with _INCIDENTS_LOCK:  # waits for any write in progress to commit or roll back
        for inc in ACTIVE_INCIDENTS:
            if inc["incident_id"] == incident_id:
                return snapshot(inc)
    raise HTTPException(status_code=404, detail="Incident not found")

@router.get("/")
async def get_all_incidents():
    """For Dispatcher to see all active incidents."""
    async with _INCIDENTS_LOCK:
        incidents = snapshot(ACTIVE_INCIDENTS)
    for inc in incidents:
        inc.setdefault("severity", 0.5)  # the dispatcher queue sorts by it
    return {"incidents": incidents}

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
        await commit(inc)
        logger.info("PLAN_APPROVED for %s by %s: hospital=%s",
                    log_safe(incident_id), log_safe(payload.dispatcher_id), log_safe(target["name"]))
        return inc


@router.post("/{incident_id}/reject")
async def reject_incident(incident_id: str, dispatcher_id: str = Query(default="DISPATCHER-01", min_length=1, max_length=100)):
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
                await commit(inc)
                logger.info("REJECTED %s by %s", log_safe(incident_id), log_safe(dispatcher_id))
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
            await commit(inc)
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
            if (inc.get("vehicle_required") or "Ambulance") in MEDICAL_UNITS:
                # No driver can take a second ambulance on a medical call, so it
                # would be a required unit that can never arrive.
                raise HTTPException(status_code=409, detail="An ambulance is already the primary unit on this call")
            inc["include_ambulance_backup"] = True
            inc["unit_statuses"] = unit_statuses(inc)
            if "citizen_view" in inc:
                inc["citizen_view"]["include_ambulance_backup"] = True
                inc["citizen_view"]["unit_statuses"] = inc["unit_statuses"]
            await commit(inc)
            return {"status": "ambulance_requested", "incident_id": incident_id}
    raise HTTPException(status_code=404, detail="Incident not found")
