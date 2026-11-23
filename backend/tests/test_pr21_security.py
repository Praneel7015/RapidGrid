"""Fixes from CodeRabbit's review of the finals PR (#21): access and privacy."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import security
import storage
from main import app
from routers import incident as incident_router
from tests.test_security_review import ORIGIN, a_call, pins, signed_in  # noqa: F401  (fixture)


def test_a_hospital_sign_in_cannot_read_every_facility_through_the_all_id(pins):
    hospital = signed_in("hospital", "blr-007", "444444")
    assert hospital.get("/api/hospital/all/incoming").status_code == 403
    dispatch = signed_in("dispatcher", "dispatch", "11111111")
    assert dispatch.get("/api/hospital/all/incoming").status_code == 200


def test_patient_and_dispatch_queues_are_never_cached(pins):
    dispatch = signed_in("dispatcher", "dispatch", "11111111")
    for path in ("/api/hospital/incoming/all", "/api/hospital/blr-007/incoming", "/api/driver/active"):
        r = dispatch.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.headers.get("cache-control") == "no-store", path


def test_an_operator_cookie_needs_our_origin_for_state_changes(pins, monkeypatch):
    monkeypatch.setenv("RAPIDGRID_OPERATOR_TOKEN", "operator-token-for-tests-only-0123456789")
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    assert c.post("/api/session", json={"token": "operator-token-for-tests-only-0123456789"}).status_code == 200
    assert c.post("/api/city/clear").status_code == 200
    no_origin = TestClient(app, base_url="http://testserver", cookies=c.cookies)
    assert no_origin.post("/api/city/clear").status_code == 403
    # A script with the bearer token is not a browser riding a cookie.
    script = TestClient(app, base_url="http://testserver",
                        headers={"Authorization": "Bearer operator-token-for-tests-only-0123456789"})
    assert script.post("/api/city/clear").status_code == 200


def test_a_recommended_hospital_gets_no_numbers_before_dispatch_approves_it(pins):
    incident_id, stored, _ = a_call(status="awaiting_dispatcher_approval", unit=None)
    stored.pop("approved_hospital_id", None)
    stored["action_plan"] = {"recommended_hospital": {"hospital_id": "blr-007", "name": "Aster CMI Hospital (Hebbal)"}}
    hospital = signed_in("hospital", "blr-007", "444444")
    got = hospital.get(f"/api/incidents/{incident_id}/contacts").json()
    assert got["dispatch"] is None and got["unit"] is None
    stored["approved_hospital_id"] = "blr-007"
    assert hospital.get(f"/api/incidents/{incident_id}/contacts").json()["dispatch"] is not None


def test_a_number_typed_into_the_report_is_hidden_from_the_open_view(pins):
    anon = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    incident_id = anon.post("/api/incidents", json={
        "citizen_text": "Chest pain, call me on +91 98450-12345 or 080 2345 6789. Gate 3.",
        "location": {"lat": 13.035, "lng": 77.597}}).json()["incident_id"]
    text = anon.get(f"/api/incidents/{incident_id}").json()["citizen_text"]
    assert "98450" not in text and "2345 6789" not in text
    assert "Gate 3" in text and "[number hidden]" in text
    # The stored report keeps the caller's words for the role-checked views.
    assert "98450-12345" in incident_router.ACTIVE_INCIDENTS[-1]["citizen_text"]


def test_a_failed_write_does_not_use_up_a_report_permit(pins, monkeypatch):
    monkeypatch.setattr(incident_router, "REPORT_LIMIT", 1)
    anon = TestClient(app, base_url="http://testserver", headers=ORIGIN, raise_server_exceptions=False)
    body = {"citizen_text": "chest pain", "location": {"lat": 13.035, "lng": 77.597}}
    real = storage.write_incident

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "write_incident", broken)
    assert anon.post("/api/incidents", json=body).status_code == 500
    monkeypatch.setattr(storage, "write_incident", real)
    assert anon.post("/api/incidents", json=body).status_code == 202
    assert anon.post("/api/incidents", json=body).status_code == 429


def test_a_cookie_from_a_purged_account_does_not_open_the_same_number_registered_again(monkeypatch):
    import citizens
    import time

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret-at-least-32-characters")
    monkeypatch.setattr(citizens, "_registrations", {})
    monkeypatch.setattr(security, "_revoked", None)
    clock = [1_900_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    first = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    body = {"name": "Asha Rao", "phone": "+91 98000 00077", "pin": "246810"}
    assert first.post("/api/citizens/register", json=body).status_code == 200
    assert first.get("/api/citizens/session").json().get("signed_in") is True
    storage.clear_citizens(database=storage.data_path())
    clock[0] += 60
    second = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    assert second.post("/api/citizens/register", json={**body, "name": "New Owner"}).status_code == 200
    assert second.get("/api/citizens/session").json().get("signed_in") is True
    assert first.get("/api/citizens/session").json().get("signed_in") is not True


@pytest.mark.parametrize("text,hidden", [
    ("Call 98450 / 12345 now", True),
    ("Call 98450 - 12345 now", True),
    ("Since 23-11-2026 at 14:30", False),
    ("Pin 13.035 77.597 near gate", False),
])
def test_number_masking_catches_spaced_numbers_but_not_dates_or_coordinates(text, hidden):
    masked = incident_router._mask_numbers(text)
    assert ("[number hidden]" in masked) is hidden, masked


@pytest.mark.parametrize("text,hidden", [
    ("Call 98450.12345 now", True),
    ("Call +91.98450.12345", True),
    ("Pin 13.0358, 77.5970 at the gate", False),
])
def test_dotted_numbers_are_masked_but_coordinates_are_not(text, hidden):
    masked = incident_router._mask_numbers(text)
    assert ("[number hidden]" in masked) is hidden, masked


def test_an_old_cookie_fails_even_when_the_number_is_registered_again_in_the_same_second(monkeypatch):
    import citizens
    import time

    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret-at-least-32-characters")
    monkeypatch.setattr(citizens, "_registrations", {})
    monkeypatch.setattr(security, "_revoked", None)
    monkeypatch.setattr(time, "time", lambda: 1_900_000_000.25)  # one frozen second
    first = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    body = {"name": "Asha Rao", "phone": "+91 98000 00078", "pin": "246810"}
    assert first.post("/api/citizens/register", json=body).status_code == 200
    storage.clear_citizens(database=storage.data_path())
    second = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    assert second.post("/api/citizens/register", json={**body, "name": "New Owner"}).status_code == 200
    assert second.get("/api/citizens/session").json().get("signed_in") is True
    assert first.get("/api/citizens/session").json().get("signed_in") is not True


def test_forged_client_addresses_cannot_out_guess_the_account_cap(pins, monkeypatch):
    import staff as _staff

    _staff.reset_throttle()
    for i in range(_staff.ACCOUNT_HARD_FAILURES):
        _staff._fail("dispatcher:dispatch", f"forged-{i}")
    # A brand-new "client" (a new forged header) must still wait.
    assert _staff._wait("dispatcher:dispatch", "forged-new") > 0
    assert _staff._wait("dispatcher:dispatch", "forged-new") <= _staff.ACCOUNT_SPACING_S + 1
    _staff.reset_throttle()


def test_a_browser_that_signed_in_before_is_not_held_by_the_account_cap(pins):
    import staff as _staff

    _staff.reset_throttle()
    dispatcher = signed_in("dispatcher", "dispatch", "11111111")  # sets the known-device cookie
    for i in range(_staff.ACCOUNT_HARD_FAILURES):
        _staff._fail("dispatcher:dispatch", f"forged-{i}")
    stranger = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    assert stranger.post("/api/staff/session", json={"role": "dispatcher", "pin": "11111111"}).status_code == 429
    again = dispatcher.post("/api/staff/session", json={"role": "dispatcher", "pin": "11111111"})
    assert again.status_code == 200, again.text
    _staff.reset_throttle()


@pytest.mark.parametrize("text,hidden", [
    ("Since 12.05.2026 10.30 he has pain", False),
    ("Call 98450.12345 since 12.05.2026", True),
])
def test_dates_with_times_are_not_taken_for_phone_numbers(text, hidden):
    masked = incident_router._mask_numbers(text)
    assert ("[number hidden]" in masked) is hidden, masked
    if hidden:
        assert "12.05.2026" in masked
