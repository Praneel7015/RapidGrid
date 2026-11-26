"""
Incident Router for live Unified React App.
Matches the event-driven async flow described in the trace.
"""
from __future__ import annotations

import copy
import os
import time
import uuid
import asyncio
import logging
import re
from typing import List, Dict, Any, Literal, Optional
from fastapi import APIRouter, Query, Request, Response, status, HTTPException
from staff import require as require_staff
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
import fleet
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
    if not any(i is incident for i in ACTIVE_INCIDENTS):
        # Cleared meanwhile (a demo reset or a purge): a late task must not
        # write the record back to disk.
        return
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
    # Records saved before the contacts directory carried demo phone numbers;
    # they are not part of a record any more.
    for incident in ACTIVE_INCIDENTS:
        for key in ("unit_phone", "hospital_phone"):
            incident.pop(key, None)
            if isinstance(incident.get("citizen_view"), dict):
                incident["citizen_view"].pop(key, None)
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

# What each kind of unit does once it reaches the scene. Only an ambulance
# carries a patient on to a hospital; police, fire and rescue units respond
# to the scene and finish there (unless an ambulance backup is added, which
# then needs a receiving hospital). Kept as data so a new unit type is one
# entry, not new branches through the code.
RESPONSE_PROFILES: dict[str, dict] = {
    "Ambulance": {"to_hospital": True, "label": "Ambulance"},
    "Medical": {"to_hospital": True, "label": "Ambulance"},
    "Fire Engine": {"to_hospital": False, "label": "Fire engine"},
    "Police Cruiser": {"to_hospital": False, "label": "Police unit"},
    "Disaster Rescue": {"to_hospital": False, "label": "Rescue unit"},
}


def response_profile(vehicle: str | None) -> dict:
    return RESPONSE_PROFILES.get(vehicle or "Ambulance", RESPONSE_PROFILES["Ambulance"])


def needs_hospital(vehicle: str | None, ambulance_backup: bool) -> bool:
    """A call needs a receiving hospital when its unit, or its backup, is medical."""
    return response_profile(vehicle)["to_hospital"] or bool(ambulance_backup)
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
    anonymous: bool = False
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
    # Required for calls that carry a patient; scene-only calls have none.
    approved_hospital: Optional[str] = Field(default=None, min_length=1, max_length=200)
    approved_route: str = Field(max_length=200)

def _resolve_modality(raw: str) -> AccessibilityInputModality:
    try:
        return AccessibilityInputModality(str(raw or "text").strip().lower())
    except ValueError:
        return AccessibilityInputModality.TEXT


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


# Caller details are personal data: no incident response carries them. They
# are read through GET /{incident_id}/caller, which checks who is asking and
# records every reveal (OWASP A01).
_PRIVATE_FIELDS = ("citizen_name", "citizen_phone", "citizen_id", "caller_reveals",
                   # Phone numbers come from GET /{incident_id}/contacts, by role.
                   "unit_phone", "hospital_phone")


def security_same_origin(request) -> bool:
    import security

    return bool(request.headers.get("origin")) and security.same_origin(request)


# A run of 10+ digits (up to three spaces, dots, dashes or slashes between,
# a leading + allowed): a phone number a caller typed into the report.
_PHONE_IN_TEXT = re.compile(r"\+?\(?\d(?:[ \t./,()-]{0,4}\d){9,}\)?")
# A latitude/longitude pair ("13.035 77.597", "13.0358, 77.5970") is a
# place, not a number to hide; dates have too few digits to match at all.
_COORDINATES = re.compile(r"\d{1,3}\.\d+[ ,]+\d{1,3}\.\d+")
# A date ("12.05.2026", "12-05-26"), possibly with a time after it, is not
# a phone number either. Only a whole match is exempt: a date followed by a
# phone number ("12.05.2026 9876543210") is still masked.
_TIME = r"(?:[ \t]+(\d{1,2})(?:[.:](\d{2}))?(?:[.:](\d{2}))?)?"
_DATE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](?:\d{4}|\d{2})" + _TIME)
_ISO_DATE = re.compile(r"(?:19|20)\d{2}-(\d{1,2})-(\d{1,2})" + _TIME)


