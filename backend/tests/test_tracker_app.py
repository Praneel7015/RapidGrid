"""Background GPS from a tracker app (Traccar Client / OsmAnd protocol)."""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts
import fleet
import security
import staff
from main import app
from routers import tracker
from tests.test_fleet_tracking import PINS

KEY = "trk_AMB01_" + "x" * 24


@pytest.fixture
def trackers(monkeypatch):
    directory = copy.deepcopy(PINS)
    directory["units"][0]["tracker_key"] = KEY
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret-" + "y" * 32)
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(directory))
    contacts.reload()
    staff.reset_throttle()
    security.reset_throttle()
    tracker.reset_throttle()
    fleet.load()
    fleet.reset()
    yield
    tracker.reset_throttle()


def _app():
    return TestClient(app, base_url="http://testserver")  # an app: no Origin, no cookies


def test_osmand_query_fix_moves_the_unit(trackers):
    now = int(time.time())
    res = _app().post(f"/api/track?id={KEY}&lat=13.04&lon=77.59&timestamp={now}&accuracy=9&speed=10&bearing=90")
    assert res.status_code == 200 and res.content == b""
    pos = fleet.get("AMB-01")["position"]
    assert (pos["lat"], pos["lng"], pos["source"], pos["accuracy_m"], pos["heading"]) == (13.04, 77.59, "gps", 9.0, 90.0)
    assert pos["speed_mps"] == pytest.approx(5.144, abs=0.01)  # knots, as that protocol sends
    assert pos["captured_at"] == now


def test_traccar_client_json_fix_moves_the_unit(trackers):
    body = {"device_id": KEY, "location": {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "coords": {"latitude": 13.03, "longitude": 77.6, "accuracy": 12, "speed": 8.0, "heading": 180}}}
    assert _app().post("/api/track", json=body).status_code == 200
    pos = fleet.get("AMB-01")["position"]
    assert (pos["lat"], pos["lng"], pos["speed_mps"], pos["heading"]) == (13.03, 77.6, 8.0, 180.0)


def test_a_wrong_key_is_refused_and_throttled(trackers):
    client = _app()
    assert client.get("/api/track?id=AMB-01&lat=13.04&lon=77.59").status_code == 401  # a unit id is not a key
    for n in range(tracker._WRONG_KEYS_PER_CLIENT - 1):  # guessing: different keys
        assert client.get(f"/api/track?id=guess{n:04d}&lat=13.04&lon=77.59").status_code == 401
    assert client.get(f"/api/track?id={KEY}&lat=13.04&lon=77.59").status_code == 429
    assert fleet.get("AMB-01")["position"]["source"] == "station"
    # Its own bucket: operator and staff sign-in are not throttled by it.
    assert len(security._all_failures) == 0


def test_out_of_area_and_old_fixes_go_through_the_same_checks(trackers):
    client = _app()
    # Delhi: not used, but answered 200 so the app's queue does not stall on it.
    assert client.get(f"/api/track?id={KEY}&lat=28.6&lon=77.2").status_code == 200
    assert fleet.get("AMB-01")["position"]["source"] == "station"
    old = int(time.time()) - 600
    assert client.get(f"/api/track?id={KEY}&lat=13.04&lon=77.59&timestamp={old}").status_code == 200
    assert fleet.get("AMB-01")["position"]["source"] == "station"  # accepted, not used


def test_the_tracker_works_on_a_locked_hosted_site(trackers, monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "operator")
    monkeypatch.setenv("RAPIDGRID_OPERATOR_TOKEN", "z" * 40)
    client = _app()
    assert client.get("/api/units").status_code == 401  # the rest stays locked
    assert client.get(f"/api/track?id={KEY}&lat=13.04&lon=77.59").status_code == 200
    assert fleet.get("AMB-01")["position"]["source"] == "gps"


def test_no_tracker_keys_means_no_endpoint(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(PINS))
    contacts.reload()
    assert _app().get("/api/track?id=anything&lat=13.04&lon=77.59").status_code == 404


def test_tracker_keys_are_never_shown(trackers):
    assert KEY not in json.dumps(contacts.public_units())
    assert KEY not in _app().get("/api/staff/directory").text


def test_hostile_bodies_are_refused_cleanly(trackers):
    client = _app()
    nested = client.post("/api/track", content=b"[" * 5000, headers={"Content-Type": "application/json"})
    assert nested.status_code == 400
    big = client.post("/api/track", content=b"{" + b" " * 9000 + b"}", headers={"Content-Type": "application/json"})
    assert big.status_code == 413
    assert client.post("/api/track", json={"location": {"coords": {"latitude": 13.04}}}).status_code == 401


def test_one_tracker_resending_an_old_key_does_not_lock_out_others(trackers):
    client = _app()  # the same carrier address for every phone
    for _ in range(3 * tracker._WRONG_KEYS_PER_CLIENT):
        assert client.get("/api/track?id=trk_retired_key_0123456789abcdef&lat=13.04&lon=77.59").status_code == 401
    assert client.get(f"/api/track?id={KEY}&lat=13.04&lon=77.59").status_code == 200


def test_a_guessable_tracker_key_is_refused():
    with pytest.raises(ValueError):
        contacts._tracker_key("a" * 32)
    with pytest.raises(ValueError):
        contacts._tracker_key("abab" * 8)
    assert contacts._tracker_key("trk_9f2c41d07be3a8561c0d4e7f92ab31c6")
