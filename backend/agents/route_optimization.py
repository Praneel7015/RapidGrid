"""
Route Optimization Agent for GeoAgentic (Live Version).

Contract (from agent-contracts.md):
  Input:  {origin, destination, vehicle_type, blocked_segments[],
           traffic_snapshot}
  Output: {route_id, coordinates[], distance, eta, delay_probability,
           alternate_routes[], selection_reason, confidence}

Implementation:
  - Calls Google Routes API (v2) for live traffic-aware routing.
  - Decodes GeoJSON linestring into Coordinates.
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from threading import RLock
import httpx
from collections import OrderedDict
from typing import Any

from agents.local_router import local_router
from bus.event_bus import ROUTE_COMPUTED, ROUTE_REQUEST, bus
from models.schemas import (
    Coordinate,
    DataFreshness,
    Event,
    RouteRequest,
    RouteResponse,
)

logger = logging.getLogger("geoagentic.agent.route")

AGENT_NAME = "route_optimization"

# --- Billing guard -----------------------------------------------------------
# Google Routes is billed per request. During a demo the same origin/destination
# pair gets recomputed on every poll, which burns quota for identical answers.
# Cache on coordinates rounded to ~11m, with a short TTL so live traffic still
# refreshes. _CACHE_PRECISION=4 decimal places ≈ 11 metres.
_CACHE_TTL_S = 90.0
_CACHE_MAX_ENTRIES = 256
_CACHE_PRECISION = 4
_PROVIDER_FRESHNESS_S = 300.0


GOOGLE_TIMEOUT_S = 4.0


def _active_interventions() -> int:
    """Closed plus obstructed segments in the city model (0 when it is not loaded)."""
    try:
        from agents.city_state import city_state
    except Exception:
        return 0
    if not city_state.ready:
        return 0
    return len(city_state.closed_edges) + len(city_state.incident_edges)


class RouteOptimizationAgent:
    """Live routing via Google Routes API."""

    def __init__(self) -> None:
        self._loaded = True
        from dotenv import load_dotenv
        load_dotenv()
        self.api_key = os.getenv("GOOGLE_ROUTES_API_KEY")
        self._cache: OrderedDict[tuple, tuple[float, RouteResponse]] = OrderedDict()
        self.api_calls = 0
        self.cache_hits = 0
        self.local_routes = 0
        self._state_lock = RLock()
        self._last_live_success_at: str | None = None
        self._last_live_error_at: str | None = None
        self._last_live_success_monotonic: float | None = None
        self._last_live_error_monotonic: float | None = None
        self._last_route_source = "unverified"
        self._fallback_reason: str | None = None
        self._provider_errors = 0

    def _cache_key(self, request: RouteRequest) -> tuple:
        return (
            round(request.origin.lat, _CACHE_PRECISION),
            round(request.origin.lng, _CACHE_PRECISION),
            round(request.destination.lat, _CACHE_PRECISION),
            round(request.destination.lng, _CACHE_PRECISION),
        )

    def _cache_get(self, key: tuple) -> RouteResponse | None:
        with self._state_lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            cached_at, response = hit
            if time.monotonic() - cached_at > _CACHE_TTL_S:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            self.cache_hits += 1
            self._last_route_source = "live_cache"
            return response.model_copy(deep=True)

    def _cache_put(self, key: tuple, response: RouteResponse) -> None:
        with self._state_lock:
            self._cache[key] = (time.monotonic(), response.model_copy(deep=True))
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)

    def _provider_success(self) -> None:
        with self._state_lock:
            self._last_live_success_at = datetime.now(timezone.utc).isoformat()
            self._last_live_success_monotonic = time.monotonic()
            self._last_route_source = "live"
            self._fallback_reason = None

    def _provider_failure(self, reason: str) -> None:
        with self._state_lock:
            self._last_live_error_at = datetime.now(timezone.utc).isoformat()
            self._last_live_error_monotonic = time.monotonic()
            self._fallback_reason = reason
            self._provider_errors += 1
            self._last_route_source = "unverified"

    def stats(self) -> dict[str, Any]:
        """Recent observed provider status; reading health never spends quota."""
        with self._state_lock:
            success = self._last_live_success_monotonic
            error = self._last_live_error_monotonic
            if not self.api_key:
                provider_status = "not_configured"
            elif error is not None and (success is None or error >= success):
                provider_status = "unavailable"
            elif success is None:
                provider_status = "unverified"
            elif time.monotonic() - success > _PROVIDER_FRESHNESS_S:
                provider_status = "stale"
            else:
                provider_status = "available"
            source = self._last_route_source
            if provider_status in {"stale", "not_configured"} and source in {"live", "live_cache"}:
                source = "unverified"
            total = self.api_calls + self.cache_hits
            return {
                "billable_api_calls": self.api_calls,
                "cache_hits": self.cache_hits,
                "cached_routes": len(self._cache),
                "hit_rate": round(self.cache_hits / total, 3) if total else 0.0,
                "cache_ttl_seconds": _CACHE_TTL_S,
                "offline_graph_routes": self.local_routes,
                "offline_graph_available": local_router.available,
                "live_api_status": provider_status,
                "active_source": source,
                "last_live_success_at": self._last_live_success_at,
                "last_live_error_at": self._last_live_error_at,
                "provider_errors": self._provider_errors,
                "fallback_reason": self._fallback_reason,
                "provider_freshness_seconds": _PROVIDER_FRESHNESS_S,
            }

    def load_network(
        self,
        nodes: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> None:
        """No-op for live API."""
        pass

    def register_on_bus(self) -> None:
        """No-op for live API. We no longer listen to local traffic events."""
        pass

    def compute_route(self, request: RouteRequest) -> RouteResponse:
        """
        Compute the optimal route using Google Routes API.
        """
        if not self.api_key:
            logger.warning(
                "GOOGLE_ROUTES_API_KEY not set - using the offline graph router"
            )
            return self._local_route(request, reason="no API key configured")

        # Google knows real traffic but not the closures and incidents drawn
        # in our city model. While any are active, route on the graph that
        # does, so the dispatched route and the hospital ranking agree.
        interventions = _active_interventions()
        if interventions:
            return self._local_route(
                request,
                reason=f"{interventions} closed or obstructed segment(s) in the city model; "
                "Google Routes does not know about them",
            )

        cache_key = self._cache_key(request)
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.info(
                "Route cache HIT (%d billable calls saved so far)", self.cache_hits
            )
            return cached

        url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.geoJsonLinestring"
        }
        
        payload = {
            "origin": {
                "location": {
                    "latLng": {
                        "latitude": request.origin.lat,
                        "longitude": request.origin.lng
                    }
                }
            },
            "destination": {
                "location": {
                    "latLng": {
                        "latitude": request.destination.lat,
                        "longitude": request.destination.lng
                    }
                }
            },
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
            "polylineEncoding": "GEO_JSON_LINESTRING"
        }

        try:
            with self._state_lock:
                self.api_calls += 1
            # Short timeout: a hanging provider must not hold up a report;
            # the offline router answers in well under a second.
            with httpx.Client(timeout=httpx.Timeout(GOOGLE_TIMEOUT_S, connect=2.0)) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error("Google Routes HTTP %s - falling back to offline router", status)
            self._provider_failure(f"Google Routes HTTP {status}")
            return self._local_route(request, reason=f"Google Routes HTTP {status}")
        except Exception as e:
            logger.error("Google Routes unreachable (%s) - falling back", type(e).__name__)
            self._provider_failure(f"Google Routes unreachable ({type(e).__name__})")
            return self._local_route(request, reason=f"Google Routes unreachable ({type(e).__name__})")

        routes = data.get("routes", []) if isinstance(data, dict) else []
        if not routes:
            self._provider_failure("Google Routes returned no path")
            return self._local_route(request, reason="Google Routes returned no path")
            
        try:
            route_data = routes[0]
            duration_str = route_data.get("duration", "0s").removesuffix("s")
            eta = float(duration_str)
            distance = float(route_data.get("distanceMeters", 0))
            geojson = route_data.get("polyline", {}).get("geoJsonLinestring", {}).get("coordinates", [])
            coords = [Coordinate(lat=c[1], lng=c[0]) for c in geojson]
            if len(coords) < 2 or not math.isfinite(eta) or not math.isfinite(distance) or eta <= 0 or distance <= 0:
                raise ValueError("Invalid route geometry or metrics")
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            self._provider_failure("Google Routes returned invalid route data")
            return self._local_route(request, reason="Google Routes returned invalid route data")
        
        reason = (
            f"Primary route via Google Maps Live Traffic. "
            f"Estimated travel time: {eta:.0f}s, distance: {distance:.0f}m."
        )

        response = RouteResponse(
            route_id=f"R-{uuid.uuid4().hex[:8]}",
            coordinates=coords,
            distance=round(distance, 1),
            eta=round(eta, 1),
            delay_probability=0.1,
            alternate_routes=[],
            selection_reason=reason,
            data_freshness=DataFreshness.LIVE,
            confidence=0.95,
            node_ids=[],
            edge_ids=[],
        )
        self._provider_success()
        self._cache_put(cache_key, response)
        return response

    def _local_route(self, request: RouteRequest, reason: str) -> RouteResponse:
        """
        Tier 2: A* over the cached OpenStreetMap graph.

        A real path over a real road network. Marked CACHED rather than LIVE
        because the speeds are modelled from road class plus whatever traffic
        the Traffic Intelligence Agent has overlaid - not observed live.
        """
        with self._state_lock:
            self._fallback_reason = reason
        if not local_router.available and not local_router.load():
            return self._fallback_route(request)

        path = local_router.find_path(
            (request.origin.lat, request.origin.lng),
            (request.destination.lat, request.destination.lng),
        )
        if not path:
            return self._fallback_route(request)

        with self._state_lock:
            self.local_routes += 1
            self._last_route_source = "offline_graph"
            self._fallback_reason = reason

        roads = path["roads"]
        via = ", ".join(roads[:3]) + ("..." if len(roads) > 3 else "")
        detail = ""
        if local_router.blocked_count:
            detail = f" Avoiding {local_router.blocked_count} blocked segment(s)."
        selection_reason = (
            f"Offline graph route ({reason}). A* shortest-time path over "
            f"{len(path['edge_ids'])} OpenStreetMap road segments via {via}."
            f"{detail} Travel time {path['travel_time_s']:.0f}s, "
            f"distance {path['distance_m']:.0f}m."
        )

        return RouteResponse(
            route_id=f"R-{uuid.uuid4().hex[:8]}",
            coordinates=[
                Coordinate(lat=c["lat"], lng=c["lng"]) for c in path["coordinates"]
            ],
            distance=path["distance_m"],
            eta=path["travel_time_s"],
            delay_probability=0.25,
            alternate_routes=[],
            selection_reason=selection_reason,
            data_freshness=DataFreshness.CACHED,
            # Lower than Google's 0.95: a genuine path on modelled speeds.
            confidence=0.75,
            node_ids=[str(n) for n in path["node_ids"]],
            edge_ids=[str(e) for e in path["edge_ids"]],
        )

    def _fallback_route(self, request: RouteRequest) -> RouteResponse:
        """Tier 3, last resort: no usable route at all. Never claim otherwise."""
        with self._state_lock:
            self._last_route_source = "unavailable"
            self._fallback_reason = self._fallback_reason or "No usable road route"
        return RouteResponse(
            route_id=f"R-{uuid.uuid4().hex[:8]}",
            coordinates=[request.origin, request.destination],
            distance=0.0,
            eta=0.0,
            delay_probability=1.0,
            alternate_routes=[],
            selection_reason=(
                "NO ROUTE AVAILABLE - both the live routing API and the offline "
                "road graph failed. Straight-line placeholder only; do not "
                "dispatch on this geometry."
            ),
            data_freshness=DataFreshness.STALE,
            confidence=0.0,
            node_ids=[],
            edge_ids=[]
        )

route_agent = RouteOptimizationAgent()

EMERGENCY_HUBS = {
    "Ambulance": [
        {"id": "AMB-HUB-01", "name": "Aster CMI Emergency Ambulance Hub", "lat": 13.0473, "lng": 77.5908},
        {"id": "AMB-HUB-02", "name": "Manipal Ambulance Base Depot", "lat": 12.9585, "lng": 77.6483},
        {"id": "AMB-HUB-03", "name": "Victoria Hospital Paramedic Base", "lat": 12.9634, "lng": 77.5746},
    ],
    "Fire Engine": [
        {"id": "FIRE-HUB-01", "name": "Hebbal Fire & Rescue Station #4", "lat": 13.0336, "lng": 77.5891},
        {"id": "FIRE-HUB-02", "name": "High Grounds Central Fire Station", "lat": 12.9866, "lng": 77.5878},
        {"id": "FIRE-HUB-03", "name": "Jayanagar Emergency Fire Station", "lat": 12.9298, "lng": 77.5831},
    ],
    "Police Cruiser": [
        {"id": "POLICE-HUB-01", "name": "Hebbal Police Patrol Base", "lat": 13.0350, "lng": 77.5880},
        {"id": "POLICE-HUB-02", "name": "Central MG Road Police Precinct", "lat": 12.9754, "lng": 77.6071},
        {"id": "POLICE-HUB-03", "name": "Cubbon Park Traffic Police Hub", "lat": 12.9740, "lng": 77.5930},
    ],
    "Disaster Rescue": [
        {"id": "DISASTER-HUB-01", "name": "NDRF Disaster Rescue Hub North", "lat": 13.0550, "lng": 77.6120},
        {"id": "DISASTER-HUB-02", "name": "Civil Defence Emergency Depot", "lat": 12.9600, "lng": 77.6000},
    ]
}

def haversine_dist(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def find_nearest_hub(vehicle_type, citizen_lat, citizen_lng):
    hubs = EMERGENCY_HUBS.get(vehicle_type) or EMERGENCY_HUBS["Ambulance"]
    best_hub = hubs[0]
    min_dist = haversine_dist(citizen_lat, citizen_lng, best_hub["lat"], best_hub["lng"])
    for h in hubs[1:]:
        d = haversine_dist(citizen_lat, citizen_lng, h["lat"], h["lng"])
        if d < min_dist:
            min_dist = d
            best_hub = h
    return best_hub
