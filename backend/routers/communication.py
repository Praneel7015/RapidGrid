"""
Communication Agent API router.

Endpoints:
  POST /api/communication/dispatch — Manually trigger dispatch of an action plan
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agents.communication import communication_agent
from models.schemas import CommunicationRequest, CommunicationResponse

router = APIRouter(prefix="/api/communication", tags=["Communication Agent"])


@router.post(
    "/dispatch",
    response_model=CommunicationResponse,
    summary="Dispatch notifications based on an action plan",
)
async def dispatch_plan(request: CommunicationRequest) -> CommunicationResponse:
    """
    Manually dispatch notifications for a given action plan.
    (Note: This normally happens automatically via the event bus when
    Decision Fusion completes).
    """
    try:
        return await communication_agent.dispatch_plan(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
