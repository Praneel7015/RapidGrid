"""Regressions for GPS fix handling: the last real place after a call, the
order of fixes whose capture time is ahead of arrival, and freshness measured
from capture time rather than arrival."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet

HEBBAL = {"lat": 13.035, "lng": 77.597}
STATION = "Aster CMI Emergency Ambulance Hub"


def restart():
    fleet._units.clear()
    fleet._loaded = False
    fleet.load()


def test_the_last_real_place_survives_the_end_of_the_call_and_a_restart():
    now = time.time()
    fleet.assign("INC-1", "Ambulance", HEBBAL, prefer_station=STATION)
    fleet.record_position("AMB-01", 13.0400, 77.5950, source="gps", accuracy_m=8, at=now, captured_at=now)
    fleet.release("INC-1")
    assert fleet.get("AMB-01")["position"]["source"] == "gps"  # kept where the phone last put it

    restart()

    unit = fleet.get("AMB-01")
    assert (unit["position"]["lat"], unit["position"]["lng"]) == (13.0400, 77.5950)
    assert unit["position"]["source"] == "gps"
    # Restored as a place only: available, not on a call, not a real vehicle.
    assert unit["status"] == "available" and unit["incident_id"] is None and unit["gps_incident"] is None
    assert not fleet.real_vehicle(unit)


def test_a_fix_captured_ahead_of_arrival_does_not_block_an_older_fix():
    now = time.time()
    fleet.assign("INC-1", "Ambulance", HEBBAL, prefer_station=STATION)
    ahead = fleet.record_position("AMB-01", 13.0400, 77.5950, source="gps", at=now, captured_at=now + 20)
    assert ahead["captured_at"] == now  # clamped to arrival, never in the future

    # Captured before the future-stamped fix, but arriving after it: accepted.
    older = fleet.record_position("AMB-01", 13.0410, 77.5940, source="gps", at=now + 2, captured_at=now + 10)
    assert older["lat"] == 13.0410
    # A fix captured before the one already kept is still refused.
    try:
        fleet.record_position("AMB-01", 13.0420, 77.5930, source="gps", at=now + 4, captured_at=now - 5)
    except fleet.StaleFix:
        pass
    else:
        raise AssertionError("an out-of-order fix was accepted")


def test_freshness_is_measured_from_capture_not_arrival():
    now = time.time()
    fleet.assign("INC-1", "Ambulance", HEBBAL, prefer_station=STATION)
    # Arrives now, but the phone took it 30 s ago (buffered): not current.
    fleet.record_position("AMB-01", 13.0400, 77.5950, source="gps", at=now, captured_at=now - 30)
    assert fleet.fix_age_s(fleet.get("AMB-01")["position"]) >= 30
    assert fleet.phone_tracking("AMB-01") is False

    # A fresh capture arriving now is current.
    fleet.record_position("AMB-01", 13.0401, 77.5951, source="gps", at=now + 1, captured_at=now + 1)
    assert fleet.phone_tracking("AMB-01") is True