def _is_date(match: str) -> bool:
    """A real date (day 1-31, month 1-12, either order) and time, not a
    phone number shaped like one ("98-76-5432 10")."""
    m = _DATE.fullmatch(match) or _ISO_DATE.fullmatch(match)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    if not ((1 <= a <= 31 and 1 <= b <= 12) or (1 <= a <= 12 and 1 <= b <= 31)):
        return False
    hour, minute, second = m.group(3), m.group(4), m.group(5)
    if hour is not None and int(hour) > 23:
        return False
    return all(v is None or int(v) <= 59 for v in (minute, second))


def _keep(match: str) -> bool:
    match = match.strip()
    return bool(_COORDINATES.fullmatch(match) or _is_date(match))


def _mask_numbers(text):
    if not isinstance(text, str):
        return text
    def replace(m):
        if not _keep(m.group(0)):
            return "[number hidden]"
        # The phone pattern stops at ":", so a colon time's minutes fall
        # just outside the match: check them with the same bound.
        after = re.match(r":(\d{2})", m.string[m.end():])
        if after and int(after.group(1)) > 59:
            return "[number hidden]"
        return m.group(0)

    return _PHONE_IN_TEXT.sub(replace, text)


def _article(word: str) -> str:
    return "an" if (word or "x")[0].lower() in "aeiou" else "a"


def public_incident(incident: dict) -> dict:
    """A deep copy without caller details or the reveal log. Phone numbers a
    caller typed into the report are masked too: the incident id is all it
    takes to read this view, and the caller-reveal check guards the number."""
    copy_ = copy.deepcopy(incident)
    for key in _PRIVATE_FIELDS:
        copy_.pop(key, None)
    if "citizen_text" in copy_:
        copy_["citizen_text"] = _mask_numbers(copy_["citizen_text"])
    view = copy_.get("citizen_view")
    if isinstance(view, dict):
        for key in ("unit_phone", "hospital_phone"):  # records saved before contacts existed
            view.pop(key, None)
    copy_["caller_hidden"] = True
    return copy_


def snapshot(value):
    """A deep copy taken under _INCIDENTS_LOCK, so a response never carries a
    change whose write is still pending or later rolled back."""
    return copy.deepcopy(value)


