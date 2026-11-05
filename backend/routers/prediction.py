"""
Prediction Agent API router.

Endpoints:
  POST /api/prediction/track   — track a route for live ETA updates
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agents.prediction import prediction_agent
from models.schemas import PredictionRequest, PredictionResponse

router = APIRouter(prefix="/api/prediction", tags=["Prediction Agent"])


@router.post(
    "/track",
    response_model=PredictionResponse,
    summary="Track a route to receive live ETA and delay probability updates",
)
async def track_route(request: PredictionRequest) -> PredictionResponse:
    """
    Registers a route with the Prediction Agent.

    The Prediction Agent will listen for Traffic Updates and continuously
    recalculate the ETA and delay probability for this route, publishing
    updates to the PREDICTION_UPDATE bus topic.
    """
    try:
        return await prediction_agent.predict_on_demand(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
