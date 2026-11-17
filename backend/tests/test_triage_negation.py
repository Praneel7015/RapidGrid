"""Triage reads negation and flags text it cannot read (audit finding F10)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.triage import classify


@pytest.mark.parametrize("text, expected", [
    # The audit's examples.
    ("No chest pain, just a sprained ankle", "general"),
    ("Patient denies any weakness or slurred speech", "general"),
    # Negation does not leak across a clause.
    ("No chest pain, but sudden weakness on one side and slurred speech", "stroke"),
    ("He fell from a ladder, no head injury", "trauma"),
    # Phrases that state a negative finding still count.
    ("He is not breathing and has no pulse", "cardiac"),
    ("Not responding, no pulse, possible heart attack", "cardiac"),
    # Unchanged behaviour for plain reports.
    ("My father collapsed with severe chest pain", "cardiac"),
    ("Two-wheeler hit by a truck, rider has heavy bleeding from the leg", "trauma"),
])
def test_category_respects_negation(text, expected):
    assert classify(text).emergency_type.value == expected


def test_negated_mentions_are_reported_and_do_not_raise_severity():
    result = classify("No chest pain, no bleeding, he is just dizzy")
    assert result.emergency_type.value == "general"
    assert any("negated" in n and "chest pain" in n for n in result.notices)
    assert "heavy bleeding" not in result.red_flags


def test_not_breathing_still_raises_severity():
    assert classify("Not breathing").severity >= 0.75


@pytest.mark.parametrize("text, script", [
    ("ನನ್ನ ತಂದೆಗೆ ಎದೆ ನೋವು", "Kannada"),
    ("मेरे पिता को सीने में दर्द है", "Devanagari"),
])
def test_other_scripts_are_flagged_for_the_dispatcher(text, script):
    result = classify(text)
    assert any(script in n for n in result.notices)
    assert script in result.as_note()


def test_nothing_recognised_is_flagged():
    assert any("No emergency keywords" in n for n in classify("please come quickly").notices)


def test_notices_reach_the_stored_incident(monkeypatch):
    import asyncio
    from agents.accessibility import accessibility_agent
    from models.schemas import AccessibilityInputModality, AccessibilityRequest, Coordinate

    response = asyncio.run(accessibility_agent.process_input(AccessibilityRequest(
        raw_input="ನನ್ನ ತಂದೆಗೆ ಎದೆ ನೋವು", modality=AccessibilityInputModality.TEXT,
        location=Coordinate(lat=13.035, lng=77.597))))
    assert any("Kannada" in n for n in response.incident_report.triage_notices)
