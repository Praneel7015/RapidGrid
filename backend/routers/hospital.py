"""
Hospital Intelligence API router.

Endpoints:
  POST /api/hospital/rank            — rank hospitals for an emergency
  GET  /api/hospital/list            — list all known hospitals
  GET  /api/hospital/{hospital_id}   — get details for one hospital
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agents.hospital_intelligence import hospital_agent
from pydantic import BaseModel

from models.schemas import HospitalRankingResponse, HospitalRequest


class HospitalResourceUpdate(BaseModel):
    """ER terminal toggling live resource availability."""
    icu_available: bool
    beds_available: int | None = None
    note: str | None = None
from routers.incident import ACTIVE_INCIDENTS

logger = logging.getLogger("geoagentic.router.hospital")

router = APIRouter(prefix="/api/hospital", tags=["Hospital Intelligence"])


@router.post(
    "/rank",
    response_model=HospitalRankingResponse,
    summary="Rank hospitals for an emergency by weighted resource availability",
)
async def rank_hospitals(request: HospitalRequest) -> HospitalRankingResponse:
    """
    Run the Hospital Intelligence Agent for the given emergency.

    Scores hospitals using the weight table for the specified
    emergency_type (general / cardiac / trauma / stroke).  Returns the
    full ranked list with per-factor score breakdowns and plain-language
    reasoning for each hospital.

    Never ranks by distance alone — this is the agent's core
    differentiator.
    """
    result = await hospital_agent.publish_ranking(request)
    return result


@router.get(
    "/list",
    summary="List all known hospitals with their capabilities",
)
async def list_hospitals():
    """Return the full list of hospitals in the system (unranked)."""
    hospitals = []
    for h in hospital_agent._hospitals:
        hospitals.append({
            "hospital_id": h["id"],
            "name": h["name"],
            "location": {"lat": h["lat"], "lng": h["lng"]},
            "icu_available": h["icu_available"],
            "beds_total": h["beds_total"],
            "beds_available": h["beds_available"],
            "specialists": h["specialists"],
            "capabilities": h["capabilities"],
        })
    return {"hospitals": hospitals, "count": len(hospitals)}


@router.get(
    "/incoming/all",
    summary="Return all active incoming emergency cases for the Hospital ER Desk",
)
async def get_all_incoming_cases():
    """Return all active incoming emergency cases for the Hospital ER Desk."""
    incoming = []
    for inc in ACTIVE_INCIDENTS:
        if inc.get("status") in ("dispatched", "awaiting_dispatcher_approval", "processing", "patient_picked_up"):
            cit_view = inc.get("citizen_view") or {}
            action_plan = inc.get("action_plan") or {}
            rec_hosp = action_plan.get("recommended_hospital") or {}
            hosp_name = cit_view.get("hospital_name") or rec_hosp.get("name") or "Nearest Hospital"

            incoming.append({
                "incident_id": inc["incident_id"],
                "status": inc.get("status"),
                "emergency_type": inc.get("emergency_type") or cit_view.get("emergency_type") or "General Emergency",
                "severity": inc.get("severity") or cit_view.get("severity") or 0.7,
                "hospital_name": hosp_name,
                "eta_minutes": cit_view.get("eta_minutes") or 7,
                "patient_summary": inc.get("details") or inc.get("citizen_text") or "Emergency reported by citizen.",
                "assigned_driver": inc.get("assigned_driver", "AMB-IND-04"),
                # Phone numbers needed by HospitalDashboard LiveEmergencyChat contacts prop
                "citizen_phone": inc.get("citizen_phone"),
                "unit_phone": inc.get("unit_phone") or cit_view.get("unit_phone"),
            })
    return {"incoming": incoming}


@router.get("/{hospital_id}/incoming")
async def get_incoming_cases(hospital_id: str, name: str = None):
    """Hospital dashboard polls for incoming cases assigned specifically to this hospital."""
    incoming = []
    for inc in ACTIVE_INCIDENTS:
        # Include patient_picked_up — that's when the hospital most needs to prepare
        if inc.get("status") in ("dispatched", "awaiting_dispatcher_approval", "processing", "patient_picked_up", "completed"):
            cit_view = inc.get("citizen_view") or {}
            action_plan = inc.get("action_plan") or {}
            rec_hosp = action_plan.get("recommended_hospital") or {}

            hosp_name = cit_view.get("hospital_name")
            if not hosp_name and isinstance(rec_hosp, dict):
                hosp_name = rec_hosp.get("name")
            elif not hosp_name and hasattr(rec_hosp, "name"):
                hosp_name = rec_hosp.name
            hosp_name = hosp_name or "Assigned Hospital"

            rec_id = ""
            if isinstance(rec_hosp, dict):
                rec_id = rec_hosp.get("hospital_id", "")
            elif hasattr(rec_hosp, "hospital_id"):
                rec_id = getattr(rec_hosp, "hospital_id", "")

            target_terms = [hospital_id.lower()]
            if name:
                target_terms.append(name.lower())

            # Match by all, hospital_id, hospital_name, or name tokens
            matches = (
                hospital_id == 'all' or
                rec_id.lower() == hospital_id.lower() or
                any(term in hosp_name.lower() for term in target_terms if term) or
                any(token in hosp_name.lower() for term in target_terms for token in term.split() if len(token) > 3)
            )

            # Fire and Police incidents without hospital transport do not clutter Hospital ER Desk
            veh_req = inc.get("vehicle_required") or "Ambulance"
            if matches and veh_req in ("Ambulance", "Medical"):
                incoming.append({
                    "incident_id": inc["incident_id"],
                    "status": inc.get("status"),
                    "emergency_type": inc.get("emergency_type") or cit_view.get("emergency_type") or "General Emergency",
                    "severity": inc.get("severity") or cit_view.get("severity") or 0.7,
                    "hospital_name": hosp_name,
                    "eta_minutes": cit_view.get("eta_minutes") or 7,
                    "patient_summary": inc.get("details") or inc.get("citizen_text") or "Emergency reported by citizen.",
                    "assigned_driver": inc.get("assigned_driver", "AMB-UNIT-04"),
                    # Phone numbers needed by HospitalDashboard LiveEmergencyChat contacts prop
                    "citizen_phone": inc.get("citizen_phone"),
                    "unit_phone": inc.get("unit_phone") or cit_view.get("unit_phone"),
                })
    return {"incoming": incoming}


@router.get(
    "/{hospital_id}",
    summary="Get details for a specific hospital",
)
async def get_hospital(hospital_id: str):
    """
    An ER desk setting its own capacity.

    Putting a facility on divert removes it from the routing engine's candidate
    list for new emergency calls - it is a real control, not an annotation.
    Accepts a curated id (blr-005), an OSM id, or the hospital name, because
    different parts of the app address hospitals differently.
    """
    key = hospital_agent.set_divert(hospital_id, on_divert=not update.icu_available)
    if key is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No hospital matched '{hospital_id}'. Use a curated id "
                f"(e.g. blr-005), an OSM id, or the full facility name."
            ),
        )

    profile = hospital_agent._load_capabilities()[key]
    logger.info(
        "ER capacity update: %s -> %s",
        profile["name"], "accepting" if update.icu_available else "on divert",
    )
    return {
        "status": "updated",
        "hospital_id": key,
        "name": profile["name"],
        "accepting": update.icu_available,
        "on_divert": not update.icu_available,
        "effect": (
            "Excluded from routing for new emergency calls."
            if not update.icu_available
            else "Available for routing."
        ),
        "diverted_facilities": sorted(hospital_agent.diverted),
    }
