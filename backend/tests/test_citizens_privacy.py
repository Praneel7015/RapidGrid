"""Citizen accounts and caller privacy (OWASP A01, A02, A07)."""

from __future__ import annotations

import asyncio
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
from routers import incident as incident_router

ORIGIN = {"Origin": "http://testserver"}
PINS = {
    "dispatch": {"name": "Dispatch", "staff_pin": "111111"},
    "units": [
        {"unit_id": "AMB-01", "type": "Ambulance", "station": "Aster CMI Emergency Ambulance Hub", "staff_pin": "222222"},
        {"unit_id": "AMB-02", "type": "Ambulance", "station": "Manipal Ambulance Base Depot", "staff_pin": "333333"},
    ],
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    import citizens
    monkeypatch.setattr(citizens, "_registrations", {})
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret")
    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(security, "_pending_revocations", {})
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", asyncio.Lock())
    monkeypatch.setattr(incident_router, "run_pipeline", lambda *a, **k: asyncio.sleep(0))
    incident_router.ACTIVE_INCIDENTS.clear()
    staff.reset_throttle()
    yield
    incident_router.ACTIVE_INCIDENTS.clear()
    staff.reset_throttle()


def client():
    return TestClient(app, base_url="http://testserver", headers=ORIGIN)


def register(c, phone="+91 98000 00001", pin="246810", name="Asha Rao"):
    return c.post("/api/citizens/register", json={"name": name, "phone": phone, "pin": pin})


def report(c, **extra):
    body = {"citizen_text": "Chest pain near Hebbal", "location": {"lat": 13.035, "lng": 77.597}, **extra}
    response = c.post("/api/incidents", json=body)
    assert response.status_code == 202, response.text
    return response.json()["incident_id"]


def test_register_then_sign_in_with_the_pin(tmp_path):
    first = client()
    assert register(first).status_code == 200
    assert first.get("/api/citizens/session").json()["name"] == "Asha Rao"
    again = client()
    assert again.post("/api/citizens/session", json={"phone": "9800000001", "pin": "246810"}).status_code == 200
    assert register(client()).status_code == 409  # one account per number


def test_wrong_pins_and_unknown_numbers_look_the_same_and_slow_down():
    register(client())
    wrong = client().post("/api/citizens/session", json={"phone": "+91 98000 00001", "pin": "000000"})
    unknown = client().post("/api/citizens/session", json={"phone": "+91 98000 09999", "pin": "246810"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    for _ in range(staff.FREE_FAILURES):
        client().post("/api/citizens/session", json={"phone": "+91 98000 00001", "pin": "000000"})
    assert client().post("/api/citizens/session", json={"phone": "+91 98000 00001", "pin": "246810"}).status_code == 429


def test_the_pin_is_never_stored_in_clear(tmp_path):
    assert register(client()).status_code == 200
    import sqlite3
    import storage
    with sqlite3.connect(storage.data_path()) as db:
        rows = [row[0] for row in db.execute("SELECT payload FROM citizens")]
    assert rows and all("246810" not in row for row in rows)
    assert all("pin_hash" in json.loads(row) for row in rows)


def test_account_requests_need_an_origin_header():
    bare = TestClient(app, base_url="http://testserver")
    assert bare.post("/api/citizens/register", json={"name": "A", "phone": "+91 98000 00002", "pin": "246810"}).status_code == 403


def test_a_signed_in_report_uses_the_account_and_is_listed_as_mine():
    mine = client()
    register(mine)
    incident_id = report(mine, citizen_name="Someone else", citizen_phone="+91 11111 11111")
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    assert stored["citizen_name"] == "Asha Rao" and stored["citizen_phone"] == "+919800000001"
    other = client()
    register(other, phone="+91 98000 00002")
    report(other)
    listed = mine.get("/api/citizens/me/incidents").json()["incidents"]
    assert [i["incident_id"] for i in listed] == [incident_id]
    assert client().get("/api/citizens/me/incidents").status_code == 401


def test_anonymous_reports_still_work():
    incident_id = report(client(), citizen_name="Walk-in", citizen_phone="+91 90000 12345")
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    assert stored["citizen_id"] is None and stored["citizen_name"] == "Walk-in"


def test_no_incident_response_carries_caller_details():
    incident_id = report(client(), citizen_name="Private Person", citizen_phone="+91 90000 77777")
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    hospital = {"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)", "location": {"lat": 13.0473, "lng": 77.5908}}
    stored.update(status="dispatched", approved_hospital_id="blr-007",
                  action_plan={"recommended_hospital": hospital, "all_hospitals": [hospital]},
                  citizen_view={"hospital_name": hospital["name"], "hospital_location": hospital["location"]})
    c = client()
    for text in (c.get("/api/incidents/").text, c.get(f"/api/incidents/{incident_id}").text,
                 c.get("/api/driver/active").text, c.get("/api/hospital/incoming/all").text):
        assert incident_id in text  # the call is listed ...
        assert "Private Person" not in text and "90000 77777" not in text  # ... without its caller


def test_caller_reveal_is_role_checked_and_recorded(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(PINS))
    contacts.reload()
    owner = client()
    register(owner)
    incident_id = report(owner)
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    stored["assigned_unit"] = "AMB-01"

    assert client().post(f"/api/incidents/{incident_id}/caller").status_code == 401
    other_unit = client()
    other_unit.post("/api/staff/session", json={"role": "unit", "subject": "AMB-02", "pin": "333333"})
    assert other_unit.post(f"/api/incidents/{incident_id}/caller").status_code == 403
    own_unit = client()
    own_unit.post("/api/staff/session", json={"role": "unit", "subject": "AMB-01", "pin": "222222"})
    assert own_unit.post(f"/api/incidents/{incident_id}/caller").json()["phone"] == "+919800000001"
    dispatcher = client()
    dispatcher.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "111111"})
    assert dispatcher.post(f"/api/incidents/{incident_id}/caller").json()["name"] == "Asha Rao"
    assert owner.post(f"/api/incidents/{incident_id}/caller").status_code == 200
    reveals = stored["caller_reveals"]
    assert [r["role"] for r in reveals] == ["unit", "dispatcher", "citizen"]
    assert "caller_reveals" not in client().get(f"/api/incidents/{incident_id}").json()


def test_the_unit_that_claimed_the_call_or_its_backup_can_reveal(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(PINS))
    contacts.reload()
    incident_id = report(client())
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    stored["assigned_unit"] = "AMB-01"
    stored["unit_assignments"] = {"primary": "AMB-01", "ambulance": "AMB-02"}  # AMB-02 is the backup
    backup = client()
    backup.post("/api/staff/session", json={"role": "unit", "subject": "AMB-02", "pin": "333333"})
    assert backup.post(f"/api/incidents/{incident_id}/caller").status_code == 200


def test_an_anonymous_report_never_carries_a_signed_in_account():
    shared_phone = client()
    register(shared_phone)
    incident_id = report(shared_phone, anonymous=True, citizen_name="Walk-in", citizen_phone="+91 90000 22222")
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    assert stored["citizen_id"] is None and stored["citizen_name"] == "Walk-in"


def test_registration_slow_down_is_per_client(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_CLIENT_IP_HEADER", "x-test-client")
    register(client())
    noisy = TestClient(app, base_url="http://testserver", headers={**ORIGIN, "x-test-client": "203.0.113.1"})
    for _ in range(8):
        register(noisy)  # repeatedly registering
    calm = TestClient(app, base_url="http://testserver", headers={**ORIGIN, "x-test-client": "203.0.113.2"})
    assert register(calm, phone="+91 98000 00009").status_code == 200


def test_accounts_survive_a_restart_without_a_configured_secret(monkeypatch):
    monkeypatch.delenv("RAPIDGRID_SECRET", raising=False)
    staff._file_secrets.clear()
    register(client())
    staff._file_secrets.clear()  # as after a restart: read back from the data directory
    assert client().post("/api/citizens/session", json={"phone": "+91 98000 00001", "pin": "246810"}).status_code == 200


def test_sign_out_revokes_the_citizen_cookie():
    me = client()
    register(me)
    copied = me.cookies.get("rapidgrid_citizen")
    assert me.delete("/api/citizens/session").status_code == 200
    replay = client()
    replay.cookies.set("rapidgrid_citizen", copied)
    assert replay.get("/api/citizens/me/incidents").status_code == 401


def test_cross_origin_sign_in_and_reveal_are_refused():
    evil = TestClient(app, base_url="http://testserver", headers={"Origin": "https://evil.example"})
    assert evil.post("/api/citizens/session", json={"phone": "+91 98000 00001", "pin": "246810"}).status_code == 403
    incident_id = report(client())
    assert evil.post(f"/api/incidents/{incident_id}/caller").status_code == 403


def test_with_staff_sign_in_off_anyone_may_reveal_and_it_is_recorded():
    # Local open demo (no staff PINs): documented in the README.
    incident_id = report(client(), citizen_name="Open Demo", citizen_phone="+91 90000 33333")
    assert client().post(f"/api/incidents/{incident_id}/caller").json()["name"] == "Open Demo"
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    assert stored["caller_reveals"][-1]["role"] == "open"


@pytest.mark.parametrize("typed", ["+91 98000 00001", "0091 98000 00001", "9800000001", "९८०००००००१"])
def test_phone_numbers_normalise_to_one_account(typed):
    import citizens
    assert citizens.normalise_phone(typed) == "+919800000001"


def test_registrations_from_one_client_are_not_rate_limited(monkeypatch):
    import citizens
    monkeypatch.setattr(citizens, "_registrations", {})
    c = client()
    codes = [register(c, phone=f"+91 98000 0010{i}").status_code for i in range(6)]
    assert codes == [200] * 6


def test_sign_out_needs_the_apps_own_origin():
    me = client()
    register(me)
    evil = TestClient(app, base_url="http://testserver", headers={"Origin": "https://evil.example"})
    evil.cookies.set("rapidgrid_citizen", me.cookies.get("rapidgrid_citizen"))
    assert evil.delete("/api/citizens/session").status_code == 403


def test_another_unit_cannot_claim_a_call_assigned_to_a_unit(monkeypatch):
    incident_id = report(client())
    stored = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    stored.update(status="dispatched", assigned_unit="AMB-01")
    claim = client().post("/api/driver/claim", json={"incident_id": incident_id, "driver_id": "AMB-02"})
    assert claim.status_code == 409
