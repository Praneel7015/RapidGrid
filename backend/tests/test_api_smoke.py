"""
API smoke tests for incident report, chat, and honest /health.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app  # noqa: E402
from routers import incident as incident_router  # noqa: E402
from routers import chat as chat_router  # noqa: E402


@pytest.fixture
def client():
    # Isolate in-memory stores between tests.
    incident_router.ACTIVE_INCIDENTS.clear()
    chat_router.manager.chat_history.clear()
    chat_router.manager.active_connections.clear()
    return TestClient(app)


def test_health_reports_status_and_routing(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in {"healthy", "degraded", "unhealthy"}
    assert "routing" in body
    assert "offline_graph_available" in body["routing"]
    assert "live_api_key_configured" in body["routing"]


def test_chat_send_and_history_have_message_ids(client):
    inc_id = "INC-test"
    send = client.post(
        f"/api/chat/{inc_id}/send",
        json={
            "incident_id": inc_id,
            "sender_role": "citizen",
            "sender_name": "Ananya",
            "message": "Need help near Jayanagar",
        },
    )
    assert send.status_code == 200
    msg = send.json()["message"]
    assert msg["id"]
    assert msg["timestamp_iso"]
    assert msg["message"] == "Need help near Jayanagar"

    hist = client.get(f"/api/chat/{inc_id}/messages")
    assert hist.status_code == 200
    messages = hist.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["id"] == msg["id"]


def test_chat_rejects_empty_message(client):
    res = client.post(
        "/api/chat/INC-empty/send",
        json={
            "incident_id": "INC-empty",
            "sender_role": "dispatcher",
            "sender_name": "Dispatch",
            "message": "   ",
        },
    )
    assert res.status_code == 400


def test_incident_report_accepts_voice_modality(client):
    with patch.object(incident_router, "run_pipeline", new=AsyncMock(return_value=None)):
        res = client.post(
            "/api/incidents",
            json={
                "citizen_text": "Chest pain and sweating",
                "citizen_name": "Ananya Sharma",
                "citizen_phone": "+91 98765 43210",
                "vehicle_required": "Ambulance",
                "location": {"lat": 12.9279, "lng": 77.5937},
                "input_modality": "voice",
            },
        )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "processing"
    assert data["incident_id"]
    poll = client.get(data["poll_url"])
    assert poll.status_code == 200
    body = poll.json()
    assert body["input_modality"] == "voice"
    assert body["status"] == "processing"


def test_pipeline_failure_marks_incident_failed():
    incident_router.ACTIVE_INCIDENTS.clear()
    incident_router.ACTIVE_INCIDENTS.append(
        {
            "incident_id": "INC-fail",
            "status": "processing",
            "citizen_phone": "+91 98765 43210",
        }
    )
    incident_router._mark_failed("INC-fail", RuntimeError("fusion exploded"))
    inc = incident_router.ACTIVE_INCIDENTS[0]
    assert inc["status"] == "failed"
    assert "fusion exploded" in inc["error"]
