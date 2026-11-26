"""Who sees which phone numbers for an incident (OWASP A01)."""

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
from routers import incident as incident_router

ORIGIN = {"Origin": "http://testserver"}
DIRECTORY = {
    "hotline_112": {"name": "112 line", "phone": "+91 90000 00112"},
    "dispatch": {"name": "Dispatch", "phone": "+91 90000 00100", "staff_pin": "111111"},
    "units": [
        {"unit_id": "AMB-01", "type": "Ambulance", "station": "Aster CMI Emergency Ambulance Hub",
         "crew": "Crew A", "phone": "+91 90000 00201", "staff_pin": "222222"},
        {"unit_id": "AMB-02", "type": "Ambulance", "station": "Manipal Ambulance Base Depot",
         "crew": "Crew B", "phone": "+91 90000 00202", "staff_pin": "333333"},
    ],
    "hospitals": [
        {"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)", "emergency_phone": "+91 80 0000 0701",
         "icu_phone": "+91 80 0000 0702", "staff_pin": "444444"},
    ],
}


@pytest.fixture
def call(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret")
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(DIRECTORY))
    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", asyncio.Lock())
    monkeypatch.setattr(incident_router, "run_pipeline", lambda *a, **k: asyncio.sleep(0))
    import citizens
    monkeypatch.setattr(citizens, "_registrations", {})
    contacts.reload()
    fleet.load()
    staff.reset_throttle()
    incident_router.ACTIVE_INCIDENTS.clear()
    owner = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    owner.post("/api/citizens/register", json={"name": "Asha", "phone": "+91 98000 00001", "pin": "246810"})
    incident_id = owner.post("/api/incidents", json={
        "citizen_text": "chest pain", "location": {"lat": 13.035, "lng": 77.597}}).json()["incident_id"]
    stored = incident_router.ACTIVE_INCIDENTS[-1]
    stored.update(status="dispatched", approved_hospital_id="blr-007")
    fleet.assign(incident_id, "Ambulance", stored["location"], prefer_station="Aster CMI Emergency Ambulance Hub")
    stored["assigned_unit"] = "AMB-01"
    yield incident_id, owner
    incident_router.ACTIVE_INCIDENTS.clear()


def staff_client(role, subject, pin):
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    assert c.post("/api/staff/session", json={"role": role, "subject": subject, "pin": pin}).status_code == 200
    return c


def contacts_for(c, incident_id):
    return c.get(f"/api/incidents/{incident_id}/contacts").json()


def test_a_stranger_gets_only_112(call):
    incident_id, _ = call
    seen = contacts_for(TestClient(app), incident_id)
    assert seen["hotline_112"]["phone"] == "+91 90000 00112"
    assert seen["dispatch"] is None and seen["unit"] is None and seen["hospital"] is None


def test_the_caller_gets_their_unit_and_dispatch_not_the_hospital(call):
    incident_id, owner = call
    seen = contacts_for(owner, incident_id)
    assert seen["unit"]["phone"] == "+91 90000 00201" and seen["dispatch"]["phone"] == "+91 90000 00100"
    assert seen["hospital"] is None


def test_dispatch_and_the_unit_on_the_call_get_the_er_and_icu(call):
    incident_id, _ = call
    for c in (staff_client("dispatcher", "dispatch", "111111"), staff_client("unit", "AMB-01", "222222")):
        seen = contacts_for(c, incident_id)
        assert seen["hospital"]["emergency_phone"] == "+91 80 0000 0701"
        assert seen["hospital"]["icu_phone"] == "+91 80 0000 0702"


def test_a_unit_not_on_the_call_gets_only_112(call):
    incident_id, _ = call
    seen = contacts_for(staff_client("unit", "AMB-02", "333333"), incident_id)
    assert seen["unit"] is None and seen["hospital"] is None and seen["dispatch"] is None


