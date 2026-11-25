"""In the open demo (no staff PINs), every visitor counts as signed in (the showcase
token gate is removed), so a visitor may take a unit off duty as well as on."""

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


def test_visitor_can_take_unit_off_duty_in_open_demo():
    client = TestClient(app)
    unit_id = fleet.units()[0]["unit_id"]
    response = client.post(f"/api/units/{unit_id}/duty", json={"on_duty": False})
    assert response.status_code == 200


def test_visitor_can_still_put_unit_on_duty():
    client = TestClient(app)
    unit_id = fleet.units()[0]["unit_id"]
    response = client.post(f"/api/units/{unit_id}/duty", json={"on_duty": True})
    assert response.status_code == 200
