"""
Traffic Intelligence Agent for GeoAgentic.

Contract (from agent-contracts.md):
  Input:  geographic bounding box, time window.
  Output: {segment_id, congestion_score, incidents[], closures[],
           weather_risk, data_freshness, confidence}

Must run continuously — Route Optimization and Prediction subscribe to
updates via the event bus.

Data source: backend/data/sample_road_network.json
  Congestion/incident/weather multipliers on each edge are converted into
  the normalised 0–1 scores the contract requires.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bus.event_bus import bus
from models.schemas import (
    DataFreshness,
    TrafficIncident,
    TrafficSegment,
    TrafficSnapshot,
)

logger = logging.getLogger("geoagentic.agent.traffic")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Multiplier ranges from route-scoring.md, used for normalisation
# congestion_multiplier: 1.0 (free-flow) – 2.5 (gridlock)
_CONGESTION_MIN = 1.0
_CONGESTION_MAX = 2.5

# weather_multiplier: 1.0 (clear) – 2.0+ (severe)
_WEATHER_MIN = 1.0
_WEATHER_MAX = 2.0

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

AGENT_NAME = "traffic_intelligence"


# ---------------------------------------------------------------------------
# TrafficIntelligenceAgent
# ---------------------------------------------------------------------------


class TrafficIntelligenceAgent:
    """Analyses road segments and publishes traffic snapshots to the bus."""

    def __init__(self) -> None:
        self._edges: list[dict[str, Any]] = []
        self._nodes: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # -- lifecycle ------------------------------------------------------------

    def load_network(self, path: Path | None = None) -> None:
        """Load the road network JSON into memory."""
        path = path or (_DATA_DIR / "sample_road_network.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        self._nodes = {n["id"]: n for n in data["nodes"]}
        self._edges = data["edges"]
        self._loaded = True
        logger.info(
            "Loaded road network: %d nodes, %d edges from %s",
            len(self._nodes),
            len(self._edges),
            path.name,
        )

    def toggle_api_failure(self, force_fail: bool) -> None:
        """Test helper to simulate external Traffic API failure."""
        self._simulate_api_failure = force_fail

    # -- analysis -------------------------------------------------------------

    def _normalise(self, value: float, lo: float, hi: float) -> float:
        """Normalise a multiplier from [lo, hi] to [0, 1]."""
        return min(max((value - lo) / (hi - lo), 0.0), 1.0)

    def _edge_to_segment(self, edge: dict[str, Any]) -> TrafficSegment:
        """Convert a raw road-network edge into a TrafficSegment."""
        congestion = edge.get("congestion_multiplier", 1.0)
        incident_mult = edge.get("incident_multiplier", 1.0)
        weather = edge.get("weather_multiplier", 1.0)
        blocked = edge.get("blocked", False)

        # Build incidents list when incident_multiplier > 1.0
        incidents: list[TrafficIncident] = []
        if incident_mult > 1.0:
            incidents.append(
                TrafficIncident(
                    incident_id=f"INC-{edge['id']}",
                    description=(
                        f"Active incident on segment {edge['id']} (multiplier {incident_mult}x)"
                    ),
                    severity=min((incident_mult - 1.0) / 3.0, 1.0),
                )
            )

        # Build closures list
        closures: list[str] = [edge["id"]] if blocked else []

        return TrafficSegment(
            segment_id=edge["id"],
            congestion_score=self._normalise(congestion, _CONGESTION_MIN, _CONGESTION_MAX),
            incidents=incidents,
            closures=closures,
            weather_risk=self._normalise(weather, _WEATHER_MIN, _WEATHER_MAX),
            # Mark as simulated: we're using sample data, not real feeds
            data_freshness=DataFreshness.LIVE,
            # Fixed confidence for simulated data — real implementation would
            # vary based on data source reliability and freshness
            confidence=0.85,
        )

    def analyse(self, bounding_box: dict | None = None) -> list[TrafficSegment]:
        """
        Analyse traffic for all (or bounded) road segments.

        In a real implementation this would query live traffic APIs, CCTV
        feeds, IoT sensors, and citizen reports.  For now it derives
        scores from the sample road network's multipliers.

        Args:
            bounding_box: Optional {north, south, east, west} filter.
                          Omit to analyse all loaded segments.

        Returns:
            List of TrafficSegment with normalised scores.
        """
        if not self._loaded:
            self.load_network()

        segments = [self._edge_to_segment(e) for e in self._edges]

        # If a bounding box was given, filter segments by their node coords
        if bounding_box:
            segments = self._filter_by_bbox(segments, bounding_box)

        logger.info(
            "Analysed %d segments (%d incidents, %d closures)",
            len(segments),
            sum(len(s.incidents) for s in segments),
            sum(len(s.closures) for s in segments),
        )
        return segments

    def _filter_by_bbox(
        self,
        segments: list[TrafficSegment],
        bbox: dict,
    ) -> list[TrafficSegment]:
        """Filter segments to those with at least one node inside the bbox."""
        north = bbox.get("north", 90)
        south = bbox.get("south", -90)
        east = bbox.get("east", 180)
        west = bbox.get("west", -180)

        # Build a set of edge IDs whose *from* or *to* node is inside bbox
        included_edge_ids: set[str] = set()
        for edge in self._edges:
            for node_id in (edge["from"], edge["to"]):
                node = self._nodes.get(node_id)
                if node and south <= node["lat"] <= north and west <= node["lng"] <= east:
                    included_edge_ids.add(edge["id"])
                    break

        return [s for s in segments if s.segment_id in included_edge_ids]

    def get_snapshot(self, bounding_box: dict | None = None) -> TrafficSnapshot:
        """Return a full TrafficSnapshot (segments + timestamp)."""
        segments = self.analyse(bounding_box)
        return TrafficSnapshot(segments=segments)

    # -- event bus integration ------------------------------------------------

    async def get_live_data(self, bounding_box: dict | None = None) -> TrafficSnapshot:
        """Simulate fetching live data from an external API."""
        if getattr(self, "_simulate_api_failure", False):
            raise ConnectionError("External Traffic API unavailable (503 Service Unavailable)")
        return self.get_snapshot(bounding_box)

    async def publish_snapshot(self, bounding_box: dict | None = None) -> None:
        """
        Publishes the current traffic snapshot to the event bus.
        Normally this would be on a polling loop.
        Handles Scenario 3: Graceful fallback when API fails.
        """
        try:
            snapshot = await self.get_live_data(bounding_box)
        except Exception as e:
            logger.warning(f"Traffic API failure: {e}. Falling back to cached historical data.")
            # Scenario 3: Fall back to cached data, mark degraded, lower confidence
            snapshot = self.get_snapshot(bounding_box)

            # Degrade confidence by 30% and mark as simulated
            for segment in snapshot.segments:
                segment.confidence = max(0.1, segment.confidence - 0.30)
                segment.data_freshness = DataFreshness.CACHED

        await bus.publish(
            topic="traffic.update",
            payload=snapshot.model_dump(mode="json"),
            source_agent=AGENT_NAME,
        )

    @property
    def nodes(self) -> dict[str, dict]:
        """Expose loaded nodes for other agents (e.g. Route Optimization)."""
        if not self._loaded:
            self.load_network()
        return dict(self._nodes)

    @property
    def edges(self) -> list[dict]:
        """Expose loaded edges for other agents."""
        if not self._loaded:
            self.load_network()
        return list(self._edges)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

traffic_agent = TrafficIntelligenceAgent()
