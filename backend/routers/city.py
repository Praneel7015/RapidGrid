"""
City state router - the intervention surface for the causal model.

These endpoints are `do()` operators. Closing a road here does not annotate a
map; it changes the cost of the network, so every route computed afterwards is
genuinely different, including routes for incidents that had already been
planned and had nothing to do with the closure.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from staff import require as require_staff
from pydantic import BaseModel, Field

from agents.city_state import city_state
from agents.local_router import local_router

logger = logging.getLogger("geoagentic.router.city")

router = APIRouter(prefix="/api/city", tags=["City State"])


class ClosureRequest(BaseModel):
    road: str | None = Field(default=None, max_length=120)
    edge_ids: list[int] | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=120)


def _checked_edges(edge_ids: list[int]) -> list[int]:
    """Reject ids outside the loaded graph before they reach the city model."""
    total = len(local_router.edge_to)
    bad = [e for e in edge_ids if not 0 <= e < total]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown road segment id: {bad[0]}")
    return edge_ids


class ConditionsRequest(BaseModel):
    hour: int | None = None
    weather: str | None = None


@router.get("/state")
async def get_state():
    """Current causal state: exogenous inputs, active interventions, provenance."""
    if not city_state.ready:
        city_state.attach(local_router)
    return city_state.snapshot()


@router.get("/overlay")
async def get_overlay(limit: int = 400):
    """Drawable geometry for the map: closures and displaced congestion."""
    if not city_state.ready:
        city_state.attach(local_router)
    return {
        "closures": city_state.closure_overlay(),
        "congestion": city_state.congestion_overlay(limit=limit),
        "provenance": "modelled",
    }


@router.get("/roads")
async def search_roads(q: str, limit: int = 12):
    """Autocomplete for road names, so a dispatcher can close one by name."""
    if not city_state.ready:
        city_state.attach(local_router)
    if not q or len(q) < 3:
        return {"roads": []}

    needle = q.lower().strip()
    seen: dict[str, int] = {}
    for name_idx, name in enumerate(local_router.road_names):
        if needle in name.lower():
            seen[name] = seen.get(name, 0) + 1
            if len(seen) >= limit * 3:
                break
    ranked = sorted(seen.items(), key=lambda kv: -kv[1])[:limit]
    return {"roads": [{"name": n, "segments": c} for n, c in ranked]}


@router.post("/close")
async def close_road(payload: ClosureRequest, request: Request = None):
    """
    do(close road).

    Removes the segments from routing and displaces their traffic onto the
    surrounding network, which is what makes unrelated routes slow down too.
    """
    require_staff(request, "dispatcher")
    if not city_state.ready:
        city_state.attach(local_router)

    edge_ids = payload.edge_ids
    label = payload.label or payload.road or "Road closure"
    if not edge_ids and payload.road:
        edge_ids = city_state.find_edges_on_road(payload.road)
    if not edge_ids:
        raise HTTPException(
            status_code=404,
            detail=f"No road segments matched '{payload.road}'. Try a fuller name.",
        )

    result = city_state.close_road(_checked_edges(edge_ids), label=label)
    return {**result, "label": label, "state": city_state.snapshot()}


@router.post("/incident")
async def report_incident(payload: ClosureRequest, request: Request = None):
    """do(incident here) - partial obstruction rather than a full closure."""
    require_staff(request, "dispatcher")
    if not city_state.ready:
        city_state.attach(local_router)
    edge_ids = payload.edge_ids
    if not edge_ids and payload.road:
        edge_ids = city_state.find_edges_on_road(payload.road, limit=40)
    if not edge_ids:
        raise HTTPException(status_code=404, detail="No matching road segments.")
    result = city_state.report_incident(_checked_edges(edge_ids), label=payload.label or "Incident")
    return {**result, "state": city_state.snapshot()}


@router.post("/conditions")
async def set_conditions(payload: ConditionsRequest, request: Request = None):
    """Set the exogenous variables - time of day and weather."""
    require_staff(request, "dispatcher")
    if not city_state.ready:
        city_state.attach(local_router)
    if payload.hour is not None:
        city_state.set_hour(payload.hour)
    if payload.weather:
        city_state.set_weather(payload.weather)
    return city_state.snapshot()


@router.post("/clear")
async def clear_state(request: Request = None):
    """Revert every intervention and return the city to baseline."""
    require_staff(request, "dispatcher")
    if not city_state.ready:
        city_state.attach(local_router)
    city_state.clear()
    return city_state.snapshot()


@router.get("/explain/{edge_id}")
async def explain_segment(edge_id: int):
    """Which mechanisms are slowing one segment, and by how much."""
    if not city_state.ready:
        city_state.attach(local_router)
    return city_state.explain(edge_id)
