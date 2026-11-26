"""A living city for the demo: other emergencies already under way.

On stage, a single scripted call on an empty map looks staged. "Busy city"
starts a few other calls across Bengaluru, part way through, and an optional
background caller adds one every couple of minutes. They are real calls,
made through the same handlers people use (report, approve, claim, pickup,
handover), so every screen shows them; the server acts for their caller,
dispatcher and crew through ``staff.system_actor`` (no outside request can).

They never get in the scripted call's way:
- they only use units outside STAGE_UNITS (the first unit of each type);
- the monitoring agent does not raise alerts on them (no open alert on a
  call nobody on stage is handling);
- each crew confirms pickup and handover by itself after a short stop.

Everything is labelled as simulated: the caller name, the dispatcher id and
a ``background`` flag the screens show.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import fleet
import staff

logger = logging.getLogger("geoagentic.city_life")

STAGE_UNITS = frozenset({"AMB-01", "AMB-02", "AMB-03", "FIRE-01", "POLICE-01", "RESCUE-01"})
CALLER_NAME = "City simulation"
DISPATCHER_ID = "CITY-SIM"
DWELL_S = 20.0               # a crew's stop at the scene or at the ER
MAX_ACTIVE = 5               # background calls at once
DEFAULT_INTERVAL_S = 150.0   # the background caller: one new call this often
ARRIVED_M = 40.0             # this close to the end of its leg: arrived

# (what the caller says, where, which service). Spread over the city and
# away from the scripted call's scene near Hebbal.
SCENARIOS = [
    ("My father slipped in the bathroom and hit his head, he is bleeding and confused",
     (12.9352, 77.6245), "Ambulance"),
    ("Two-wheeler accident, the rider is conscious but his leg looks broken",
     (12.9784, 77.6408), "Ambulance"),
    ("My mother has severe breathing difficulty and her lips are turning blue",
     (12.9250, 77.5938), "Ambulance"),
    ("My grandfather collapsed, his face is drooping and he cannot speak",
     (13.0035, 77.5710), "Ambulance"),
    ("A 58-year-old colleague has chest pain and is sweating a lot",
     (12.9698, 77.7300), "Ambulance"),
    ("My wife is pregnant and in labour, the pains are very close together",
     (12.9166, 77.6101), "Ambulance"),
    ("A diabetic neighbour is unconscious and not responding",
     (12.9255, 77.5468), "Ambulance"),
    ("A worker fell from the scaffolding at a construction site",
     (13.0285, 77.5540), "Ambulance"),
    ("There is thick smoke coming from a restaurant kitchen",
     (12.9910, 77.5550), "Fire Engine"),
    ("A car crash is blocking the junction and people are fighting",
     (12.9756, 77.6066), "Police Cruiser"),
    ("An old wall collapsed after the rain and someone is trapped",
     (12.9857, 77.6057), "Disaster Rescue"),
]

_state = {"background": False, "interval_s": DEFAULT_INTERVAL_S, "next_at": 0.0,
          "generation": 0, "created": 0}
_arrived_at: dict[str, float] = {}   # "incident_id:status" -> when its unit reached the end of that leg
_tasks: set = set()


def status() -> dict:
    from routers import incident as incident_router

    active = [i for i in incident_router.ACTIVE_INCIDENTS
              if i.get("background") and i.get("status") not in ("completed", "rejected", "failed")]
    return {"background_caller": _state["background"], "interval_s": _state["interval_s"],
            "active_calls": len(active), "created": _state["created"],
            "next_call_in_s": (max(0, round(_state["next_at"] - time.time()))
                               if _state["background"] else None)}


def configure(background: Optional[bool] = None, interval_s: Optional[float] = None) -> dict:
    if interval_s is not None:
        _state["interval_s"] = max(30.0, min(1800.0, float(interval_s)))
    if background is not None:
        _state["background"] = bool(background)
        _state["next_at"] = time.time() + (20.0 if background else 0.0)
    return status()


def reset() -> None:
    """The demo reset: no background caller, nothing in flight."""
    _state.update(background=False, next_at=0.0, created=0)
    _state["generation"] += 1
    _arrived_at.clear()
    for task in list(_tasks):
        task.cancel()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def seed(count: int) -> int:
    """Start `count` calls at once, each part way through its first leg, so
    the map is busy from the first second."""
    picks = random.sample(SCENARIOS, k=min(count, len(SCENARIOS)))
    for n, scenario in enumerate(picks):
        _spawn(create_call(scenario, head_start=(0.15, 0.45, 0.7, 0.9, 0.3, 0.6)[n % 6]))
    return len(picks)


async def _find(incident_id: str) -> Optional[dict]:
    from routers import incident as incident_router

    return next((i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id), None)


async def create_call(scenario, head_start: float = 0.0) -> Optional[str]:
    """Report, approve and send one call, as its caller, dispatcher and crew."""
    from fastapi import HTTPException, Response

    from routers import incident as incident_router
    from routers.driver import ClaimPayload, claim_dispatch

    generation = _state["generation"]
    text, (lat, lng), vehicle = scenario
    payload = incident_router.IncidentReportPayload(
        citizen_text=text, citizen_name=CALLER_NAME, anonymous=True,
        vehicle_required=vehicle, location={"lat": lat, "lng": lng})
    try:
        with staff.system_actor("city"):
            created = await incident_router.report_incident(payload, Response(), request=None)
        incident_id = created["incident_id"]
        inc = await _find(incident_id)
        if inc is not None:
            inc["background"] = True  # before its plan is approved and a unit chosen
        # The pipeline (triage, ranking, routing) runs in the background.
        for _ in range(240):
            await asyncio.sleep(0.5)
            inc = await _find(incident_id)
            if generation != _state["generation"] or inc is None:
                return None
            if inc.get("status") != "processing":
                break
        if inc is None or inc.get("status") != "awaiting_dispatcher_approval":
            logger.info("Background call %s not ready: %s", incident_id, inc and inc.get("status"))
            return None
        plan = inc.get("action_plan") or {}
        hospital = plan.get("recommended_hospital") or {}
        approve = incident_router.ApprovePayload(
            dispatcher_id=DISPATCHER_ID, approved_route="recommended",
            approved_hospital=hospital.get("hospital_id") or hospital.get("name") or None)
        with staff.system_actor("city"):
            try:
                await incident_router.approve_incident(incident_id, approve, request=None)
            except HTTPException as refused:
                # No free background unit (or every hospital on divert): the
                # call is withdrawn, not left in the stage dispatcher's queue.
                logger.info("Background call %s not dispatched: %s", incident_id, refused.detail)
                await incident_router.reject_incident(incident_id, dispatcher_id=DISPATCHER_ID, request=None)
                return None
            inc = await _find(incident_id)
            unit_id = inc and inc.get("assigned_unit")
            if not unit_id:
                return None
            await claim_dispatch(ClaimPayload(incident_id=incident_id, driver_id=unit_id), request=None)
        if head_start > 0:
            from agents import simulator

            inc = await _find(incident_id)
            if inc is not None:
                simulator.start_part_way(unit_id, inc, head_start)
        _state["created"] += 1
        logger.info("Background call %s: %s, %s", incident_id, vehicle, unit_id)
        return incident_id
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Background call failed")
        return None


# A leg's end point changes only when the call's status does, so it is looked up
# once per (call, status) instead of once per tick.
_leg_end_cache: dict[str, Optional[dict]] = {}


def _leg_end(inc: dict) -> Optional[dict]:
    from agents.simulator import fleet_leg

    # The hospital is part of the key: a reroute keeps the status but changes the leg.
    key = f"{inc['incident_id']}:{inc.get('status')}:{inc.get('approved_hospital_id') or ''}"
    if key not in _leg_end_cache:
        _key, route, _eta = fleet_leg(inc)
        _leg_end_cache[key] = route[-1] if route else None
    return _leg_end_cache[key]


async def advance() -> None:
    """Each tick: crews of background calls confirm arrival after a short
    stop, and the background caller adds a call when it is due."""
    from fastapi import HTTPException

    from agents.monitor import _dist
    from routers import incident as incident_router
    from routers.driver import ClaimPayload, arrived_pickup

    now = time.time()
    calls = [i for i in incident_router.ACTIVE_INCIDENTS
             if i.get("background") and i.get("status") in ("dispatched", "patient_picked_up")]
    live_ids = {i["incident_id"] for i in calls}
    live = {f"{i['incident_id']}:{i.get('status')}" for i in calls}
    for gone in [k for k in _leg_end_cache if k.split(":", 1)[0] not in live_ids]:
        _leg_end_cache.pop(gone, None)
    for gone in [k for k in _arrived_at if k not in live]:
        _arrived_at.pop(gone, None)
    for inc in calls:
        unit = fleet.unit_for_incident(inc["incident_id"])
        end = _leg_end(inc)
        key = f"{inc['incident_id']}:{inc.get('status')}"
        if unit is None or end is None or _dist(unit["position"], end) > ARRIVED_M:
            # Left the stop (or never reached it): the dwell starts again on arrival.
            _arrived_at.pop(key, None)
            continue
        first = _arrived_at.setdefault(key, now)
        if now - first < DWELL_S:
            continue
        _arrived_at.pop(key, None)
        medical = (inc.get("vehicle_required") or "Ambulance") in ("Ambulance", "Medical")
        try:
            with staff.system_actor("city"):
                if inc.get("status") == "dispatched" and medical:
                    await arrived_pickup(ClaimPayload(incident_id=inc["incident_id"], driver_id=unit["unit_id"]),
                                         request=None)
                else:
                    await incident_router.incident_arrived(inc["incident_id"], request=None)
        except HTTPException as refused:
            logger.info("Background call %s could not advance: %s", inc["incident_id"], refused.detail)
        except Exception:
            logger.exception("Background call %s could not advance", inc["incident_id"])

    if _state["background"] and now >= _state["next_at"]:
        _state["next_at"] = now + _state["interval_s"]
        if len(calls) < MAX_ACTIVE:
            _spawn(create_call(random.choice(SCENARIOS)))
