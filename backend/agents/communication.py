"""
Communication Agent for GeoAgentic.

Contract (from agent-contracts.md):
  Input:  approved action plan
  Output: delivery receipts per channel {recipient, channel, status, timestamp}

Key rule:
  Never blocks the critical path — dispatch proceeds even if a notification
  channel fails; log and retry failures separately.
"""

from __future__ import annotations

import asyncio
import logging

from bus.event_bus import COMMUNICATION_DISPATCHED, FUSION_COMPLETE, bus
from models.schemas import (
    CommunicationChannel,
    CommunicationRequest,
    CommunicationResponse,
    DataFreshness,
    DeliveryReceipt,
    Event,
)

logger = logging.getLogger("geoagentic.agent.communication")

AGENT_NAME = "communication_agent"


class CommunicationAgent:
    """Dispatches notifications based on the approved action plan."""

    def __init__(self) -> None:
        pass

    def register_on_bus(self) -> None:
        """Subscribe to FUSION_COMPLETE to auto-dispatch action plans."""
        bus.subscribe(FUSION_COMPLETE, self.handle_fusion_complete)
        logger.info(f"Registered on event bus for {FUSION_COMPLETE}")

    async def handle_fusion_complete(self, event: Event) -> None:
        """Auto-trigger dispatch when a new action plan is completed."""
        logger.info("Action plan received from bus; starting communication dispatch...")
        request = CommunicationRequest(action_plan=event.payload)
        await self.dispatch_plan(request)

    async def dispatch_plan(self, request: CommunicationRequest) -> CommunicationResponse:
        """
        Dispatches notifications concurrently.
        Mocked to force one channel to fail to prove non-blocking behaviour.
        """
        plan = request.action_plan
        hospital_id = "Unknown Hospital"
        ranked_hospitals = plan.get("ranked_hospitals", [])
        if ranked_hospitals and isinstance(ranked_hospitals, list) and len(ranked_hospitals) > 0:
            hospital_id = ranked_hospitals[0].get("hospital_id", "Unknown Hospital")

        # Define the channels we need to notify
        tasks = [
            self._notify_hospital(hospital_id),
            self._notify_driver(),
            self._notify_traffic_control(),
            self._notify_family(),  # This one is set up to fail
        ]

        # Use return_exceptions=True so a single failure doesn't crash the gather
        results = await asyncio.gather(*tasks, return_exceptions=True)

        receipts = []
        has_errors = False

        for result in results:
            if isinstance(result, Exception):
                # A hard exception was thrown by a mocked notification coroutine
                has_errors = True
                # In a real system, we'd queue this for retry or DLQ
                logger.error(f"Notification task failed with exception: {result}")
                # We can't easily extract the intended receipt from the generic Exception here,
                # but our mock functions return explicit DeliveryReceipts even on failure
                # (via try/except inside the mock), so this branch shouldn't actually hit in our setup.
            elif isinstance(result, DeliveryReceipt):
                receipts.append(result)
                if result.status == "failed":
                    has_errors = True
                    logger.error(
                        f"Dispatch failed for {result.recipient} via {result.channel.value}: {result.error_message}"
                    )
                else:
                    logger.info(
                        f"Dispatch successful for {result.recipient} via {result.channel.value}"
                    )

        overall_status = "completed_with_errors" if has_errors else "success"

        response = CommunicationResponse(
            receipts=receipts, overall_status=overall_status, data_freshness=DataFreshness.LIVE
        )

        # Publish the delivery receipts back to the bus
        await bus.publish(
            topic=COMMUNICATION_DISPATCHED,
            payload=response.model_dump(mode="json"),
            source_agent=AGENT_NAME,
        )

        return response

    async def _notify_hospital(self, hospital_id: str) -> DeliveryReceipt:
        """Mock notifying the destination hospital API."""
        await asyncio.sleep(0.1)  # mock network latency
        return DeliveryReceipt(
            recipient=f"{hospital_id} ER Desk",
            channel=CommunicationChannel.HOSPITAL_API,
            status="delivered",
        )

    async def _notify_driver(self) -> DeliveryReceipt:
        """Mock sending the route to the ambulance driver app."""
        await asyncio.sleep(0.05)
        return DeliveryReceipt(
            recipient="Ambulance Driver", channel=CommunicationChannel.APP_PUSH, status="delivered"
        )

    async def _notify_traffic_control(self) -> DeliveryReceipt:
        """Mock notifying city traffic control of the green corridor."""
        await asyncio.sleep(0.1)
        return DeliveryReceipt(
            recipient="City Traffic Control",
            channel=CommunicationChannel.TRAFFIC_CONTROL,
            status="delivered",
        )

    async def _notify_family(self) -> DeliveryReceipt:
        """Mock sending an SMS to the patient's emergency contact, FORCED TO FAIL."""
        await asyncio.sleep(0.2)
        # Mocking an SMS gateway timeout/failure
        return DeliveryReceipt(
            recipient="Patient's Emergency Contact",
            channel=CommunicationChannel.SMS,
            status="failed",
            error_message="SMS Gateway Timeout (504 Gateway Time-out)",
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

communication_agent = CommunicationAgent()
