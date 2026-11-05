"""
In-process async event bus for GeoAgentic.

Agents communicate asynchronously through this bus (message queue / event
stream), never via direct function calls — keeping agents independently
deployable and testable (see geoagentic-architecture SKILL.md,
"Communication bus" section).

This is an in-process implementation for local dev.  To swap to Redis
Pub/Sub or Streams later, replace this module — agents import only the
`bus` singleton and the topic constants; they never touch Redis directly.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from models.schemas import Event

logger = logging.getLogger("geoagentic.bus")

# ---------------------------------------------------------------------------
# Topic constants — add new topics here, never use raw strings in agents
# ---------------------------------------------------------------------------

TRAFFIC_UPDATE = "traffic.update"
ROUTE_REQUEST = "route.request"
ROUTE_COMPUTED = "route.computed"
HOSPITAL_RANKED = "hospital.ranked"
FUSION_COMPLETE = "fusion.complete"
PREDICTION_UPDATE = "prediction.update"
INCIDENT_REPORTED = "incident.reported"
INCIDENT_COORDINATED = "incident.coordinated"
COMMUNICATION_DISPATCHED = "communication.dispatched"
CASE_CLOSED = "case.closed"


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """
    Async in-process pub/sub.

    - publish(topic, payload, source_agent) → broadcasts to all subscribers
    - subscribe(topic, callback) → registers an async handler
    - get_latest(topic) → returns the most recent event (for polling/fusion)
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Event], Coroutine]]] = defaultdict(list)
        self._latest: dict[str, Event] = {}
        self._history: list[Event] = []

    # -- publish --------------------------------------------------------------

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        source_agent: str,
    ) -> Event:
        """Create an Event and broadcast it to all subscribers of *topic*."""
        event = Event(
            event_id=str(uuid.uuid4()),
            topic=topic,
            timestamp=datetime.now(UTC),
            payload=payload,
            source_agent=source_agent,
        )

        self._latest[topic] = event
        self._history.append(event)

        logger.info(
            "Event published — topic=%s source=%s id=%s",
            topic,
            source_agent,
            event.event_id,
        )

        # Fan-out to subscribers (fire-and-forget; errors are logged, not raised)
        for callback in self._subscribers.get(topic, []):
            try:
                await callback(event)
            except Exception:
                logger.exception(
                    "Subscriber %s failed for event %s",
                    callback.__qualname__,
                    event.event_id,
                )

        return event

    # -- subscribe ------------------------------------------------------------

    def subscribe(
        self,
        topic: str,
        callback: Callable[[Event], Coroutine],
    ) -> None:
        """Register an async callback for *topic*."""
        self._subscribers[topic].append(callback)
        logger.info(
            "Subscribed %s to topic=%s",
            callback.__qualname__,
            topic,
        )

    # -- query ----------------------------------------------------------------

    def get_latest(self, topic: str) -> Event | None:
        """Return the most recent event for *topic*, or None."""
        return self._latest.get(topic)

    def get_history(self, topic: str | None = None) -> list[Event]:
        """Return event history, optionally filtered by topic."""
        if topic is None:
            return list(self._history)
        return [e for e in self._history if e.topic == topic]


# ---------------------------------------------------------------------------
# Module-level singleton — import `bus` from anywhere
# ---------------------------------------------------------------------------

bus = EventBus()
