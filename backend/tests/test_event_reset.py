"""Demo reset (dispatch) and the post-event personal-data purge (operator)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts
import fleet
import security
import storage
from agents import monitor
from main import app
from routers import chat
from routers import incident as incident_router

ORIGIN = {"Origin": "http://testserver"}


def _seed(monkeypatch):
    monkeypatch.setattr(incident_router, "_INCIDENTS_LOCK", asyncio.Lock())
    monkeypatch.setattr(incident_router, "run_pipeline", lambda *a, **k: asyncio.sleep(0))
    c = TestClient(app, base_url="http://testserver", headers=ORIGIN)
    c.post("/api/citizens/register", json={"name": "Asha", "phone": "+91 98000 00088", "pin": "246810"})
    incident_id = c.post("/api/incidents", json={"citizen_text": "help", "location": {"lat": 13.035, "lng": 77.597}}).json()["incident_id"]
    fleet.load()
    fleet.assign(incident_id, "Ambulance", {"lat": 13.035, "lng": 77.597})
    c.post(f"/api/chat/{incident_id}/send", json={"incident_id": incident_id, "sender_role": "citizen",
                                                   "sender_name": "Caller", "message": "hello"})
    monitor._log(incident_id, "AMB-01", "demo", "x")
    return c, incident_id


def test_reset_clears_the_demo_but_keeps_accounts(monkeypatch):
    c, incident_id = _seed(monkeypatch)
    from agents.city_state import city_state
    from agents.hospital_intelligence import hospital_agent
    from agents.local_router import local_router
    if not city_state.ready:
        city_state.attach(local_router)
    city_state.set_weather("Rain")
    hospital_agent.set_divert("blr-007", on_divert=True)
    monkeypatch.setattr(security, "authenticated", lambda request: True)  # open demo: operator only
    r = c.post("/api/monitoring/reset", json={})
    assert r.status_code == 200 and r.json()["incidents_cleared"] == 1
    assert incident_router.ACTIVE_INCIDENTS == []
    assert all(u["status"] == "available" for u in fleet.units())
    assert chat.manager.history(incident_id) == []
    assert storage.load_citizen(__import__("citizens").citizen_id("+919800000088"), database=storage.data_path())
    assert city_state.weather == "Clear" and not city_state.closed_edges
    assert not hospital_agent.diverted


def test_in_the_open_demo_a_visitor_cannot_reset(monkeypatch):
    c, _ = _seed(monkeypatch)
    assert c.post("/api/monitoring/reset", json={}).status_code == 401
    assert len(incident_router.ACTIVE_INCIDENTS) == 1


def test_reset_needs_dispatch_when_staff_sign_in_is_on(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_MODE", "demo")
    monkeypatch.setenv("RAPIDGRID_SECRET", "test-only-secret-at-least-32-characters")
    monkeypatch.setenv("RAPIDGRID_CONTACTS", json.dumps({"dispatch": {"name": "D", "staff_pin": "111111"}}))
    contacts.reload()
    try:
        anon = TestClient(app, base_url="http://testserver", headers=ORIGIN)
        assert anon.post("/api/monitoring/reset", json={}).status_code == 401
        evil = TestClient(app, base_url="http://testserver", headers={"Origin": "https://evil.example"})
        assert evil.post("/api/monitoring/reset", json={}).status_code in (401, 403)
    finally:
        monkeypatch.delenv("RAPIDGRID_CONTACTS")
        contacts.reload()


def test_purge_is_operator_only_and_deletes_accounts(monkeypatch):
    c, _ = _seed(monkeypatch)
    assert c.post("/api/session/purge", json={}).status_code == 401
    monkeypatch.setattr(security, "authenticated", lambda request: True)
    assert c.post("/api/session/purge", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    storage.save_collection("incidents", [{"citizen_phone": "+91 98000 00088"}], database=storage.data_path())
    r = c.post("/api/session/purge", json={})
    assert r.status_code == 200 and r.json()["accounts_deleted"] == 1
    assert incident_router.ACTIVE_INCIDENTS == []
    import sqlite3
    db = sqlite3.connect(storage.data_path())
    for table in ("incidents", "chat_messages", "citizens"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert db.execute("SELECT COUNT(*) FROM collections WHERE name = 'incidents' OR name LIKE 'chat:%'").fetchone()[0] == 0
    db.close()
    raw = Path(storage.data_path()).read_bytes()
    assert b"98000 00088" not in raw and b"Asha" not in raw
