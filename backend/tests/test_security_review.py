"""Fixes from the pre-freeze security review (OWASP A01, A02, A04, A05)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts
import fleet
import security
import staff
from main import app
from routers import chat
from routers import incident as incident_router

ORIGIN = {"Origin": "http://testserver"}
DIRECTORY = {
    "dispatch": {"name": "Dispatch", "phone": "+91 90000 00100", "staff_pin": "11111111"},
    "units": [
        {"unit_id": "AMB-01", "type": "Ambulance", "station": "Aster CMI Emergency Ambulance Hub", "staff_pin": "222222"},
        {"unit_id": "AMB-02", "type": "Ambulance", "station": "Manipal Ambulance Base Depot", "staff_pin": "333333"},
        {"unit_id": "POL-01", "type": "Police Cruiser", "station": "Hebbal Police Patrol Base", "staff_pin": "555555"},
    ],
    "hospitals": [{"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)", "staff_pin": "444444"}],
}


@pytest.fixture
def pins(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret-at-least-32-characters")
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(DIRECTORY))
    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", asyncio.Lock())
    monkeypatch.setattr(incident_router, "run_pipeline", lambda *a, **k: asyncio.sleep(0))
    contacts.reload()
    fleet.load()
    incident_router.ACTIVE_INCIDENTS.clear()
    yield
    incident_router.ACTIVE_INCIDENTS.clear()


def signed_in(role, subject, pin, ip=None):
    headers = dict(ORIGIN)
    if ip:
        headers["X-Forwarded-For"] = ip
    c = TestClient(app, base_url="http://testserver", headers=headers)
    r = c.post("/api/staff/session", json={"role": role, "subject": subject, "pin": pin})
    assert r.status_code == 200, r.text
    return c


def a_call(status="dispatched", unit="AMB-01", **extra):
    anon = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    incident_id = anon.post("/api/incidents", json={
        "citizen_text": "chest pain", "location": {"lat": 13.035, "lng": 77.597}}).json()["incident_id"]
    stored = incident_router.ACTIVE_INCIDENTS[-1]
    stored.update(status=status, approved_hospital_id="blr-007", **extra)
    if unit:
        fleet.assign(incident_id, fleet.get(unit)["type"], stored["location"], prefer_station=fleet.get(unit)["station"])
        stored["assigned_unit"] = unit
    return incident_id, stored, anon


def test_only_the_unit_on_the_call_can_mark_arrival(pins):
    incident_id, stored, _ = a_call(vehicle_required="Police Cruiser", unit="POL-01")
    stored["unit_statuses"] = {"primary": "dispatched"}
    other = signed_in("unit", "AMB-02", "333333")
    assert other.post(f"/api/incidents/{incident_id}/arrived?unit_type=primary").status_code == 403
    assert stored["status"] == "dispatched"


def test_a_unit_cannot_claim_a_slot_dispatch_did_not_give_it(pins):
    incident_id, stored, _ = a_call(unit=None)  # no unit was free at approval
    police = signed_in("unit", "POL-01", "555555")
    r = police.post("/api/driver/claim", json={"incident_id": incident_id, "driver_id": "POL-01", "unit_type": "primary"})
    assert r.status_code == 409
    stored["include_ambulance_backup"] = True
    r = police.post("/api/driver/claim", json={"incident_id": incident_id, "driver_id": "POL-01", "unit_type": "ambulance"})
    assert r.status_code in (403, 409)
    assert police.post(f"/api/incidents/{incident_id}/caller").status_code == 403


def test_nobody_can_post_as_dispatch_without_a_dispatch_sign_in(pins):
    incident_id, _stored, anon = a_call()
    anon.post(f"/api/chat/{incident_id}/send", json={
        "incident_id": incident_id, "sender_role": "dispatcher", "sender_name": "City Dispatch",
        "message": "Cancelled, no unit coming"})
    roles = [m["sender_role"] for m in chat.manager.history(incident_id)]
    assert roles == ["citizen"]
    unit = signed_in("unit", "AMB-01", "222222")
    unit.post(f"/api/chat/{incident_id}/send", json={
        "incident_id": incident_id, "sender_role": "dispatcher", "sender_name": "x", "message": "on my way"})
    assert chat.manager.history(incident_id)[-1]["sender_role"] == "driver"


def test_call_lists_need_the_right_sign_in(pins):
    incident_id, _stored, anon = a_call()
    a_call(unit="AMB-02")
    assert anon.get("/api/incidents/").status_code == 401
    assert anon.get("/api/driver/active").status_code == 401
    assert anon.get("/api/hospital/incoming/all").status_code == 401
    assert anon.get("/api/hospital/blr-007/incoming").status_code == 401
    assert signed_in("dispatcher", "dispatch", "11111111").get("/api/incidents/").status_code == 200
    mine = signed_in("unit", "AMB-01", "222222").get("/api/driver/active").json()["dispatches"]
    assert [d["incident_id"] for d in mine] == [incident_id]
    assert signed_in("hospital", "blr-007", "444444").get("/api/hospital/blr-007/incoming").status_code == 200


def test_a_stranger_cannot_lock_the_dispatcher_out(pins, monkeypatch):
    monkeypatch.setenv("RENDER", "true")  # client addresses come from the proxy header
    attacker = TestClient(app, base_url="http://testserver", headers={**ORIGIN})
    for _ in range(10):
        attacker.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "000000"},
                      headers={"X-Forwarded-For": "198.51.100.1"})
    assert attacker.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "11111111"},
                         headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 429
    # The real dispatcher, elsewhere, signs in at once.
    signed_in("dispatcher", "dispatch", "11111111", ip="192.0.2.50")


def test_many_addresses_get_little_once_an_account_is_under_attack(pins, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(staff, "ACCOUNT_FAILURES", 5)
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    for ip in ["198.51.100.0"] * 3 + ["198.51.100.1", "198.51.100.2"]:
        c.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "000000"},
               headers={"X-Forwarded-For": ip})
    # Past the ceiling, an address that has failed repeatedly waits.
    r = c.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "000001"},
               headers={"X-Forwarded-For": "198.51.100.0"})
    assert r.status_code == 429


def test_hosted_without_the_private_directory_staff_sign_in_is_locked(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("RAPIDGRID_CONTACTS", raising=False)
    monkeypatch.delenv("RAPIDGRID_CONTACTS_FILE", raising=False)
    contacts.reload()
    try:
        assert contacts.staff_pins_configured() is True
        assert contacts.directory().invalid is True
        anon = TestClient(app, base_url="http://testserver", headers=ORIGIN)
        assert anon.get("/api/incidents/").status_code in (401, 503)  # refused either way
    finally:
        monkeypatch.delenv("RENDER")
        contacts.reload()


def test_a_short_secret_is_not_used(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_SECRET", "short")
    assert staff._secret() != b"short"


def test_reports_are_rate_limited_per_client(pins, monkeypatch):
    monkeypatch.setattr(incident_router, "REPORT_LIMIT", 3)
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    body = {"citizen_text": "help", "location": {"lat": 13.035, "lng": 77.597}}
    codes = [c.post("/api/incidents", json=body).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]


def test_too_many_pending_reports_send_callers_to_112(pins, monkeypatch):
    monkeypatch.setattr(incident_router, "PENDING_CAP", 1)
    a_call(status="processing", unit=None)
    r = TestClient(app, base_url="http://testserver", headers=ORIGIN).post(
        "/api/incidents", json={"citizen_text": "help", "location": {"lat": 13.035, "lng": 77.597}})
    assert r.status_code == 429 and "112" in r.json()["detail"]


def test_the_audit_trail_names_the_signed_in_dispatcher(pins):
    incident_id, stored, _ = a_call(status="awaiting_dispatcher_approval", unit=None)
    d = signed_in("dispatcher", "dispatch", "11111111")
    d.post(f"/api/incidents/{incident_id}/reject?dispatcher_id=SOMEONE-ELSE")
    assert stored["dispatcher_id"] == "dispatch"


def test_longer_staff_pins_are_accepted():
    assert contacts._pin("12345678") == "12345678"
    with pytest.raises(ValueError):
        contacts._pin("12345")


def test_one_ambulance_cannot_hold_the_backup_slot_on_two_calls(pins):
    first, a, _ = a_call(vehicle_required="Police Cruiser", unit="POL-01", include_ambulance_backup=True)
    second, b, _ = a_call(vehicle_required="Police Cruiser", unit=None, include_ambulance_backup=True)
    amb = signed_in("unit", "AMB-02", "333333")
    claim = {"driver_id": "AMB-02", "unit_type": "ambulance"}
    assert amb.post("/api/driver/claim", json={"incident_id": first, **claim}).status_code == 200
    assert amb.post("/api/driver/claim", json={"incident_id": second, **claim}).status_code == 409
    assert amb.post(f"/api/incidents/{second}/caller").status_code == 403


def test_one_typo_never_locks_out_the_real_dispatcher(pins, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(staff, "ACCOUNT_FAILURES", 5)
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    for i in range(8):
        c.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "000000"},
               headers={"X-Forwarded-For": f"198.51.100.{i}"})
    me = {"X-Forwarded-For": "192.0.2.60"}
    assert c.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "1111111"},
                  headers=me).status_code == 401
    assert c.post("/api/staff/session", json={"role": "dispatcher", "subject": "dispatch", "pin": "11111111"},
                  headers=me).status_code == 200


def test_a_full_anonymous_queue_never_turns_away_a_signed_in_caller(pins, monkeypatch):
    monkeypatch.setattr(incident_router, "PENDING_CAP", 1)
    a_call(status="processing", unit=None)
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    c.post("/api/citizens/register", json={"name": "Asha", "phone": "+91 98000 00077", "pin": "246810"})
    r = c.post("/api/incidents", json={"citizen_text": "help", "location": {"lat": 13.035, "lng": 77.597}})
    assert r.status_code == 202


def test_trusted_hops_with_too_few_entries_use_the_connection(monkeypatch):
    from starlette.requests import Request

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RAPIDGRID_CLIENT_IP_HOPS", "3")
    scope = {"type": "http", "headers": [(b"x-forwarded-for", b"198.51.100.9")], "client": ("192.0.2.1", 1)}
    assert "198.51.100.9" not in security.client_key(Request(scope))


def test_a_unit_cannot_post_to_a_call_it_is_not_on(pins):
    incident_id, _stored, _ = a_call()
    police = signed_in("unit", "POL-01", "555555")
    r = police.post(f"/api/chat/{incident_id}/send", json={
        "incident_id": incident_id, "sender_role": "driver", "sender_name": "x", "message": "hi"})
    assert r.status_code == 403
    hospital = signed_in("hospital", "blr-007", "444444")
    r = hospital.post(f"/api/chat/{incident_id}/send", json={
        "incident_id": incident_id, "sender_role": "hospital", "sender_name": "ER", "message": "ready"})
    assert r.status_code == 200 and chat.manager.history(incident_id)[-1]["sender_role"] == "hospital"


def test_the_open_demo_lists_stay_open(monkeypatch):
    contacts.reload()  # the example directory: no PINs
    assert not staff.enabled()
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    for url in ("/api/incidents/", "/api/driver/active", "/api/hospital/incoming/all"):
        assert c.get(url).status_code == 200, url


def test_an_ambulance_holding_a_backup_slot_is_not_dispatched_elsewhere(pins):
    first, _a, _ = a_call(vehicle_required="Police Cruiser", unit="POL-01", include_ambulance_backup=True)
    amb = signed_in("unit", "AMB-02", "333333")
    assert amb.post("/api/driver/claim", json={"incident_id": first, "driver_id": "AMB-02",
                                               "unit_type": "ambulance"}).status_code == 200
    # A new medical call: the allocator must skip AMB-02 (it holds a slot).
    second, stored, _ = a_call(unit=None)
    stored["citizen_view"] = {}
    incident_router._assign_unit(stored)
    assert stored["assigned_unit"] != "AMB-02"
    # And AMB-02 no longer sees other calls' open backup slots.
    third, _c, _ = a_call(vehicle_required="Police Cruiser", unit=None, include_ambulance_backup=True)
    seen = [d["incident_id"] for d in amb.get("/api/driver/active").json()["dispatches"]]
    assert third not in seen and first in seen
