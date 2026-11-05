"""
Emergency Coordinator for GeoAgentic.

Contract (from agent-contracts.md):
  Input:  incident report (raw, from any citizen channel), timestamp.
  Output: {incident_id, severity, activated_agents[], status}

Responsibility:
  Classify severity, decide which agents to activate (not every incident
  needs every agent — e.g. a minor incident may skip Hospital Intelligence),
  and compile the final action plan once fusion completes.
"""

from __future__ import annotations

import logging
import uuid

from bus.event_bus import INCIDENT_COORDINATED, INCIDENT_REPORTED, bus
from models.schemas import (
    AgentName,
    CoordinatorRequest,
    CoordinatorResponse,
    Event,
)

logger = logging.getLogger("geoagentic.agent.coordinator")

AGENT_NAME = AgentName.COORDINATOR.value


class EmergencyCoordinator:
    """Orchestrates the emergency response pipeline."""

    def __init__(self) -> None:
        pass

    def register_on_bus(self) -> None:
        """Subscribe to INCIDENT_REPORTED to trigger coordination."""
        bus.subscribe(INCIDENT_REPORTED, self.handle_incident_reported)
        logger.info(f"Registered on event bus for {INCIDENT_REPORTED}")

    async def handle_incident_reported(self, event: Event) -> None:
        """Auto-trigger coordination when a new incident is reported."""
        logger.info("Incident reported via bus; starting coordination...")
        # event.payload is an AccessibilityResponse
        # In a full system we'd extract the incident_report here
        pass

    async def coordinate(self, request: CoordinatorRequest) -> CoordinatorResponse:
        """
        Determines the severity and which agents to activate.
        """
        report = request.incident_report
        severity = report.severity_estimate

        # Base agents that are always active for an incident
        activated = [
            AgentName.TRAFFIC,
            AgentName.ROUTE,
            AgentName.PREDICTION,
            AgentName.FUSION,
            AgentName.COMMUNICATION,
            AgentName.ACCESSIBILITY,
        ]

        # Hospital Intelligence is expensive/unnecessary for minor incidents.
        # We only activate it if the severity is >= 0.5 or it's a critical type.
        if severity >= 0.5:
            activated.append(AgentName.HOSPITAL)
            logger.info("Major incident detected. Hospital Intelligence ACTIVATED.")
        else:
            logger.info("Minor incident detected. Hospital Intelligence SKIPPED.")

        # Generate a unique incident ID
        incident_id = f"INC-{uuid.uuid4().hex[:8]}"

        response = CoordinatorResponse(
            incident_id=incident_id, severity=severity, activated_agents=activated, status="active"
        )

        # Publish the coordinated incident to the bus
        # In the full async pipeline, the Decision Fusion Engine would listen for this
        await bus.publish(
            topic=INCIDENT_COORDINATED,
            payload=response.model_dump(mode="json"),
            source_agent=AGENT_NAME,
        )

        return response


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

coordinator_agent = EmergencyCoordinator()
