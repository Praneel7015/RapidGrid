"""Driver assignments and validated pickup/transport transitions."""
from __future__ import annotations

import asyncio
from typing import Literal

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from staff import require as require_staff
from pydantic import BaseModel, Field, model_validator

from models.schemas import Coordinate, RouteRequest
from routers.incident import (
    ACTIVE_INCIDENTS, _INCIDENTS_LOCK, commit, required_units, route_agent, unit_statuses,
)

logger = logging.getLogger("geoagentic.driver")

router = APIRouter(prefix="/api/driver", tags=["Driver Queue"])


class ClaimPayload(BaseModel):
    incident_id: str
    driver_id: str = Field(default="drv-11", min_length=1, max_length=100)
    unit_type: Literal["primary", "ambulance"] = "primary"


class HospitalChangePayload(BaseModel):
    incident_id: str
    hospital_name: str | None = Field(default=None, max_length=200)
    # Preferred: names repeat (branches), ids do not. The name is a fallback.
    hospital_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _one_selector(self):
        if not self.hospital_id and not self.hospital_name:
            raise ValueError("Give hospital_id (or hospital_name)")
        return self


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
    # A call dispatch gave to a unit can be claimed (as primary) only by that unit.
    fleet_unit = incident.get("assigned_unit")
    if unit_type == "primary" and fleet_unit and driver_id != fleet_unit:
        raise HTTPException(status_code=409, detail=f"This call is assigned to {fleet_unit}")
    import staff
    if staff.enabled():
        # With staff sign-in, a unit holds only a slot dispatch gave it (A01):
        # primary is the dispatched unit; the backup slot needs a free (or
        # already this call's) ambulance.
        if unit_type == "primary" and not fleet_unit:
            raise HTTPException(status_code=409, detail="No unit has been dispatched to this call yet")
        if unit_type == "ambulance":
            import fleet
            unit = fleet.get(driver_id)
            if unit is None or unit.get("type") != "Ambulance":
                raise HTTPException(status_code=403, detail="Only an ambulance can take the backup slot")
            if unit.get("incident_id") not in (None, "", incident["incident_id"]):
                raise HTTPException(status_code=409, detail="This ambulance is on another call")
            # A backup claim is not in the fleet record: check the other calls.
            if driver_id in units_on_calls(except_incident=incident["incident_id"]):
                raise HTTPException(status_code=409, detail="This ambulance is on another call")


def units_on_calls(except_incident: str | None = None) -> set:
    """Units holding any slot (dispatched, claimed or backup) on an active
    call. The fleet record knows only the dispatched unit; a backup claim
    lives on the incident, so allocation must check here too."""
    held: set = set()
    for other in ACTIVE_INCIDENTS:
        if other.get("incident_id") == except_incident:
            continue
        if other.get("status") not in {"dispatched", "patient_picked_up"}:
            continue
        held.update(v for v in (other.get("assigned_unit"), *_assignments(other).values()) if v)
    return held


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
async def get_active_dispatches(response: Response, request: Request = None):
    # A driver must not act on a report before a dispatcher approves it.
    from routers.hospital import destination_on_divert

    who = _unit_or_dispatch(request)
    response.headers["Cache-Control"] = "no-store"
    async with _INCIDENTS_LOCK:
        from routers.incident import public_incident

        dispatches = _active_dispatches(destination_on_divert)
        if who[0] == "unit":
            # A unit sees only its own calls, and calls whose backup
            # ambulance slot is still open if it is a free ambulance (A01).
            import fleet
            me = fleet.get(who[1]) or {}
            free_ambulance = (me.get("type") == "Ambulance" and not me.get("incident_id")
                              and who[1] not in units_on_calls())
            dispatches = [d for d in dispatches
                          if who[1] in {d.get("assigned_unit"), *d["unit_assignments"].values()}
                          or (free_ambulance and "ambulance" in required_units(d)
                              and not d["unit_assignments"].get("ambulance"))]
        return {"dispatches": [public_incident(d) for d in dispatches]}


def _require_on_call(incident: dict, who: tuple[str, str]) -> None:
    """A unit acts only on a call it is on; a call with no unit has none (A01)."""
    if who[0] != "unit":
        return
    on_call = {incident.get("assigned_unit"), *_assignments(incident).values()} - {None, ""}
    if who[1] not in on_call:
        raise HTTPException(status_code=403, detail="Only a unit on this call can change its destination")


def _unit_or_dispatch(request) -> tuple[str, str]:
    try:
        return require_staff(request, "dispatcher")
    except HTTPException:
        return require_staff(request, "unit")


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


