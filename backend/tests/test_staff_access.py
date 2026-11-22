"""Staff sign-in and who may do what (OWASP A01, A07)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts
import security
import staff
from main import app

DIRECTORY = {
    "dispatch": {"name": "Dispatch", "phone": "+91 90000 00001", "staff_pin": "111111"},
    "units": [
        {"unit_id": "AMB-01", "type": "Ambulance", "station": "Aster CMI Emergency Ambulance Hub",
         "crew": "Crew A", "phone": "+91 90000 00002", "staff_pin": "222222"},
        {"unit_id": "AMB-02", "type": "Ambulance", "station": "Manipal Ambulance Base Depot",
         "crew": "Crew B", "phone": "+91 90000 00003", "staff_pin": "333333"},
    ],
    "hospitals": [
        {"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)", "emergency_phone": "+91 80 0000 0001",
         "icu_phone": "+91 80 0000 0002", "staff_pin": "444444"},
    ],
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret")
    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(security, "_pending_revocations", {})
    staff.reset_throttle()
    yield
    staff.reset_throttle()
    contacts.reload()


@pytest.fixture
def with_pins(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(DIRECTORY))
    contacts.reload()


ORIGIN = {"Origin": "http://testserver"}


def fresh():
    # Browsers send Origin on state-changing requests; staff actions need it.
    return TestClient(app, base_url="http://testserver", headers=ORIGIN)


def sign_in(client, role, subject, pin):
    return client.post("/api/staff/session", json={"role": role, "subject": subject, "pin": pin})


def test_without_staff_pins_staff_actions_stay_open(monkeypatch):
    monkeypatch.delenv("RAPIDGRID_CONTACTS", raising=False)
    monkeypatch.setenv("RAPIDGRID_CONTACTS_FILE", str(Path(contacts.__file__).with_name("contacts.example.json")))
    contacts.reload()
    assert not staff.enabled()
    assert fresh().post("/api/city/conditions", json={"hour": 9}).status_code == 200
    assert fresh().post("/api/staff/session", json={"role": "dispatcher", "pin": "111111"}).status_code == 404


def test_staff_actions_need_the_right_sign_in(with_pins):
    anonymous = fresh()
    assert anonymous.post("/api/city/conditions", json={"hour": 9}).status_code == 401

    dispatcher = fresh()
    assert sign_in(dispatcher, "dispatcher", "dispatch", "111111").status_code == 200
    assert dispatcher.post("/api/city/conditions", json={"hour": 9}).status_code == 200
    assert dispatcher.post("/api/driver/arrived_pickup", json={"incident_id": "x", "driver_id": "y"}).status_code == 403

    unit = fresh()
    assert sign_in(unit, "unit", "AMB-01", "222222").status_code == 200
    assert unit.post("/api/city/conditions", json={"hour": 9}).status_code == 403
    # Passes the role check; fails later only because the incident does not exist.
    assert unit.post("/api/driver/arrived_pickup", json={"incident_id": "x", "driver_id": "y"}).status_code not in (401, 403)


def test_a_hospital_can_change_only_its_own_capacity(with_pins):
    hospital = fresh()
    assert sign_in(hospital, "hospital", "blr-007", "444444").status_code == 200
    assert hospital.patch("/api/hospital/blr-001/resources", json={"beds_available": 3}).status_code == 403
    assert hospital.patch("/api/hospital/blr-007/resources", json={"beds_available": 3}).status_code == 200


def test_compute_endpoints_that_spend_quota_are_dispatcher_only(with_pins):
    body = {"origin": {"lat": 13.03, "lng": 77.59}, "destination": {"lat": 12.97, "lng": 77.59}}
    assert fresh().post("/api/route/compute", json=body).status_code == 401


def test_the_role_comes_from_the_session_not_the_request(with_pins):
    unit = fresh()
    sign_in(unit, "unit", "AMB-01", "222222")
    # Claiming another identity in the body changes nothing.
    assert unit.post("/api/city/close", json={"road": "Bellary Road", "label": "dispatcher"}).status_code == 403


def test_wrong_pins_slow_down_only_that_account(with_pins):
    attacker = fresh()
    codes = [sign_in(attacker, "dispatcher", "dispatch", "000000").status_code for _ in range(staff.FREE_FAILURES + 1)]
    assert codes[:staff.FREE_FAILURES] == [401] * staff.FREE_FAILURES
    assert codes[-1] == 401
    throttled = sign_in(attacker, "dispatcher", "dispatch", "000000")
    assert throttled.status_code == 429 and int(throttled.headers["retry-after"]) >= 1
    # Another account is unaffected: no global lock-out.
    assert sign_in(fresh(), "unit", "AMB-02", "333333").status_code == 200


def test_unknown_accounts_get_the_same_answer_as_wrong_pins(with_pins):
    unknown = sign_in(fresh(), "unit", "AMB-99", "222222")
    wrong = sign_in(fresh(), "unit", "AMB-01", "999999")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_sign_out_revokes_a_copied_staff_cookie(with_pins):
    dispatcher = fresh()
    sign_in(dispatcher, "dispatcher", "dispatch", "111111")
    copied = dispatcher.cookies.get(staff.COOKIE_NAME)
    assert dispatcher.delete("/api/staff/session").status_code == 200
    replay = fresh()
    replay.cookies.set(staff.COOKIE_NAME, copied)
    assert replay.post("/api/city/conditions", json={"hour": 9}).status_code == 401


def test_a_tampered_staff_cookie_is_refused(with_pins):
    unit = fresh()
    sign_in(unit, "unit", "AMB-01", "222222")
    parts = unit.cookies.get(staff.COOKIE_NAME).split(".")
    parts[2] = "dispatcher"  # escalate the role without re-signing
    forged = fresh()
    forged.cookies.set(staff.COOKIE_NAME, ".".join(parts))
    assert forged.post("/api/city/conditions", json={"hour": 9}).status_code == 401


def test_session_status_and_public_lists_hold_no_numbers_or_pins(with_pins):
    dispatcher = fresh()
    sign_in(dispatcher, "dispatcher", "dispatch", "111111")
    status = dispatcher.get("/api/staff/session").json()
    assert status == {"enabled": True, "role": "dispatcher", "subject": "dispatch"}
    public = json.dumps(contacts.public_units() + contacts.public_hospitals())
    for secret in ("+91", "111111", "222222", "444444"):
        assert secret not in public


def test_invalid_pins_in_the_directory_are_rejected(monkeypatch):
    bad = dict(DIRECTORY, dispatch={"name": "Dispatch", "staff_pin": "12"})
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(bad))
    contacts.reload()
    assert not contacts.staff_pins_configured()  # whole file refused, nothing half-loaded


def test_staff_actions_without_an_origin_header_are_refused(with_pins):
    dispatcher = TestClient(app, base_url="http://testserver")
    assert dispatcher.post("/api/staff/session", json={"role": "dispatcher", "pin": "111111"}).status_code == 200
    assert dispatcher.post("/api/city/conditions", json={"hour": 9}).status_code == 403
    assert dispatcher.post("/api/city/conditions", json={"hour": 9},
                           headers={"Origin": "https://evil.example"}).status_code == 403
    assert dispatcher.post("/api/city/conditions", json={"hour": 9}, headers=ORIGIN).status_code == 200
