"""Regression tests for bugs found by the bug-bounty review (26 Nov 2026)."""

from __future__ import annotations

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
    monkeypatch.setenv("RAPIDGRID_CONTACTS", '{"units": [{"unit_id": "AMB-01", "type": "Ambulance", '
                       '"station": "Hub", "tracker_key": "' + "a" * 32 + '"}]}')
    contacts.reload()
    try:
        assert contacts.unit_for_tracker("\ud800") is None
    finally:
        monkeypatch.delenv("RAPIDGRID_CONTACTS")
        contacts.reload()


def test_a_route_from_outside_the_service_area_is_refused():
    client = TestClient(app, base_url="http://localhost")
    response = client.post("/api/route/compute", json={
        "origin": {"lat": 0, "lng": 0}, "destination": {"lat": 12.97, "lng": 77.59}})
    assert response.status_code == 422
