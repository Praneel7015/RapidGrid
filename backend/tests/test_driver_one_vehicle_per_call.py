"""One vehicle never holds both slots on a call, in either order."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routers import driver  # noqa: E402


def _incident(**extra):
    return {"incident_id": "INC-1", "status": "dispatched", "vehicle_required": "Fire Engine",
            "include_ambulance_backup": True, **extra}


def test_primary_cannot_also_be_the_backup(monkeypatch):
    monkeypatch.setattr(driver, "required_units", lambda inc: ["primary", "ambulance"])
    inc = _incident(assigned_unit="AMB-01", unit_assignments={"primary": "AMB-01"})
    with pytest.raises(HTTPException) as refused:
        driver._require_driver(inc, "AMB-01", "ambulance")
    assert refused.value.status_code == 409


def test_backup_cannot_also_be_the_primary(monkeypatch):
    monkeypatch.setattr(driver, "required_units", lambda inc: ["primary", "ambulance"])
    inc = _incident(unit_assignments={"ambulance": "AMB-02"})
    with pytest.raises(HTTPException) as refused:
        driver._require_driver(inc, "AMB-02", "primary")
    assert refused.value.status_code == 409
