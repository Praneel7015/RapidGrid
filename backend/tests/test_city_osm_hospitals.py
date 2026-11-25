"""The saved Bangalore hospital list serves the map (public, read-only)."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app  # noqa: E402


def test_osm_hospitals_are_served_with_named_areas():
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/api/city/osm-hospitals")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["hospitals"]) > 0
    areas = {a["id"] for a in body["areas"]}
    assert {"whitefield", "mathikere", "venue"} <= areas
    tagged = {h["area"] for h in body["hospitals"]}
    assert "whitefield" in tagged and "mathikere" in tagged
    for h in body["hospitals"]:
        assert -90 <= h["lat"] <= 90 and -180 <= h["lng"] <= 180
        assert h["name"]
