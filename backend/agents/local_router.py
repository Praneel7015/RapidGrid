"""
Offline graph router — the resilience tier beneath the Google Routes API.

Implements the cost model specified in
``.skills/emergency-routing/references/route-scoring.md``:

    edge_cost = base_travel_time
              x congestion_multiplier
              x incident_multiplier
              x weather_multiplier

Blocked segments are excluded from the search entirely.

Why this exists
---------------
The Google Routes tier fails for reasons outside our control — quota
exhaustion, a disabled API, venue wi-fi, billing. When it does, the previous
behaviour was to return a straight line with ``eta=0.0`` and pretend it was a
route. This module computes a real path over a real road network instead, and
labels it ``CACHED`` so the provenance is never overstated.

It is also what makes traffic *matter*: because the Traffic Intelligence Agent
writes multipliers onto edges here, a road closure genuinely changes the path
the ambulance takes, rather than decorating a score nobody consumes.
"""

from __future__ import annotations

import gzip
import heapq
import json
import logging
import math
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger("geoagentic.agent.local_router")

_EARTH_R = 6_371_000

# Fastest possible emergency travel (motorway 80km/h x 1.25 priority factor)
# expressed in m/s. Used as the A* heuristic divisor: dividing straight-line
# distance by the maximum achievable speed can never overestimate remaining
# cost, which keeps the heuristic admissible and the result optimal.
_MAX_SPEED_MS = (80.0 * 1.25) / 3.6

# Spatial index bucket size in degrees (~550 m at Bengaluru's latitude).
_GRID = 0.005