async def _phase1(veh_type: str, location) -> tuple[dict, list, int | None, object | None]:
    """Nearest station of this unit type and its route to the scene."""
    hub = find_nearest_hub(veh_type, location.lat, location.lng)
    try:
        route = await asyncio.to_thread(route_agent.compute_route, RouteRequest(
            origin=Coordinate(lat=hub["lat"], lng=hub["lng"]),
            destination=location,
        ))
        if route.confidence <= 0 or len(route.coordinates) < 2:
            raise ValueError("No usable hub-to-scene route")
        return hub, [{"lat": c.lat, "lng": c.lng} for c in route.coordinates], max(1, int(route.eta // 60)), route
    except Exception:
        return hub, [], None, None


async def _plan_scene_response(incident_id: str, payload, access_res) -> None:
    """Plan a police, fire or rescue call: station to scene, no hospital."""
    veh_type = payload.vehicle_required or "Ambulance"
    profile = response_profile(veh_type)
    hub, coords, eta, route = await _phase1(veh_type, payload.location)
    where = f"{hub['name']} to the scene"
    explanation = (
        f"{profile['label']} from {where}"
        + (f", about {eta} min by road." if eta else ". No road route was found; check the location before dispatching.")
        + " No hospital leg: this unit responds to the scene."
    )
    plan = {
        "response_profile": "scene",
        "incident_id": incident_id,
        "vehicle_required": veh_type,
        "recommended_hospital": None,
        "all_hospitals": [],
        "recommended_route": None if route is None else {
            "route_id": getattr(route, "route_id", None),
            "coordinates": coords,
            "eta": route.eta,
            "distance": route.distance,
            "data_freshness": str(getattr(getattr(route, "data_freshness", None), "value", "") or "") or None,
        },
        "explanation": explanation,
        "overall_confidence": route.confidence if route is not None else 0.0,
    }
    view = {
        "message": f"Dispatching {_article(profile['label'])} {profile['label'].lower()} from {where}.",
        "eta_minutes": eta,
        "route_degraded": eta is None,
        "response_profile": "scene",
        "hospital_name": None,
        "hospital_location": None,
        "origin": {"lat": payload.location.lat, "lng": payload.location.lng},
        "route_coordinates": [],
        "phase1_hub": hub,
        "phase1_route_coordinates": coords,
        "phase1_eta_minutes": eta,
        "phase1_route_degraded": eta is None,
        "distance_meters": route.distance if route is not None else None,
        "emergency_type": access_res.incident_report.incident_type.value,
        "severity": access_res.incident_report.severity_estimate,
        "vehicle_required": veh_type,
        "include_ambulance_backup": False,
    }
    async with _INCIDENTS_LOCK:
        for inc in ACTIVE_INCIDENTS:
            if inc["incident_id"] == incident_id and inc.get("status") == "processing":
                inc.update({"action_plan": plan, "citizen_view": view, "status": "awaiting_dispatcher_approval"})
                await commit(inc)
                return
    logger.warning("Pipeline: incident %s no longer present after scene planning — aborting", log_safe(incident_id))


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

        veh_type = payload.vehicle_required or "Ambulance"
        if not needs_hospital(veh_type, wants_backup(payload)):
            await _plan_scene_response(incident_id, payload, access_res)
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
        nearest_hub, p1_coords, p1_eta, _ = await _phase1(veh_type, payload.location)

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
            "response_profile": "hospital",
            # No usable route means no ETA to show; approval re-routes and refuses if it still fails.
            "eta_minutes": None if route_degraded else max(1, int(plan_route.eta // 60)),
            "route_degraded": route_degraded,
            "hospital_name": hosp.name,
            "hospital_location": {"lat": hosp.location.lat, "lng": hosp.location.lng},
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

# Reports: each one starts the planning pipeline (CPU and outside lookups).
# A per-client limit, and a cap on reports still waiting for a dispatcher, keep
# a flood from burying real calls or the server (A04). Over a limit, the
# caller is told to call 112, which always works.
REPORT_LIMIT = int(os.getenv("RAPIDGRID_REPORT_LIMIT", "20") or 20)
REPORT_WINDOW_S = 600
PENDING_CAP = 150  # anonymous reports only; a signed-in citizen is never turned away
_reports: dict[str, list[float]] = {}


def _admit_report(request, signed_in: bool = False):
    """Refuse the report if a limit is reached; otherwise return a callable
    that charges it. The caller charges only once the report is stored, under
    the same lock, so a failed write does not use up a permit."""
    import time as _time

    import security

    pending = sum(1 for i in ACTIVE_INCIDENTS
                  if i.get("status") in {"processing", "awaiting_dispatcher_approval"} and not i.get("citizen_id")
                  and not i.get("background"))
    if not signed_in and pending >= PENDING_CAP:
        raise HTTPException(status_code=429, detail="The service is very busy. Call 112 now.")
    if request is None:
        return lambda: None
    client = security.client_key(request)
    now = _time.monotonic()
    recent = [t for t in _reports.get(client, []) if now - t < REPORT_WINDOW_S]
    _reports[client] = recent
    if len(recent) >= REPORT_LIMIT:
        raise HTTPException(status_code=429, detail="Too many reports from here. Call 112 now.",
                            headers={"Retry-After": str(int(REPORT_WINDOW_S - (now - recent[0])) + 1)})
    return lambda: _charge_report(client, now)


def _charge_report(client: str, now: float) -> None:
    _reports.setdefault(client, []).append(now)
    if len(_reports) > 2000:
        for key in [k for k, v in _reports.items() if not v or now - v[-1] >= REPORT_WINDOW_S]:
            del _reports[key]


@router.post("")
async def report_incident(payload: IncidentReportPayload, response: Response, request: Request = None):
    """Step 1 - Citizen raises emergency. Returns 202 immediately."""
    if not payload.citizen_text.strip():
        raise HTTPException(status_code=422, detail="Describe the emergency")
    if not in_service_area(payload.location.lat, payload.location.lng):
        raise HTTPException(
            status_code=422,
            detail="That location is outside the Bengaluru area this service covers. Call 112.",
        )
    incident_id = f"INC-{uuid.uuid4().hex}"
    # A signed-in citizen's report carries their account's name and number;
    # an anonymous report keeps whatever the caller typed (it is never refused).
    from citizens import current_async as current_citizen

    # An explicitly anonymous report never carries an account's details,
    # even if this browser is still signed in (a shared phone).
    me = None if payload.anonymous else await current_citizen(request)
    incident = {
        "incident_id": incident_id,
        "location": payload.location.model_dump(),
        "citizen_text": payload.citizen_text,
        "citizen_id": me["id"] if me else None,
        "citizen_name": me["name"] if me else (payload.citizen_name or "Anonymous Citizen"),
        "citizen_phone": me["phone"] if me else (payload.citizen_phone or "Unregistered"),
        "vehicle_required": payload.vehicle_required or "Ambulance",
        "include_ambulance_backup": wants_backup(payload),
        "input_modality": _resolve_modality(payload.input_modality).value,
        "status": "processing",
        "action_plan": None
    }
    async with _INCIDENTS_LOCK:
        try:
            account_still_there = (not me) or bool(await asyncio.to_thread(
                storage.load_citizen, me["id"], database=storage.data_path()))
        except Exception:
            # Cannot check: keep the report (an emergency), without the
            # account's details.
            logger.exception("Could not re-check the caller's account; filing the report without it")
            account_still_there = False
        if me and not account_still_there:
            # The account was deleted (a purge) while this report was on its
            # way: keep none of its details.
            incident.update(citizen_id=None, citizen_name=payload.citizen_name or "Anonymous Citizen",
                            citizen_phone=payload.citizen_phone or "Unregistered")
            me = None
        # Checked and appended without a suspension between them, so
        # concurrent reports cannot overshoot the pending cap.
        charge = _admit_report(request, signed_in=bool(me))
        ACTIVE_INCIDENTS.append(incident)
        await commit(incident)  # on failure the record is removed and the caller gets an error
        charge()
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
                return public_incident(inc)
    raise HTTPException(status_code=404, detail="Incident not found")

@router.post("/{incident_id}/caller")
async def reveal_caller(incident_id: str, response: Response, request: Request = None):
    """The caller's name and number, for dispatch, a unit on this call (the
    assigned unit, the one that claimed it, or its backup), or the caller.
    Every reveal is recorded on the incident. A POST, because it writes that
    record; never cached.

    With staff sign-in switched off (a local open demo, no staff PINs
    configured) the staff checks allow everyone, so anyone can reveal; the
    README says so. The hosted showcase runs with staff PINs."""
    import staff
    from citizens import current_async as current_citizen
    from routers.driver import _assignments

    response.headers["Cache-Control"] = "no-store"
    if request is not None and not security_same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin or origin-less request refused")
    async with _INCIDENTS_LOCK:
        # The session is checked under the same lock as the decision.
        me = await current_citizen(request)
        inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        owner = bool(me and inc.get("citizen_id") == me["id"])
        if owner:
            who = ("citizen", "self")
        else:
            who = None
            last: HTTPException = HTTPException(status_code=401, detail="Staff sign-in required")
            on_call = {inc.get("assigned_unit"), *_assignments(inc).values()} - {None, ""}
            try:
                who = staff.require(request, "dispatcher")
            except HTTPException as denied:
                last = denied
                for unit_id in sorted(on_call):
                    try:
                        who = staff.require(request, "unit", unit_id)
                        break
                    except HTTPException as refused:
                        last = refused
            if who is None:
                raise last
        inc.setdefault("caller_reveals", []).append(
            {"at": time.time(), "role": who[0], "by": who[1]})
        inc["caller_reveals"] = inc["caller_reveals"][-50:]
        await commit(inc)
        logger.info("Caller details revealed for %s to %s", log_safe(incident_id), log_safe(who[0]))
        return {"name": inc.get("citizen_name"), "phone": inc.get("citizen_phone"),
                "registered": bool(inc.get("citizen_id"))}


@router.get("/{incident_id}/contacts")
async def incident_contacts(incident_id: str, response: Response, request: Request = None):
    """Who to call about this incident, by who is asking.

    Everyone: the 112 line. The signed-in caller: also their unit and
    dispatch. Dispatch and the units on the call: also the receiving
    hospital's ER and ICU. The receiving hospital: the unit and dispatch.
    Numbers come from the private contacts directory; nothing is invented.
    An anonymous caller gets only 112: incident ids are visible in the open
    incident list, so knowing one proves nothing.
    """
    import contacts
    import security
    import staff
    from citizens import current_async as current_citizen
    from routers.driver import _assignments

    response.headers["Cache-Control"] = "no-store"
    # The caller lookup awaits, so it runs before the snapshot: the role checks
    # below then use one assignment state with no suspension point between.
    me = await current_citizen(request)
    async with _INCIDENTS_LOCK:
        inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        on_call = {inc.get("assigned_unit"), *_assignments(inc).values()} - {None, ""}
        # The unit whose number is disclosed, read with the same snapshot.
        current_unit = fleet.unit_for_incident(incident_id)
        approved_hospital = inc.get("approved_hospital_id")
        raw_hospital = approved_hospital or (
            ((inc.get("action_plan") or {}).get("recommended_hospital") or {}).get("hospital_id"))
        owner_id = inc.get("citizen_id")
    from routers.hospital import _resolve

    hospital_id = (_resolve(str(raw_hospital)) or raw_hospital) if raw_hospital else None
    # Only the destination dispatch approved is "the receiving hospital"; a
    # recommendation is a candidate until then.
    receiving_id = (_resolve(str(approved_hospital)) or approved_hospital) if approved_hospital else None

    def allowed(role: str, subject: str | None = None) -> bool:
        # Staff numbers need a real staff (or operator) sign-in. With staff
        # sign-in off (no PINs configured), nobody is treated as staff here:
        # an open demo must not hand every number to every visitor.
        if not staff.enabled():
            return False
        try:
            staff.require(request, role, subject)
        except HTTPException:
            return False
        return True

    is_owner = bool(me and owner_id and me["id"] == owner_id)
    is_dispatch = allowed("dispatcher")
    is_unit = any(allowed("unit", u) for u in sorted(on_call))
    is_hospital = bool(receiving_id) and allowed("hospital", receiving_id)
    is_staff = is_dispatch or is_unit or is_hospital

    result: dict = {"hotline_112": contacts.hotline(), "dispatch": None, "unit": None, "hospital": None}
    directory = contacts.directory()
    if is_owner or is_dispatch or is_unit or is_hospital:
        if directory.dispatch:
            result["dispatch"] = {"name": directory.dispatch.name, "phone": directory.dispatch.phone}
        unit = current_unit
        entry = contacts.unit(unit["unit_id"]) if unit else None
        if entry:
            result["unit"] = {"unit_id": entry.unit_id, "phone": entry.phone}
            if is_staff:  # the crew's name only for staff, not the caller
                result["unit"]["crew"] = entry.crew or None
    if (is_dispatch or is_unit) and hospital_id:
        entry = contacts.hospital(hospital_id)
        if entry:
            result["hospital"] = {"name": entry.name, "emergency_phone": entry.emergency_phone,
                                  "icu_phone": entry.icu_phone}
    return result


@router.get("/")
async def get_all_incidents(response: Response = None, request: Request = None):
    """For Dispatcher to see all active incidents. With staff sign-in on, only
    dispatch: the list holds every caller's location and words, and the
    incident ids that open each call's channel (A01)."""
    require_staff(request, "dispatcher")
    if response is not None:
        response.headers["Cache-Control"] = "no-store"  # not kept for the next person at this browser
    async with _INCIDENTS_LOCK:
        incidents = [public_incident(i) for i in ACTIVE_INCIDENTS]
    for inc in incidents:
        inc.setdefault("severity", 0.5)  # the dispatcher queue sorts by it
    return {"incidents": incidents}

def _reserve_unit(inc: dict) -> tuple[str, bool]:
    """Reserve a free unit for a call being approved (its planned station's
    first). Returns (unit id, whether this call created the reservation).
    With no suitable unit free, nothing changes and dispatch is told so: an
    approved call with no unit would sit "dispatched" with no crew."""
    view = inc.get("citizen_view") or {}
    hub = view.get("phase1_hub") or {}
    vehicle = inc.get("vehicle_required") or "Ambulance"
    if vehicle == "Medical":
        vehicle = "Ambulance"  # the fleet has no separate "Medical" units
    from routers.driver import units_on_calls

    held = fleet.unit_for_incident(inc["incident_id"])
    exclude = set(units_on_calls(inc["incident_id"]))
    if inc.get("background"):
        # The simulated city never takes a unit the scripted call may need.
        from agents.city_life import STAGE_UNITS

        exclude |= STAGE_UNITS
    unit = fleet.assign(inc["incident_id"], vehicle, near=dict(inc["location"]),
                        prefer_station=hub.get("name"), exclude=exclude)
    if unit is None:
        logger.warning("No free %s for %s", log_safe(vehicle), log_safe(inc["incident_id"]))
        raise HTTPException(status_code=409, detail=(
            f"No {vehicle.lower()} is free right now. The call stays in the queue; "
            "approve it again when a unit is back."))
    return unit["unit_id"], held is None


def _attach_unit(inc: dict, unit_id: str) -> None:
    inc["assigned_unit"] = unit_id
    inc.setdefault("citizen_view", {})["assigned_unit"] = unit_id


def _assign_unit(inc: dict) -> None:
    """Reserve and attach a unit in one step (no approval write follows)."""
    unit_id, _ = _reserve_unit(inc)
    _attach_unit(inc, unit_id)


def _distance_m(a: dict, b: dict) -> float:
    from agents.monitor import _dist
    return _dist(a, b)


async def _commit_approval(inc: dict, unit_id: str, created: bool) -> None:
    """Store the approval; if that fails, free the unit this approval
    reserved (a unit the call already held stays held)."""
    try:
        await commit(inc)
    except Exception:
        if created and (fleet.unit_for_incident(inc["incident_id"]) or {}).get("unit_id") == unit_id:
            # Freed in memory now. If storing that also fails, the stored
            # record still names this call; startup frees units held by a
            # call that is not being driven, so it cannot stay stuck.
            fleet.release(inc["incident_id"])
        raise
    # The plan routed from the nearest station; if the unit sent is based
    # elsewhere (that station's units were busy), route from where it is.
    # Decided by where the unit is, not by station names: a unit from the
    # planned station that is elsewhere (back from a call) needs it too, and
    # one already at the route's start does not.
    unit = fleet.get(unit_id) or {}
    planned = (inc.get("citizen_view") or {}).get("phase1_route_coordinates") or []
    pos = unit.get("position") or {}
    if unit and planned and "lat" in pos and _distance_m(pos, planned[0]) > 300:
        task = asyncio.create_task(_align_first_leg(inc["incident_id"], unit_id,
                                                    (inc.get("citizen_view") or {}).get("phase1_route_coordinates")))
        _ALIGN_TASKS.add(task)
        task.add_done_callback(_ALIGN_TASKS.discard)


_ALIGN_TASKS: set = set()


async def _align_first_leg(incident_id: str, unit_id: str, planned: Optional[list]) -> None:
    """Replace the to-scene route with one from the assigned unit's
    position, so the agent does not see it as off its route from the start
    (and the simulator does not drive a straight line to the planned one)."""
    from agents.local_router import local_router

    try:
        await _align_first_leg_inner(incident_id, unit_id, planned, local_router)
    except Exception:
        logger.exception("Re-routing %s from where it is failed", log_safe(unit_id))


async def _align_first_leg_inner(incident_id: str, unit_id: str, planned, local_router) -> None:
    unit = fleet.get(unit_id)
    async with _INCIDENTS_LOCK:
        inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        scene = dict(inc["location"]) if inc and inc.get("location") else None
    if unit is None or scene is None or not local_router.available:
        return
    pos = unit["position"]
    try:
        path = await asyncio.to_thread(local_router.find_path, (pos["lat"], pos["lng"]), (scene["lat"], scene["lng"]))
    except Exception:
        logger.exception("Could not route %s from its own station", log_safe(unit_id))
        return
    if not path:
        return
    async with _INCIDENTS_LOCK:
        inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        if (inc is None or inc.get("status") != "dispatched" or inc.get("assigned_unit") != unit_id
                or (inc.get("citizen_view") or {}).get("phase1_route_coordinates") != planned):
            return  # the call moved on (or the route was already changed)
        view = inc.setdefault("citizen_view", {})
        view["phase1_hub"] = {"name": unit["station"], **unit["station_location"]}
        view["phase1_route_coordinates"] = path["coordinates"]
        view["phase1_eta_minutes"] = max(1, round(path["travel_time_s"] / 60))
        try:
            await commit(inc)
        except Exception:
            logger.exception("Could not store the route from %s's station", log_safe(unit_id))


@router.post("/{incident_id}/approve")
async def approve_incident(incident_id: str, payload: ApprovePayload, request: Request = None):
    """Approve a reviewed destination without allowing terminal cases to rewind."""
    who = require_staff(request, "dispatcher")
    # The audit trail names the signed-in dispatcher, not a value the request
    # chose; in the open demo, the given id is kept.
    payload.dispatcher_id = who[1] if who[0] == "dispatcher" else payload.dispatcher_id
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
        if plan.get("response_profile") == "scene":
            view = dict(inc.get("citizen_view") or {})
            view.update({"status": "dispatched",
                         "message": f"Help is on the way: {response_profile(inc.get('vehicle_required'))['label'].lower()} dispatched to the scene."})
            backup_hospital = plan.get("recommended_hospital") if inc.get("include_ambulance_backup") else None

            def open_now(h):
                return (_resolve(str(h.get("hospital_id", ""))) or h.get("hospital_id")) not in hospital_agent.diverted

            if backup_hospital and not open_now(backup_hospital):
                # The backup ambulance needs a destination that is open: the
                # next ranked one, or none and dispatch is told.
                backup_hospital = next((h for h in plan.get("all_hospitals") or []
                                        if h.get("location") and open_now(h)), None)
                if backup_hospital is None:
                    raise HTTPException(status_code=409, detail=(
                        "Every ranked hospital is on divert; the backup ambulance has nowhere to go"))
                view.update({"hospital_name": backup_hospital["name"],
                             "hospital_location": backup_hospital["location"]})
                # The route to the diverted hospital no longer applies; pickup
                # routes afresh to this destination.
                for stale in ("route_coordinates", "eta_minutes", "distance_meters"):
                    view.pop(stale, None)
            unit_id, created = _reserve_unit(inc)
            inc.update({"status": "dispatched", "driver_stage": "pickup", "dispatcher_id": payload.dispatcher_id,
                        "approved_hospital_id": (backup_hospital or {}).get("hospital_id"), "citizen_view": view})
            inc["unit_statuses"] = {unit: "dispatched" for unit in required_units(inc)}
            view["unit_statuses"] = inc["unit_statuses"]
            _attach_unit(inc, unit_id)
            await _commit_approval(inc, unit_id, created)
            logger.info("PLAN_APPROVED for %s by %s: scene response",
                        log_safe(incident_id), log_safe(payload.dispatcher_id))
            return public_incident(inc)
        if not payload.approved_hospital:
            raise HTTPException(status_code=422, detail="Choose a hospital from the reviewed plan")
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
        unit_id, created = _reserve_unit(inc)
        view = dict(inc.get("citizen_view") or {})
        view.update(route)
        view.update({
            "status": "dispatched",
            "message": f"Help is being dispatched. Approved Hospital: {target['name']}.",
            "hospital_name": target["name"], "hospital_location": destination,
            "origin": origin, "emergency_type": inc.get("emergency_type", "general"),
            "severity": inc.get("severity", 0.5),
        })
        inc.update({"status": "dispatched", "driver_stage": "pickup", "dispatcher_id": payload.dispatcher_id,
                    "approved_hospital_id": canonical or target.get("hospital_id"),
                    "citizen_view": view})
        inc["unit_statuses"] = {unit: "dispatched" for unit in required_units(inc)}
        view["unit_statuses"] = inc["unit_statuses"]
        _attach_unit(inc, unit_id)
        await _commit_approval(inc, unit_id, created)
        logger.info("PLAN_APPROVED for %s by %s: hospital=%s",
                    log_safe(incident_id), log_safe(payload.dispatcher_id), log_safe(target["name"]))
        return public_incident(inc)


@router.post("/{incident_id}/reject")
async def reject_incident(incident_id: str, dispatcher_id: str = Query(default="DISPATCHER-01", min_length=1, max_length=100), request: Request = None):
    """Dispatcher rejects an incident plan — sets status to 'rejected' so it leaves the approval queue."""
    who = require_staff(request, "dispatcher")
    dispatcher_id = who[1] if who[0] == "dispatcher" else dispatcher_id  # audit trail from the session
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
                fleet.release(incident_id)
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
async def incident_arrived(incident_id: str, request: Request = None, unit_type: str = "primary"):
    """Record each unit independently; finish only when all required units arrive."""
    who = require_staff(request, "unit")
    if unit_type not in {"primary", "ambulance"}:
        raise HTTPException(status_code=422, detail="Unknown unit type")
    async with _INCIDENTS_LOCK:
        for inc in ACTIVE_INCIDENTS:
            if inc["incident_id"] != incident_id:
                continue
            if who[0] == "unit":
                # Only the unit holding this slot on this call can mark it (A01).
                from routers.driver import _assignments
                holder = _assignments(inc).get(unit_type) or (
                    inc.get("assigned_unit") if unit_type == "primary" else None)
                if who[1] != holder:
                    raise HTTPException(status_code=403, detail="Only the unit on this call can mark its arrival")
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
            # A delivered patient (medical handover by any unit) keeps the call in the
            # transport phase until every required unit has finished.
            handed_over = medical and states[unit_type] == "arrived"
            on_board = handed_over or "patient_picked_up" in states.values()
            inc["status"] = "completed" if complete else ("patient_picked_up" if on_board else "dispatched")
            inc["driver_stage"] = "completed" if complete else ("hospital" if inc["status"] == "patient_picked_up" else "pickup")
            if "citizen_view" in inc:
                inc["citizen_view"]["unit_statuses"] = states
                inc["citizen_view"]["status"] = inc["status"]
            await commit(inc)
            if complete:
                fleet.release(incident_id)
            return {"status": "arrived", "unit_type": unit_type, "unit_statuses": states, "incident_status": inc["status"]}
    raise HTTPException(status_code=404, detail="Incident not found")


@router.post("/clear")
async def clear_incidents(request: Request = None):
    # Bulk delete of every case record: with staff sign-in on, only dispatch.
    # In the open demo anyone may, as with the other demo resets.
    require_staff(request, "dispatcher")
    async with _INCIDENTS_LOCK:
        ACTIVE_INCIDENTS.clear()
        save_db()
    fleet.reset()
    # Also clear chat history so old messages don't surface on new incidents with the same ID.
    from routers.chat import manager
    await manager.clear_all()  # under the chat write lock, so no late insert survives
    return {"status": "cleared"}
async def _backup_destination(inc: dict) -> dict:
    """Rank hospitals from the scene and route there, for a backup ambulance
    added to a scene-only call (it has to take the patient somewhere)."""
    from agents.hospital_intelligence import NoHospitalCandidatesError, hospital_agent
    from models.schemas import EmergencyType, HospitalRequest
    from routers.driver import _route

    try:
        etype = EmergencyType(inc.get("emergency_type") or "general")
    except ValueError:
        etype = EmergencyType.GENERAL
    scene = dict(inc["location"])
    try:
        ranking = await hospital_agent.rank_hospitals(HospitalRequest(
            incident_location=Coordinate(**scene), emergency_type=etype))
    except NoHospitalCandidatesError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ranking.ranked_hospitals:
        raise HTTPException(status_code=409, detail="No hospital is accepting patients")
    ranked = [h.model_dump(mode="json") for h in ranking.ranked_hospitals]
    top = ranked[0]
    route = await _route(scene, dict(top["location"]))
    return {"hospital": top, "ranked": ranked, "route": route}


@router.post("/{incident_id}/request_ambulance")
async def request_ambulance_backup(incident_id: str, request: Request = None):
    require_staff(request, "dispatcher")
    async with _INCIDENTS_LOCK:
        inc = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        scene_only = (inc.get("action_plan") or {}).get("response_profile") == "scene"
        has_destination = bool((inc.get("citizen_view") or {}).get("hospital_location"))
    # A scene-only call has no hospital yet; find one before the backup goes.
    destination = await _backup_destination(inc) if scene_only and not has_destination else None
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
            if destination is not None and (inc.get("citizen_view") or {}).get("hospital_location"):
                # Another request attached a destination while this one was
                # planning (the crew may already be heading there): keep it.
                destination = None
            if destination is not None:
                from agents.hospital_intelligence import hospital_agent
                from routers.hospital import _resolve as _resolve_hospital

                hospital = destination["hospital"]
                hid = _resolve_hospital(str(hospital.get("hospital_id", ""))) or hospital.get("hospital_id")
                if hid in hospital_agent.diverted:
                    raise HTTPException(status_code=409, detail=(
                        "The nearest suitable hospital went on divert while planning; request the backup again"))
                plan = inc.setdefault("action_plan", {})
                plan["recommended_hospital"] = hospital
                plan["all_hospitals"] = destination["ranked"]
                view = inc.setdefault("citizen_view", {})
                view.update(destination["route"])
                view.update({
                    "hospital_name": hospital["name"],
                    "hospital_location": hospital["location"],
                })
                if inc.get("status") != "awaiting_dispatcher_approval":
                    inc["approved_hospital_id"] = hid
            inc["include_ambulance_backup"] = True
            inc["unit_statuses"] = unit_statuses(inc)
            if "citizen_view" in inc:
                inc["citizen_view"]["include_ambulance_backup"] = True
                inc["citizen_view"]["unit_statuses"] = inc["unit_statuses"]
            await commit(inc)
            return {"status": "ambulance_requested", "incident_id": incident_id}
    raise HTTPException(status_code=404, detail="Incident not found")
