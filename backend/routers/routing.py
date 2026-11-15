"""
Route Optimization API router.

Endpoints:
  POST /api/route/compute       — compute optimal route + alternates
  GET  /api/route/{route_id}    — retrieve a previously computed route
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from agents.route_optimization import route_agent
from models.schemas import RouteRequest, RouteResponse

router = APIRouter(prefix="/api/route", tags=["Route Optimization"])

# In-memory cache of computed routes (keyed by route_id)
_route_cache: dict[str, RouteResponse] = {}


@router.post(
    "/compute",
    response_model=RouteResponse,
    summary="Compute the optimal emergency route with alternates",
)
async def compute_route(request: RouteRequest) -> RouteResponse:
    """
    Run the Route Optimization Agent for the given origin → destination.

    Returns the primary recommended route plus at least one distinct
    alternate (if one exists).  Each route includes coordinates, ETA,
    distance, delay probability, and a plain-language selection reason.

    The request accepts node IDs from the loaded road network
    (e.g. "N1" for Incident Site, "N5" for Hospital Gate).
    """
    try:
        route = await asyncio.to_thread(route_agent.compute_route, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Cache the primary and its alternates for later retrieval
    _route_cache[route.route_id] = route
    for alt in route.alternate_routes:
        _route_cache[alt.route_id] = alt

    return route


@router.get(
    "/{route_id}",
    response_model=RouteResponse,
    summary="Retrieve a previously computed route by ID",
)
async def get_route(route_id: str) -> RouteResponse:
    """
    Look up a route that was previously computed.

    In production this would query a persistent store; here it uses an
    in-memory cache that lives as long as the server process.
    """
    route = _route_cache.get(route_id)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=f"Route '{route_id}' not found. It may have expired or was never computed.",
        )
    return route