def _require_unit(request, driver_id: str) -> None:
    """A unit session may act only as itself, whatever the body says."""
    who = require_staff(request, "unit")
    if who[0] == "unit" and driver_id != who[1]:
        raise HTTPException(status_code=403, detail="This sign-in cannot act for another unit")


@router.post("/claim")
async def claim_dispatch(payload: ClaimPayload, request: Request = None):
    _require_unit(request, payload.driver_id)
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
    _require_unit(request, payload.driver_id)
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
        emergency = incident.get("emergency_type") or "general"
        current_id = incident.get("approved_hospital_id")
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
        # With the patient aboard, rank hospitals again from the scene (the
        # plan is minutes old). In the background, so the crew's tap is never
        # held up by a slow hospital lookup; attached when ready.
        task = asyncio.create_task(_attach_ranking(payload.incident_id, origin, emergency,
                                                   current_id, view.get("hospital_name"), destination))
        _RANKING_TASKS.add(task)
        task.add_done_callback(_RANKING_TASKS.discard)
        from routers.hospital import destination_on_divert
        return {"status": "patient_picked_up", "citizen_view": view, "unit_statuses": states,
                "unit_assignments": incident["unit_assignments"],
                "destination_on_divert": destination_on_divert(incident)}


RANKING_TIMEOUT_S = 15.0  # in the background: pickup itself never waits
_RANKING_TASKS: set = set()


def _is_current(entry: dict, current_id: str | None, current_name: str | None) -> bool:
    """Match the destination by curated id, else the raw id, else the name
    (hospitals found only on OpenStreetMap have no curated id)."""
    from routers.hospital import _resolve

    hid = str(entry.get("hospital_id") or "")
    if current_id:
        if hid == str(current_id):
            return True
        resolved = _resolve(hid)
        if resolved and resolved == _resolve(str(current_id)):
            return True
        if hid:
            return False  # both ids known and different: never match by name
    return bool(current_name) and entry.get("name") == current_name


def _summarise(ranking: dict) -> None:
    """Keep-or-switch line from the ranking and its current flags."""
    hospitals = ranking["hospitals"]
    current = next((h for h in hospitals if h["current"]), None)
    best = hospitals[0]
    ranking["current_still_first"] = bool(best["current"])
    if best["current"]:
        ranking["summary"] = "The current hospital is still the best choice from the scene."
    elif current is None:
        ranking["summary"] = (f"{best['name']} ranks first from the scene; the current hospital is not in the "
                              "top five on capability and modelled road time.")
    elif Number(best.get("road_eta_min")) is not None and Number(current.get("road_eta_min")) is not None:
        saves = current["road_eta_min"] - best["road_eta_min"]
        ranking["summary"] = (f"{best['name']} ranks first from the scene ({best['road_eta_min']} min by road); "
                              f"the current hospital is {current['road_eta_min']} min"
                              + (f", so switching saves {saves:.1f} min." if saves > 0 else
                                 ", but it ranks higher on capability."))
    else:
        ranking["summary"] = f"{best['name']} ranks first from the scene."


def Number(value):
    return value if isinstance(value, (int, float)) else None


async def _rank_from_scene(origin: dict, emergency: str, current_id: str | None,
                           current_name: str | None) -> dict | None:
    """Hospitals ranked from the scene now: score, modelled road time, reasoning.
    Hospitals on divert are left out."""
    import time as _time

    from agents.hospital_intelligence import hospital_agent
    from models.schemas import Coordinate, EmergencyType, HospitalRequest
    from routers.hospital import _resolve

    try:
        etype = EmergencyType(emergency)
    except ValueError:
        etype = EmergencyType.GENERAL
    result = await hospital_agent.rank_hospitals(HospitalRequest(
        incident_location=Coordinate(lat=origin["lat"], lng=origin["lng"]), emergency_type=etype))
    diverted = hospital_agent.diverted
    hospitals = []
    for entry in result.ranked_hospitals:
        data = entry.model_dump(mode="json")
        if (_resolve(str(data.get("hospital_id"))) or data.get("hospital_id")) in diverted:
            continue
        hospitals.append({
            "hospital_id": data.get("hospital_id"),
            "name": data.get("name"),
            "location": data.get("location"),
            "score": data.get("score"),
            "road_eta_min": round(data["road_eta_seconds"] / 60, 1) if data.get("road_eta_seconds") else None,
            "reasoning": (data.get("reasoning") or "")[:400],
        })
        if len(hospitals) == 5:
            break
    if not hospitals:
        return None
    for h in hospitals:
        h["current"] = _is_current(h, current_id, current_name)
    ranking = {"at": _time.time(), "emergency_type": etype.value, "hospitals": hospitals}
    _summarise(ranking)
    return ranking


