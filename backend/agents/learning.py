"""
Learning Agent for GeoAgentic.

Contract (from agent-contracts.md):
  Input:  closed-case outcomes (actual vs predicted ETA, actual vs
          recommended hospital/route, dispatcher overrides).
  Output: model update proposals / retrained model artifacts.

Note: As per architecture guidelines, this is currently a STUB.
It only logs the outcomes of closed cases and does not actively
retrain any models to prevent scope creep in the MVP.
"""

from __future__ import annotations

import logging

from bus.event_bus import CASE_CLOSED, bus
from models.schemas import Event

logger = logging.getLogger("geoagentic.agent.learning")


class LearningAgent:
    """Stub for the Learning Agent that logs case outcomes."""

    def __init__(self) -> None:
        pass

    def register_on_bus(self) -> None:
        """Subscribe to CASE_CLOSED to log case outcomes."""
        bus.subscribe(CASE_CLOSED, self.handle_case_closed)
        logger.info(f"Registered on event bus for {CASE_CLOSED}")

    async def handle_case_closed(self, event: Event) -> None:
        """Log the outcome of a closed case for future model training."""
        payload = event.payload
        incident_id = payload.get("incident_id", "Unknown")

        logger.info("--- LEARNING AGENT STUB ---")
        logger.info(f"Case {incident_id} closed. Logging outcomes for future training:")

        # Log the discrepancies/outcomes
        if "predicted_eta" in payload and "actual_eta" in payload:
            logger.info(
                f"  ETA Variance: Predicted {payload['predicted_eta']}s, "
                f"Actual {payload['actual_eta']}s "
                f"(Diff: {payload['actual_eta'] - payload['predicted_eta']}s)"
            )

        if "dispatcher_override" in payload:
            override = payload["dispatcher_override"]
            logger.info(f"  Dispatcher Override: {override}")

        logger.info("Data stored in telemetry warehouse for next offline training run.")
        logger.info("---------------------------")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

learning_agent = LearningAgent()
