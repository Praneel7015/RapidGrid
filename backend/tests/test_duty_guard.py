"""In the open demo (no staff PINs), a visitor may not take a unit off duty (OWASP A01)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
import security
import staff
from main import app


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret")
    monkeypatch.setattr(staff, "enabled", lambda: False)


def test_visitor_cannot_take_unit_off_duty():
    client = TestClient(app)
    unit_id = fleet.units()[0]["unit_id"]
    response = client.post(f"/api/units/{unit_id}/duty", json={"on_duty": False})
    assert response.status_code == 401


def test_visitor_can_still_put_unit_on_duty():
    client = TestClient(app)
    unit_id = fleet.units()[0]["unit_id"]
    response = client.post(f"/api/units/{unit_id}/duty", json={"on_duty": True})
    assert response.status_code == 200