async def _attach_ranking(incident_id: str, origin: dict, emergency: str, current_id: str | None,
                          current_name: str | None, destination: dict) -> None:
    try:
        ranking = await asyncio.wait_for(_rank_from_scene(origin, emergency, current_id, current_name),
                                         RANKING_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Re-ranking hospitals at pickup did not finish in time; the plan stands")
        return
    except Exception:
        logger.exception("Re-ranking hospitals at pickup failed; the plan stands")
        return
    if ranking is None:
        return
    async with _INCIDENTS_LOCK:
        incident = next((i for i in ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)
        # Attach only if the call is still in transport to the same hospital.
        if incident is None or incident.get("status") != "patient_picked_up":
            return
        if (incident.get("citizen_view") or {}).get("hospital_location") != destination:
            return
        if incident.get("approved_hospital_id") != current_id:
            return  # the destination changed while ranking (same place, other hospital)
        incident["pickup_ranking"] = ranking
        await commit(incident)


@router.post("/change_hospital")
async def change_hospital(payload: HospitalChangePayload, request: Request = None):
    who = require_staff(request, "unit")
    from agents.hospital_intelligence import hospital_agent
    from routers.hospital import _resolve

    async with _INCIDENTS_LOCK:
        incident = _incident(payload.incident_id)
        if incident.get("status") != "patient_picked_up":
            raise HTTPException(status_code=409, detail="A destination can be changed only during transport")
        _require_on_call(incident, who)
        candidates = ((incident.get("action_plan") or {}).get("all_hospitals") or []) + \
            ((incident.get("pickup_ranking") or {}).get("hospitals") or [])
        hospital = None
        if payload.hospital_id:
            # The fresh ranking first: it is what the unit was shown.
            hospital = next((h for h in reversed(candidates)
                             if str(h.get("hospital_id")) == payload.hospital_id), None)
        if hospital is None and not payload.hospital_id:
            hospital = next((h for h in candidates if payload.hospital_name in {h.get("name"), h.get("hospital_id")}), None)
        if not hospital or not hospital.get("location"):
            raise HTTPException(status_code=422, detail="Choose a hospital from the incident's reviewed destinations")
        canonical = _resolve(str(hospital.get("hospital_id", ""))) or _resolve(hospital["name"])
        if canonical in hospital_agent.diverted:
            raise HTTPException(status_code=409, detail="That hospital is on divert; select another destination")
        view = dict(incident.get("citizen_view") or {})
        origin = view.get("origin") or incident.get("location")
        old_destination = view.get("hospital_location")
        old_hospital_id = incident.get("approved_hospital_id")
    route = await _route(origin, hospital["location"])
    async with _INCIDENTS_LOCK:
        incident = _incident(payload.incident_id)
        _require_on_call(incident, who)  # again: a backup may have taken over while routing
        if (incident.get("status") != "patient_picked_up"
                or (incident.get("citizen_view") or {}).get("hospital_location") != old_destination
                or incident.get("approved_hospital_id") != old_hospital_id):
            raise HTTPException(status_code=409, detail="The incident changed while routing; refresh the call")
        if canonical in hospital_agent.diverted:
            raise HTTPException(status_code=409, detail="The hospital went on divert; select another destination")
        view = dict(incident.get("citizen_view") or {})
        view.update(route)
        view.update({"hospital_name": hospital["name"], "hospital_location": hospital["location"],
                     "message": f"Rerouted by driver to {hospital['name']}."})
        ranking = incident.get("pickup_ranking")
        if ranking:
            for h in ranking["hospitals"]:
                h["current"] = _is_current(h, canonical or hospital.get("hospital_id"), hospital["name"])
            _summarise(ranking)
        incident.update({"hospital_name": hospital["name"], "approved_hospital_id": canonical or hospital.get("hospital_id"),
                         "citizen_view": view})
        # A reroute the agent proposed for the old destination no longer
        # fits: close it, so dispatch cannot apply a route to hospital A
        # while the unit is heading to hospital B.
        stale = [a for a in (incident.get("monitoring") or {}).get("alerts", [])
                 if a.get("status") == "open" and a.get("leg") == "to_hospital"]
        for alert in stale:
            alert["status"] = "superseded"
        await commit(incident)  # on failure the record, alerts included, is restored
        from routers.hospital import destination_on_divert
        on_divert = destination_on_divert(incident)
    from agents import monitor

    for alert in stale:
        monitor.clear_alert(alert["unit_id"])
    return {"status": "rerouted", "citizen_view": view, "hospital_name": hospital["name"],
            "destination_on_divert": on_divert}
