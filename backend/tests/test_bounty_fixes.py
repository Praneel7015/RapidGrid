"""Regression tests for bugs found by the bug-bounty review (26 Nov 2026)."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts  # noqa: E402
from main import app  # noqa: E402
from routers.chat import safe_sender_name  # noqa: E402


@pytest.mark.parametrize("name", [
    "Dispatch decision",
    "Dispatch dеcision",          # Cyrillic е
    "Route monitoring​",     # zero-width space
    "Route Monitoring",
])
def test_chat_cannot_impersonate_the_system(name):
    assert safe_sender_name(name) == "User"


def test_ordinary_names_are_kept():
    assert safe_sender_name("Ruby") == "Ruby"


def test_a_lone_surrogate_tracker_key_is_a_wrong_key_not_an_error(monkeypatch):
    # Generated at run time: no secret-shaped literal in the source.
    key = secrets.token_hex(16)
    monkeypatch.setenv("RAPIDGRID_CONTACTS", '{"units": [{"unit_id": "AMB-01", "type": "Ambulance", '
                       '"station": "Hub", "tracker_key": "' + key + '"}]}')
    contacts.reload()
    try:
        assert contacts.unit_for_tracker(key) == "AMB-01"
        assert contacts.unit_for_tracker("\ud800") is None
    finally:
        monkeypatch.delenv("RAPIDGRID_CONTACTS")
        contacts.reload()


def test_a_route_from_outside_the_service_area_is_refused():
    client = TestClient(app, base_url="http://localhost")
    response = client.post("/api/route/compute", json={
        "origin": {"lat": 0, "lng": 0}, "destination": {"lat": 12.97, "lng": 77.59}})
    assert response.status_code == 422


def test_hosted_site_without_staff_pins_closes_stage_controls_and_duty_off(monkeypatch):
    import fleet
    import staff

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(staff, "enabled", lambda: False)
    client = TestClient(app, base_url="http://localhost")
    assert client.post("/api/monitoring/city-life", json={"seed": 1},
                       headers={"Origin": "http://localhost"}).status_code == 503
    unit_id = fleet.units()[0]["unit_id"]
    assert client.post(f"/api/units/{unit_id}/duty", json={"on_duty": False},
                       headers={"Origin": "http://localhost"}).status_code == 503
