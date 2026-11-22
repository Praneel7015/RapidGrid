"""
Decision Fusion Engine API router.

Endpoints:
  POST /api/fusion/combine  — run the full pipeline and produce an action plan
  GET  /api/fusion/weights   — view the current fusion weight tables
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from agents.decision_fusion import FUSION_WEIGHTS, fusion_engine
from models.schemas import FusionRequest, FusionResponse

from staff import require as require_staff

router = APIRouter(prefix="/api/fusion", tags=["Decision Fusion Engine"])


@router.post(
    "/combine",
    response_model=FusionResponse,
    summary="Fuse all agents' outputs into one explainable action plan",
)
async def combine(request: FusionRequest, http: Request = None) -> FusionResponse:
    """
    Run the full GeoAgentic pipeline for an incident:

    1. Traffic Intelligence analyses road conditions.
    2. Hospital Intelligence ranks hospitals for the emergency type.
    3. Route Optimization computes the route to the best hospital.
    4. Decision Fusion Engine combines everything into one action plan
       with a `factor_breakdown` and `explanation`.

    The response includes the recommended route, recommended hospital,
    per-dimension scoring breakdown, and a plain-language explanation
    suitable for dispatcher review (workflow step 8).
    """
    require_staff(http, "dispatcher")
    try:
        return await fusion_engine.fuse(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get(
    "/weights",
    summary="View the fusion weight tables for each emergency type",
)
async def get_weights():
    """
    Return the current fusion weight tables.

    Weights are tunable per emergency type and determine how much
    each dimension (response time, traffic, hospital, severity,
    accessibility) contributes to the final decision.
    """
    return {
        "description": (
            "Fusion-level weights per emergency type. "
            "These combine agent outputs into the final action plan. "
            "Distinct from hospital-scoring weights (internal to Hospital Intelligence)."
        ),
        "weight_tables": FUSION_WEIGHTS,
    }
