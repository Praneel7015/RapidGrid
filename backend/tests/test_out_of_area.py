"""A vehicle that drives out of the service area is shown as out of area."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet
from routers import units
from tests.test_fleet_tracking import signed_in, with_pins  # noqa: F401  (fixture)


def test_dispatch_sees_why_a_unit_stopped_moving(with_pins):
    crew = signed_in("unit", "AMB-01", "222222")
    dispatch = signed_in("dispatcher", "dispatch", "111111")
    assert crew.post("/api/units/AMB-01/position", json={"lat": 13.04, "lng": 77.59}).status_code == 200

    def shown():
        return {u["unit_id"]: u for u in dispatch.get("/api/units").json()["units"]}["AMB-01"]

    assert shown()["out_of_area"] is False
    # Driven out to Hosur: the fix is not used, and dispatch is told why.
    assert crew.post("/api/units/AMB-01/position", json={"lat": 12.74, "lng": 77.83}).status_code == 422
    assert shown()["out_of_area"] is True
    assert fleet.get("AMB-01")["position"]["lat"] == 13.04  # still its last place inside
    # Back inside: the flag clears with the next good fix.
    fleet._units["AMB-01"]["position"]["at"] -= 5  # past the 1 s rate limit
    assert crew.post("/api/units/AMB-01/position", json={"lat": 13.05, "lng": 77.59}).status_code == 200
    assert shown()["out_of_area"] is False


def test_unknown_units_never_add_to_the_out_of_area_list():
    units._OUT_OF_AREA.clear()
    try:
        units.store_fix("NOPE-99", 12.0, 76.0)
    except Exception:
        pass
    assert "NOPE-99" not in units._OUT_OF_AREA
