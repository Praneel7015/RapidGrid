"""
Accessibility Agent for GeoAgentic.

Contract (from agent-contracts.md):
  Input:  raw citizen input via any modality (voice, sign-language video,
          text, SOS button, symbol tap).
  Output: {incident_report, citizen_accessibility_profile,
           input_modality, confidence}

This agent normalizes various input modalities (including mocked
sign-language parsing and unstructured text) into a standard
IncidentReport that the Decision Fusion Engine can consume.
"""

from __future__ import annotations

import logging

from agents.triage import classify
from bus.event_bus import INCIDENT_REPORTED, bus
from models.schemas import (
    AccessibilityInputModality,
    AccessibilityProfile,
    AccessibilityRequest,
    AccessibilityResponse,
    DataFreshness,
    EmergencyType,
    IncidentReport,
)

logger = logging.getLogger("geoagentic.agent.accessibility")

AGENT_NAME = "accessibility_agent"


class AccessibilityAgent:
    """Normalizes raw multimodal citizen inputs into structured incident reports."""

    def __init__(self) -> None:
        pass

    async def process_input(self, request: AccessibilityRequest) -> AccessibilityResponse:
        """
        Takes raw input from any modality and normalizes it.

        For this implementation, we simulate the ML pipeline that would normally
        convert video/sign language or unstructured voice/text into structured data.
        Both "text" and mocked "sign_language" inputs result in the exact same
        normalized IncidentReport shape downstream.
        """
        logger.info(f"AccessibilityAgent processing input via {request.modality.value}")

        raw_content = str(request.raw_input)

        # Classification is delegated to the triage model so the rules live in
        # one auditable place (see agents/triage.py).
        triage = classify(raw_content)
        etype = triage.emergency_type
        severity = triage.severity
        details = triage.as_note()
        confidence = triage.confidence

        # Special handling for SOS button — minimal input, maximum urgency
        if request.modality == AccessibilityInputModality.SOS_BUTTON:
            severity = 0.95
            details = "Immediate SOS trigger. No details provided."
            # The prompt says if the citizen can't complete the follow-up, it auto-submits
            # as "unspecified/general".
            etype = EmergencyType.GENERAL

        # 2. Build the normalized IncidentReport
        incident_report = IncidentReport(
            incident_type=etype,
            location=request.location,
            severity_estimate=severity,
            patient_count=1,
            extracted_details=details,
        )

        # 3. Handle Accessibility Profile
        # If the user didn't provide one, default to tracking the modality they used
        profile = request.known_citizen_profile or AccessibilityProfile(
            preferred_communication=request.modality,
            needs_sign_interpreter=(request.modality == AccessibilityInputModality.SIGN_LANGUAGE),
        )

        # Confidence drops slightly for non-text parsing in the mock
        if request.modality in [
            AccessibilityInputModality.VOICE,
            AccessibilityInputModality.SIGN_LANGUAGE,
        ]:
            confidence = 0.85

        response = AccessibilityResponse(
            incident_report=incident_report,
            citizen_accessibility_profile=profile,
            input_modality=request.modality,
            confidence=confidence,
            data_freshness=DataFreshness.LIVE,
        )

        logger.info(
            f"Normalized {request.modality.value} input to {etype.value} "
            f"(severity={severity:.2f}, confidence={confidence:.2f})"
        )

        # 4. Publish to the event bus for the Emergency Coordinator / Fusion Engine
        await bus.publish(
            topic=INCIDENT_REPORTED,
            payload=response.model_dump(mode="json"),
            source_agent=AGENT_NAME,
        )

        return response


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

accessibility_agent = AccessibilityAgent()
