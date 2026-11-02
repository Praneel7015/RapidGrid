"""
Learning Agent API router.

Endpoints:
  POST /api/learning/close_case — Trigger a mock case close to test the stub
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bus.event_bus import CASE_CLOSED, bus

router = APIRouter(prefix="/api/learning", tags=["Learning Agent"])


@router.post(
    "/close_case",
    summary="Mock closing a case to trigger the Learning Agent",
)
async def close_case(payload: dict) -> dict:
    """
    Manually trigger a CASE_CLOSED event to test the Learning Agent stub.
    """
    try:
        await bus.publish(
            topic=CASE_CLOSED,
            payload=payload,
            source_agent="api_router",
        )
        return {"status": "event_published"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/dispatch_decision",
    summary="Handle dispatcher approve/reject decisions for an action plan",
)
async def dispatch_decision(payload: dict) -> dict:
    """
    Called by the Frontend Dashboard when the dispatcher clicks Approve or Reject.
    We immediately simulate closing the case (triggering the Learning Agent STUB)
    with the dispatcher_override logic.
    """
    try:
        # We forward this to the CASE_CLOSED topic so the Learning Agent STUB logs it.
        # Payload shape: { incident_id, decision: "approved" | "rejected", reason: "...", ... }
        await bus.publish(
            topic=CASE_CLOSED,
            payload=payload,
            source_agent="dispatcher_ui",
        )
        return {"status": "decision_logged"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
