"""An off-route alert reaches the caller's channel only once dispatch confirms it."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import simulator  # noqa: E402
from routers import monitoring  # noqa: E402
from routers import chat  # noqa: E402


def _capture(monkeypatch):
    posted = []

    async def fake_post(incident_id, text, sender="Route monitoring"):
        posted.append((incident_id, text))

    monkeypatch.setattr(chat, "post_system_message", fake_post)
    return posted


def test_a_stored_deviation_alert_is_not_posted_to_the_caller(monkeypatch):
    posted = _capture(monkeypatch)
    alert = {"trigger": "deviation", "unit_id": "AMB-01", "cause": {"text": "a closure ahead"}}
    asyncio.run(simulator._post_alerts([("INC-1", alert)]))
    assert posted == []


def test_other_alerts_are_still_posted_at_once(monkeypatch):
    posted = _capture(monkeypatch)
    alert = {"trigger": "slowing_ahead", "unit_id": "AMB-01", "cause": {"text": "congestion"}}
    asyncio.run(simulator._post_alerts([("INC-1", alert)]))
    assert len(posted) == 1 and posted[0][0] == "INC-1"


def test_dispatch_confirming_a_deviation_announces_it(monkeypatch):
    posted = _capture(monkeypatch)
    alert = {"trigger": "deviation", "unit_id": "AMB-01", "cause": {"text": "a closure ahead"}}
    asyncio.run(monitoring._announce_confirmed_deviation("INC-1", alert))
    assert len(posted) == 1 and "AMB-01" in posted[0][1]


def test_confirming_a_non_deviation_alert_adds_no_caller_post(monkeypatch):
    posted = _capture(monkeypatch)
    asyncio.run(monitoring._announce_confirmed_deviation("INC-1", {"trigger": "slowing_ahead"}))
    assert posted == []