_GRAPH_PATH = Path(__file__).resolve().parent.parent / "data" / "bengaluru_road_graph.json.gz"


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class LocalGraphRouter:
    """A* pathfinding over the cached OpenStreetMap road graph."""

    def __init__(self) -> None:
        self._loaded = False
        self.lat: list[float] = []
        self.lng: list[float] = []
        self.road_names: list[str] = []
        self.road_classes: list[str] = []

        # Adjacency in CSR-like form: for node i, its outgoing edges are
        # _adj_edges[_adj_start[i]:_adj_start[i + 1]].
        self._adj_start: list[int] = []
        self._adj_edges: list[int] = []

        self.edge_to: list[int] = []
        self.edge_time: list[float] = []
        self.edge_len: list[float] = []
        self.edge_name: list[int] = []
        self.edge_class: list[int] = []

        # Runtime traffic state, written by the Traffic Intelligence Agent.
        # Sparse: only edges that deviate from neutral are stored.
        self._congestion: dict[int, float] = {}
        self._incident: dict[int, float] = {}
        self._weather: float = 1.0
        self._blocked: set[int] = set()

        self._grid: dict[tuple[int, int], list[int]] = {}
        self.meta: dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when a road graph has been built and loaded."""
        return self._loaded

    def load(self, path: Path | None = None) -> bool:
        """Load the cached graph. Returns False if it hasn't been built yet."""
        path = path or _GRAPH_PATH
        if not path.exists():
            logger.warning(
                "Road graph not found at %s - run scripts/build_road_graph.py "
                "to enable offline routing.",
                path.name,
            )
            return False

        started = time.time()
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)

        self.meta = data.get("meta", {})
        self.lat = data["node_lat"]
        self.lng = data["node_lng"]
        self.road_names = data["road_names"]
        self.road_classes = data["road_classes"]
        self.edge_to = data["edge_to"]
        self.edge_time = data["edge_time"]
        self.edge_len = data["edge_len"]
        self.edge_name = data["edge_name"]
        self.edge_class = data["edge_class"]
        edge_from = data["edge_from"]

        self._build_adjacency(edge_from)
        self._build_spatial_index()

        self._loaded = True
        logger.info(
            "Local road graph loaded: %d nodes, %d edges in %.2fs (source: %s)",
            len(self.lat),
            len(self.edge_to),
            time.time() - started,
            self.meta.get("source", "unknown"),
        )
        return True

    def _build_adjacency(self, edge_from: list[int]) -> None:
        """Bucket edges by origin node into a CSR-style adjacency structure."""
        n_nodes = len(self.lat)
        counts = [0] * (n_nodes + 1)
        for src in edge_from:
            counts[src + 1] += 1
        for i in range(1, n_nodes + 1):
            counts[i] += counts[i - 1]

        self._adj_start = counts
        self._adj_edges = [0] * len(edge_from)
        cursor = counts[:]
        for edge_id, src in enumerate(edge_from):
            self._adj_edges[cursor[src]] = edge_id
            cursor[src] += 1

    def _build_spatial_index(self) -> None:
        """Grid-bucket every node so nearest-node snapping is O(1)-ish."""
        grid: dict[tuple[int, int], list[int]] = {}
        for i, (la, ln) in enumerate(zip(self.lat, self.lng)):
            grid.setdefault((int(la / _GRID), int(ln / _GRID)), []).append(i)
        self._grid = grid

    # -- traffic state --------------------------------------------------------

    def apply_traffic(
        self,
        congestion: dict[int, float] | None = None,
        incidents: dict[int, float] | None = None,
        blocked: Iterable[int] | None = None,
        weather_multiplier: float = 1.0,
    ) -> None:
        """
        Overlay live traffic onto the graph.

        This is the hook that makes Traffic Intelligence actually influence
        routing rather than only scoring.
        """
        self._congestion = dict(congestion or {})
        self._incident = dict(incidents or {})
        self._blocked = set(blocked or ())
        self._weather = weather_multiplier

    def clear_traffic(self) -> None:
        self.apply_traffic()

    def block_edges(self, edge_ids: Iterable[int]) -> None:
        self._blocked.update(edge_ids)

    def unblock_all(self) -> None:
        self._blocked.clear()

    @property
    def blocked_count(self) -> int:
        return len(self._blocked)

    def _edge_cost(self, edge_id: int) -> float:
        """route-scoring.md cost model."""
        return (
            self.edge_time[edge_id]
            * self._congestion.get(edge_id, 1.0)
            * self._incident.get(edge_id, 1.0)
            * self._weather
        )

    # -- queries --------------------------------------------------------------

    def nearest_node(self, lat: float, lng: float, max_rings: int = 12) -> int | None:
        """Snap a coordinate to the closest graph node, widening the search ring."""
        gy, gx = int(lat / _GRID), int(lng / _GRID)
        best_id, best_dist = None, float("inf")

        for ring in range(max_rings):
            candidates: list[int] = []
            for dy in range(-ring, ring + 1):
                for dx in range(-ring, ring + 1):
                    # Only scan the perimeter of each new ring.
                    if ring and abs(dy) != ring and abs(dx) != ring:
                        continue
                    candidates.extend(self._grid.get((gy + dy, gx + dx), ()))

            for node_id in candidates:
                d = haversine(lat, lng, self.lat[node_id], self.lng[node_id])
                if d < best_dist:
                    best_id, best_dist = node_id, d

            # A hit inside a fully-scanned ring can't be beaten by an outer one.
            if best_id is not None and ring >= 1:
                break

        return best_id

    def find_path(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
    ) -> dict[str, Any] | None:
        """
        A* shortest-time path.

        Returns a dict with coordinates, travel time, distance and the road
        names traversed — or None if either endpoint can't be snapped or no
        connected path exists.
        """
        if not self._loaded:
            return None

        start = self.nearest_node(*origin)
        goal = self.nearest_node(*destination)
        if start is None or goal is None:
            logger.warning("Could not snap origin/destination onto the road graph")
            return None
        if start == goal:
            return None

        goal_lat, goal_lng = self.lat[goal], self.lng[goal]

        def heuristic(node: int) -> float:
            return haversine(self.lat[node], self.lng[node], goal_lat, goal_lng) / _MAX_SPEED_MS

        g_score: dict[int, float] = {start: 0.0}
        came_from: dict[int, tuple[int, int]] = {}  # node -> (prev_node, edge_id)
        open_heap: list[tuple[float, int]] = [(heuristic(start), start)]
        closed: set[int] = set()
        expansions = 0

        adj_start, adj_edges, edge_to = self._adj_start, self._adj_edges, self.edge_to
        blocked = self._blocked

        while open_heap:
            _, current = heapq.heappop(open_heap)
            if current in closed:
                continue
            if current == goal:
                break
            closed.add(current)
            expansions += 1

            current_g = g_score[current]
            for slot in range(adj_start[current], adj_start[current + 1]):
                edge_id = adj_edges[slot]
                if edge_id in blocked:
                    continue
                neighbour = edge_to[edge_id]
                if neighbour in closed:
                    continue
                tentative = current_g + self._edge_cost(edge_id)
                if tentative < g_score.get(neighbour, float("inf")):
                    g_score[neighbour] = tentative
                    came_from[neighbour] = (current, edge_id)
                    heapq.heappush(open_heap, (tentative + heuristic(neighbour), neighbour))

        if goal not in g_score:
            logger.warning("No connected path found between the snapped endpoints")
            return None

        # Walk the predecessor chain back to the origin.
        node_path: list[int] = [goal]
        edge_path: list[int] = []
        cursor = goal
        while cursor != start:
            prev, edge_id = came_from[cursor]
            edge_path.append(edge_id)
            node_path.append(prev)
            cursor = prev
        node_path.reverse()
        edge_path.reverse()

        coordinates = [{"lat": self.lat[n], "lng": self.lng[n]} for n in node_path]
        distance_m = sum(self.edge_len[e] for e in edge_path)
        travel_time_s = g_score[goal]

        # Ordered, de-duplicated road names for a human-readable description.
        roads: list[str] = []
        for e in edge_path:
            name = self.road_names[self.edge_name[e]]
            if not roads or roads[-1] != name:
                roads.append(name)

        return {
            "coordinates": coordinates,
            "distance_m": round(distance_m, 1),
            "travel_time_s": round(travel_time_s, 1),
            "roads": roads,
            "edge_ids": edge_path,
            "node_ids": node_path,
            "nodes_expanded": expansions,
            "snap_distance_m": round(
                haversine(origin[0], origin[1], self.lat[start], self.lng[start]), 1
            ),
        }


local_router = LocalGraphRouter()