def test_a_unit_replaced_by_a_backup_loses_access_to_the_call(call):
    incident_id, _ = call
    stored = incident_router.ACTIVE_INCIDENTS[-1]
    stored["unit_assignments"] = {"primary": "AMB-01"}  # AMB-01 had claimed the call
    old = staff_client("unit", "AMB-01", "222222")
    assert contacts_for(old, incident_id)["hospital"] is not None
    # Monitoring hands the call to AMB-02, as the backup endpoint does.
    from routers import monitoring
    fleet.swap(incident_id, "AMB-02")
    stored["assigned_unit"] = "AMB-02"
    stored["unit_assignments"] = {k: v for k, v in stored["unit_assignments"].items() if v != "AMB-01"}
    seen = contacts_for(old, incident_id)
    assert seen["unit"] is None and seen["hospital"] is None and seen["dispatch"] is None
    new = contacts_for(staff_client("unit", "AMB-02", "333333"), incident_id)
    assert new["unit"]["unit_id"] == "AMB-02" and new["hospital"] is not None
    assert monitoring  # the endpoint's clean-up is covered in test_monitoring_agent


def test_the_receiving_hospital_gets_the_unit_and_dispatch(call):
    incident_id, _ = call
    seen = contacts_for(staff_client("hospital", "blr-007", "444444"), incident_id)
    assert seen["unit"]["unit_id"] == "AMB-01" and seen["dispatch"] is not None


def test_no_phone_number_is_in_any_incident_response(call):
    incident_id, _ = call
    # Legacy records carried phone fields; response redaction must drop them.
    stored = incident_router.ACTIVE_INCIDENTS[-1]
    stored.update(unit_phone="+91 90000 00201", hospital_phone="+91 80 0000 0701")
    stored.setdefault("citizen_view", {}).update(unit_phone="+91 90000 00201", hospital_phone="+91 80 0000 0701")
    c = staff_client("dispatcher", "dispatch", "111111")
    for path in ("/api/incidents/", f"/api/incidents/{incident_id}", "/api/driver/active", "/api/hospital/incoming/all"):
        r = c.get(path)
        # A refusal or an empty list would pass trivially: the call must be there.
        assert r.status_code == 200 and incident_id in r.text, path
        assert "90000 00201" not in r.text and "0000 0701" not in r.text


def test_with_staff_sign_in_off_a_stranger_still_gets_only_112(monkeypatch, call):
    # With no staff PINs, a visitor who knows the incident id is still a
    # stranger: staff numbers never go to an anonymous browser.
    incident_id, owner = call
    no_pins = {k: v for k, v in DIRECTORY.items()}
    no_pins["dispatch"] = {"name": "Dispatch", "phone": "+91 90000 00100"}
    no_pins["units"] = [{k: v for k, v in u.items() if k != "staff_pin"} for u in DIRECTORY["units"]]
    no_pins["hospitals"] = [{k: v for k, v in h.items() if k != "staff_pin"} for h in DIRECTORY["hospitals"]]
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps(no_pins))
    contacts.reload()
    assert not staff.enabled()
    stranger = contacts_for(TestClient(app), incident_id)
    assert stranger["dispatch"] is None and stranger["unit"] is None and stranger["hospital"] is None
    assert stranger["hotline_112"]["phone"] == "+91 90000 00112"


def test_the_caller_does_not_get_the_crews_name(call):
    incident_id, owner = call
    assert "crew" not in contacts_for(owner, incident_id)["unit"]
    staff_view = contacts_for(staff_client("dispatcher", "dispatch", "111111"), incident_id)
    assert staff_view["unit"]["crew"] == "Crew A"


def test_nobody_can_post_as_the_system(call):
    incident_id, owner = call
    as_system = owner.post(f"/api/chat/{incident_id}/send", json={
        "incident_id": incident_id, "sender_role": "system", "sender_name": "x", "message": "fake"})
    assert as_system.status_code == 422
    imitation = owner.post(f"/api/chat/{incident_id}/send", json={
        "incident_id": incident_id, "sender_role": "citizen", "sender_name": "Route monitoring", "message": "fake"})
    assert imitation.status_code == 200
    from routers import chat
    assert chat.manager.history(incident_id)[-1]["sender_name"] == "User"


def test_legacy_phone_fields_are_dropped_when_records_load(call, monkeypatch):
    incident_id, _ = call
    stored = incident_router.ACTIVE_INCIDENTS[-1]
    stored["unit_phone"] = "+91 98001 10004"
    stored.setdefault("citizen_view", {})["hospital_phone"] = "+91 80 4342 0107"
    incident_router.save_db()
    incident_router.load_db()
    reloaded = next(i for i in incident_router.ACTIVE_INCIDENTS if i["incident_id"] == incident_id)
    assert "unit_phone" not in reloaded and "hospital_phone" not in reloaded.get("citizen_view", {})
