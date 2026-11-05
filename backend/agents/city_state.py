"""
City state - a structural causal model of Bengaluru's road network.

WHAT THIS IS, PRECISELY
-----------------------
This is a *structural causal model*, not causal inference. The mechanisms below
are specified from domain knowledge and literature, not learned from data - we
have no historical incident corpus to identify effects from, and claiming
otherwise would not survive one informed question.

What that buys us is Pearl's rungs 2 and 3 for free. In the real world you
cannot run `do(close Bellary Road)` - you would have to actually close a road.
Inside a model we own, interventions and counterfactuals are simply available.
That is the honest and defensible framing: we skip rung 1 (learning structure
from observation) and say so.

THE CAUSAL GRAPH
----------------
    TimeOfDay ─┐
    Weather ───┼──▶ Congestion(edge) ──▶ ETA(route) ──▶ TimeToTreatment
    Closure ───┘         │  ▲
       │                 │  └── displacement ──┐
       └── do() ─────────┘                     │
                                    neighbouring edges

The load-bearing edge is DISPLACEMENT. Traffic removed from a closed road does
not evaporate; it reappears on parallel routes, which congest, which slows a
*different* ambulance somewhere else in the city. That second-order effect is
what makes the model feel like a living city rather than a static map, and it
is the thing a purely local "mark this road blocked" implementation misses.

CALIBRATION - READ THIS BEFORE QUOTING NUMBERS
----------------------------------------------
The coefficients are literature-informed estimates, NOT fitted to Bengaluru
traffic counts. Directionally they are sound: closing an arterial raises
congestion on parallel roads, rain slows everything, rush hour is worse than
3am. The magnitudes are plausible rather than validated. Every output carries
`provenance="modelled"` and the UI must say so.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("geoagentic.city")

# --- Diurnal congestion by road class ---------------------------------------
# Multipliers on free-flow travel time. Bengaluru's arterials are notoriously
# worse than its side streets at peak, so the profile is class-dependent.
_PEAK_MORNING = (8, 11)
_PEAK_EVENING = (17, 21)

_CLASS_PEAK_SENSITIVITY: dict[str, float] = {
    "motorway": 0.55,
    "motorway_link": 0.55,
    "trunk": 0.85,
    "trunk_link": 0.85,
    "primary": 1.00,
    "primary_link": 1.00,
    "secondary": 0.80,
    "secondary_link": 0.80,
    "tertiary": 0.55,
    "tertiary_link": 0.55,
}

_PEAK_AMPLITUDE = 0.95  # +95% travel time on a primary road at full peak
_OFFPEAK_FLOOR = 1.02
_NIGHT_RELIEF = 0.88  # 23:00-05:00 runs faster than free-flow assumption

# --- Weather ----------------------------------------------------------------
_WEATHER_MULTIPLIER: dict[str, float] = {
    "Thunderstorm": 1.55,
    "Rain": 1.35,
    "Drizzle": 1.18,
    "Snow": 1.60,
    "Fog": 1.30,
    "Mist": 1.15,
    "Haze": 1.08,
    "Smoke": 1.08,
    "Dust": 1.12,
    "Clouds": 1.00,
    "Clear": 1.00,
}

# --- Displacement -----------------------------------------------------------
# Traffic from a closed segment reappears nearby, decaying with graph distance.
_DISPLACEMENT_HOPS = 4
_DISPLACEMENT_STRENGTH = 0.85  # peak added congestion on immediate neighbours
_DISPLACEMENT_DECAY = 0.55  # per hop
_DISPLACEMENT_CAP = 2.6  # no edge exceeds this total multiplier

# An incident (not a closure) partially obstructs its own segment.
_INCIDENT_SELF = 1.9
_INCIDENT_SPILL_HOPS = 2
_INCIDENT_STRENGTH = 0.35


@dataclass
class Intervention:
    """One `do()` applied to the city."""

    kind: str  # close_road | incident | weather | time
    target: str
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


class CityState:
    """
    Live causal state over the road graph.

    Owns the multipliers the router reads. Nothing here mutates geometry - only
    the cost of traversing it - so a route recomputed after an intervention
    differs *because the city changed*, not because the router changed.
    """

    def __init__(self) -> None:
        self._router = None
        self.hour: int = datetime.now().hour
        self.weather: str = "Clear"
        self.closed_edges: set[int] = set()
        self.incident_edges: set[int] = set()
        self.interventions: list[Intervention] = []

        # edge -> multiplier, by cause. Kept separate so a segment can explain
        # which mechanism slowed it rather than reporting one opaque number.
        self._diurnal: dict[int, float] = {}
        self._displacement: dict[int, float] = {}
        self._incident: dict[int, float] = {}

        self._node_edges: dict[int, list[int]] | None = None

    # -- wiring ---------------------------------------------------------------

    def attach(self, router) -> bool:
        """Bind to the loaded road graph."""
        if not router.available and not router.load():
            logger.warning("City state has no road graph to model")
            return False
        self._router = router
        self._build_node_index()
        self.set_hour(datetime.now().hour)
        logger.info(
            "City state attached: %d edges under causal model",
            len(router.edge_to),
        )
        return True

    def _build_node_index(self) -> None:
        """Undirected node -> edges index, for spreading effects outward."""
        r = self._router
        index: dict[int, list[int]] = {}
        for edge_id, to_node in enumerate(r.edge_to):
            index.setdefault(to_node, []).append(edge_id)
        for node in range(len(r.lat)):
            start, end = r._adj_start[node], r._adj_start[node + 1]
            if start != end:
                index.setdefault(node, []).extend(r._adj_edges[start:end])
        self._node_edges = index

    @property
    def ready(self) -> bool:
        return self._router is not None

    # -- exogenous variables --------------------------------------------------

    def set_hour(self, hour: int) -> None:
        """TimeOfDay -> Congestion. Recomputes the diurnal layer."""
        self.hour = int(hour) % 24
        self._recompute_diurnal()
        self.interventions.append(Intervention(kind="time", target=f"{self.hour:02d}:00"))
        self._push()

    def set_weather(self, condition: str) -> None:
        """Weather -> Congestion. Applied globally at push time."""
        self.weather = condition or "Clear"
        self.interventions.append(
            Intervention(
                kind="weather",
                target=self.weather,
                detail={"multiplier": _WEATHER_MULTIPLIER.get(self.weather, 1.0)},
            )
        )
        self._push()

    def _peak_factor(self) -> float:
        """0 at the quietest hour, 1 at full peak."""
        h = self.hour
        if _PEAK_MORNING[0] <= h < _PEAK_MORNING[1]:
            span = _PEAK_MORNING
        elif _PEAK_EVENING[0] <= h < _PEAK_EVENING[1]:
            span = _PEAK_EVENING
        else:
            return 0.0
        # Smooth bell across the peak window rather than a step.
        mid = (span[0] + span[1]) / 2
        width = (span[1] - span[0]) / 2
        return max(0.0, math.cos((h - mid) / width * (math.pi / 2)))

    def _recompute_diurnal(self) -> None:
        if not self._router:
            return
        r = self._router
        peak = self._peak_factor()
        night = 23 <= self.hour or self.hour < 5
        self._diurnal = {}
        for edge_id, class_idx in enumerate(r.edge_class):
            road_class = r.road_classes[class_idx]
            sensitivity = _CLASS_PEAK_SENSITIVITY.get(road_class, 0.7)
            if night:
                value = _NIGHT_RELIEF
            else:
                value = _OFFPEAK_FLOOR + (_PEAK_AMPLITUDE * peak * sensitivity)
            if abs(value - 1.0) > 0.01:
                self._diurnal[edge_id] = round(value, 3)

    # -- interventions --------------------------------------------------------

    def close_road(self, edge_ids: Iterable[int], label: str = "Road closure") -> dict:
        """
        do(close these segments).

        The closure removes them from routing AND displaces their traffic onto
        the surrounding network - which is the part that makes a second,
        unrelated ambulance slower.
        """
        ids = [int(e) for e in edge_ids]
        if not ids:
            return {"closed": 0, "displaced": 0}
        self.closed_edges.update(ids)
        affected = self._spread(
            ids,
            _DISPLACEMENT_HOPS,
            _DISPLACEMENT_STRENGTH,
            _DISPLACEMENT_DECAY,
            self._displacement,
        )
        self.interventions.append(
            Intervention(
                kind="close_road",
                target=label,
                detail={"segments": len(ids), "displaced_onto": len(affected)},
            )
        )
        logger.info(
            "do(close_road): %d segments closed, congestion displaced onto %d neighbours",
            len(ids),
            len(affected),
        )
        self._push()
        return {"closed": len(ids), "displaced": len(affected)}

    def report_incident(self, edge_ids: Iterable[int], label: str = "Incident") -> dict:
        """do(incident here) - partial obstruction, smaller spill than a closure."""
        ids = [int(e) for e in edge_ids]
        if not ids:
            return {"segments": 0, "affected": 0}
        self.incident_edges.update(ids)
        for e in ids:
            self._incident[e] = max(self._incident.get(e, 1.0), _INCIDENT_SELF)
        affected = self._spread(
            ids,
            _INCIDENT_SPILL_HOPS,
            _INCIDENT_STRENGTH,
            _DISPLACEMENT_DECAY,
            self._incident,
        )
        self.interventions.append(
            Intervention(
                kind="incident",
                target=label,
                detail={"segments": len(ids), "affected": len(affected)},
            )
        )
        self._push()
        return {"segments": len(ids), "affected": len(affected)}

    def clear(self) -> None:
        """Undo every intervention and return the city to baseline."""
        self.closed_edges.clear()
        self.incident_edges.clear()
        self._displacement.clear()
        self._incident.clear()
        self.interventions.append(Intervention(kind="clear", target="all"))
        self._push()
        logger.info("City state cleared - all interventions reverted")

    # -- propagation ----------------------------------------------------------

    def _spread(
        self,
        seeds: list[int],
        hops: int,
        strength: float,
        decay: float,
        sink: dict[int, float],
    ) -> set[int]:
        """
        Breadth-first congestion spread outward from seed edges.

        Effect on a neighbour falls off geometrically with hop distance, so a
        closure bites hardest on the immediately parallel streets and fades
        rather than stopping abruptly at an arbitrary radius.
        """
        if not self._router or not self._node_edges:
            return set()
        r = self._router
        seen: set[int] = set(seeds)
        touched: set[int] = set()
        frontier = deque((e, 0) for e in seeds)

        while frontier:
            edge_id, depth = frontier.popleft()
            if depth >= hops:
                continue
            # Spread from BOTH endpoints. Walking only the destination node
            # propagated the effect downstream but never back up the road the
            # traffic actually diverts onto, which under-reported the spill.
            origin = self._edge_origin(edge_id)
            endpoints = (r.edge_to[edge_id],) if origin is None else (origin, r.edge_to[edge_id])
            for node in endpoints:
                for neighbour in self._node_edges.get(node, ()):
                    if neighbour in seen:
                        continue
                    seen.add(neighbour)
                    added = strength * (decay**depth)
                    if added < 0.02:
                        continue
                    current = sink.get(neighbour, 1.0)
                    sink[neighbour] = min(_DISPLACEMENT_CAP, current + added)
                    touched.add(neighbour)
                    frontier.append((neighbour, depth + 1))
        return touched

    # -- output ---------------------------------------------------------------

    def _push(self) -> None:
        """Write the combined multipliers onto the router."""
        if not self._router:
            return
        combined: dict[int, float] = {}
        for source in (self._diurnal, self._displacement):
            for edge_id, value in source.items():
                combined[edge_id] = combined.get(edge_id, 1.0) * value
        self._router.apply_traffic(
            congestion=combined,
            incidents=dict(self._incident),
            blocked=self.closed_edges,
            weather_multiplier=_WEATHER_MULTIPLIER.get(self.weather, 1.0),
        )

    def explain(self, edge_id: int) -> dict[str, Any]:
        """Causal chain for one segment - which mechanism slowed it, and by how much."""
        chain = []
        diurnal = self._diurnal.get(edge_id, 1.0)
        if abs(diurnal - 1.0) > 0.01:
            chain.append(
                {
                    "cause": "time_of_day",
                    "detail": f"{self.hour:02d}:00 traffic profile",
                    "multiplier": round(diurnal, 2),
                }
            )
        weather = _WEATHER_MULTIPLIER.get(self.weather, 1.0)
        if weather > 1.0:
            chain.append(
                {
                    "cause": "weather",
                    "detail": self.weather,
                    "multiplier": round(weather, 2),
                }
            )
        displaced = self._displacement.get(edge_id, 1.0)
        if displaced > 1.0:
            chain.append(
                {
                    "cause": "displacement",
                    "detail": "traffic displaced from a nearby closure",
                    "multiplier": round(displaced, 2),
                }
            )
        incident = self._incident.get(edge_id, 1.0)
        if incident > 1.0:
            chain.append(
                {
                    "cause": "incident",
                    "detail": "active incident on or beside this segment",
                    "multiplier": round(incident, 2),
                }
            )
        total = diurnal * weather * displaced * incident
        return {
            "edge_id": edge_id,
            "blocked": edge_id in self.closed_edges,
            "causes": chain,
            "total_multiplier": round(total, 2),
            "provenance": "modelled",
        }

    def congestion_overlay(self, limit: int = 600) -> list[dict[str, Any]]:
        """
        Worst-affected segments as drawable geometry, for the map heatmap.

        Sorted by severity so a capped list still shows the interesting parts.
        """
        if not self._router:
            return []
        r = self._router
        scored: list[tuple[float, int]] = []
        for edge_id in set(self._displacement) | set(self._incident):
            value = self._displacement.get(edge_id, 1.0) * self._incident.get(edge_id, 1.0)
            if value > 1.05:
                scored.append((value, edge_id))
        scored.sort(reverse=True)

        out = []
        for value, edge_id in scored[:limit]:
            to_node = r.edge_to[edge_id]
            from_node = self._edge_origin(edge_id)
            if from_node is None:
                continue
            out.append(
                {
                    "edge_id": edge_id,
                    "multiplier": round(value, 2),
                    "road": r.road_names[r.edge_name[edge_id]],
                    "coordinates": [
                        {"lat": r.lat[from_node], "lng": r.lng[from_node]},
                        {"lat": r.lat[to_node], "lng": r.lng[to_node]},
                    ],
                }
            )
        return out

    def closure_overlay(self) -> list[dict[str, Any]]:
        """Closed segments as drawable geometry."""
        if not self._router:
            return []
        r = self._router
        out = []
        for edge_id in self.closed_edges:
            from_node = self._edge_origin(edge_id)
            if from_node is None:
                continue
            out.append(
                {
                    "edge_id": edge_id,
                    "road": r.road_names[r.edge_name[edge_id]],
                    "coordinates": [
                        {"lat": r.lat[from_node], "lng": r.lng[from_node]},
                        {"lat": r.lat[r.edge_to[edge_id]], "lng": r.lng[r.edge_to[edge_id]]},
                    ],
                }
            )
        return out

    def _edge_origin(self, edge_id: int) -> int | None:
        """Recover an edge's origin node from the CSR adjacency."""
        r = self._router
        cache = getattr(self, "_origin_cache", None)
        if cache is None:
            cache = {}
            for node in range(len(r.lat)):
                for slot in range(r._adj_start[node], r._adj_start[node + 1]):
                    cache[r._adj_edges[slot]] = node
            self._origin_cache = cache
        return cache.get(edge_id)

    def find_edges_on_road(self, name_fragment: str, limit: int = 200) -> list[int]:
        """Look up segments by road name - how the dispatcher closes 'Bellary Road'."""
        if not self._router:
            return []
        r = self._router
        needle = name_fragment.lower().strip()
        matches = [
            i for i, name_idx in enumerate(r.edge_name) if needle in r.road_names[name_idx].lower()
        ]
        return matches[:limit]

    def snapshot(self) -> dict[str, Any]:
        return {
            "hour": self.hour,
            "weather": self.weather,
            "weather_multiplier": _WEATHER_MULTIPLIER.get(self.weather, 1.0),
            "peak_factor": round(self._peak_factor(), 2),
            "closed_segments": len(self.closed_edges),
            "incident_segments": len(self.incident_edges),
            "displaced_segments": len(self._displacement),
            "interventions": [
                {"kind": i.kind, "target": i.target, "detail": i.detail, "at": i.at}
                for i in self.interventions[-12:]
            ],
            "model": "structural-causal-v1",
            "provenance": "modelled",
            "calibration": (
                "Coefficients are literature-informed estimates, not fitted to "
                "Bengaluru traffic counts. Directionally sound; magnitudes "
                "unvalidated."
            ),
        }


city_state = CityState()
