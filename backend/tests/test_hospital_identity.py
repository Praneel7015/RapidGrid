"""Hospital identity: OpenStreetMap entries are matched to curated hospitals by
name and position, never by name alone (audit finding F18)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.hospital_intelligence as hi
from agents.hospital_intelligence import HospitalIntelligenceAgent
from models.schemas import Coordinate, EmergencyType, HospitalRequest


class _Overpass:
    """Stands in for httpx.AsyncClient and returns the given OSM elements."""

    def __init__(self, elements):
        self.elements = elements

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        elements = self.elements

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"elements": elements}

        return Response()


def node(osm_id, name, lat, lon):
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": {"name": name}}


def catalogue(monkeypatch, elements):
    monkeypatch.setattr(hi.httpx, "AsyncClient", _Overpass(elements))
    agent = HospitalIntelligenceAgent()
    return agent, asyncio.run(agent._fetch_live_hospitals(12.97, 77.70))


def by_name(hospitals):
    return {h["name"]: h for h in hospitals}


BRANCHES = ("Manipal Hospital (Whitefield)", "Manipal Hospital (Old Airport Road)",
            "Manipal Hospital (Sarjapur Road)")


def test_a_bare_chain_name_does_not_remove_other_branches(monkeypatch):
    # The audit's case: one OSM node "Manipal Hospital" near Whitefield, about
    # 3 km from the branch's mapped position. Before, it removed all three
    # branches by substring and took Old Airport Road's profile and position.
    _, hospitals = catalogue(monkeypatch, [node(9003, "Manipal Hospital", 12.9700, 77.7500)])
    names = by_name(hospitals)
    for branch in BRANCHES:
        assert branch in names, branch
    # Too far from any branch to be one: kept where it is, unverified.
    stray = names["Manipal Hospital"]
    assert stray["curated_id"] is None and stray["capability_provenance"] == "simulated"
    assert (stray["lat"], stray["lng"]) == (12.97, 77.75)


def test_an_osm_entry_at_a_branch_is_that_branch_not_a_duplicate(monkeypatch):
    # 60 m from the Whitefield branch's mapped coordinate.
    _, hospitals = catalogue(monkeypatch, [node(9004, "Manipal Hospital", 12.9885, 77.7290)])
    names = by_name(hospitals)
    assert "Manipal Hospital" not in names
    for branch in BRANCHES:
        assert branch in names, branch
    assert names["Manipal Hospital (Whitefield)"]["id"] == "blr-015"


def test_every_curated_hospital_keeps_its_own_id_and_position(monkeypatch):
    elements = [node(9100 + i, "Manipal Hospital", 12.9 + i * 0.01, 77.55) for i in range(5)]
    agent, hospitals = catalogue(monkeypatch, elements)
    curated = agent._load_capabilities()
    present = {h["curated_id"]: h for h in hospitals if h["curated_id"]}
    assert set(present) == set(curated)
    for curated_id, entry in curated.items():
        assert (present[curated_id]["lat"], present[curated_id]["lng"]) == (entry["lat"], entry["lng"])


def test_a_far_namesake_is_its_own_unclassified_facility(monkeypatch):
    # Shares a word with Manipal, but is kilometres from every branch.
    _, hospitals = catalogue(monkeypatch, [node(9200, "Sri Manipal Nursing Home", 13.10, 77.50)])
    entry = by_name(hospitals)["Sri Manipal Nursing Home"]
    assert entry["curated_id"] is None
    assert entry["capability_provenance"] == "simulated"
    assert (entry["lat"], entry["lng"]) == (13.10, 77.50)  # never relocated


def test_an_unrelated_clinic_next_to_a_curated_hospital_is_not_merged(monkeypatch):
    # 100 m from Aster CMI, but no name in common.
    _, hospitals = catalogue(monkeypatch, [node(9300, "Lakshmi Clinic", 13.0480, 77.5910)])
    entry = by_name(hospitals)["Lakshmi Clinic"]
    assert entry["curated_id"] is None
    assert "Aster CMI Hospital (Hebbal)" in by_name(hospitals)


def test_whitefield_stroke_still_reaches_the_whitefield_branch(monkeypatch):
    monkeypatch.setattr(hi.httpx, "AsyncClient", _Overpass([node(9003, "Manipal Hospital", 12.9700, 77.7500)]))
    agent = HospitalIntelligenceAgent()
    result = asyncio.run(agent.rank_hospitals(HospitalRequest(
        incident_location=Coordinate(lat=12.975, lng=77.745), emergency_type=EmergencyType.STROKE)))
    names = [entry.name for entry in result.ranked_hospitals]
    assert "Manipal Hospital (Whitefield)" in names[:3]


def test_overpass_failure_still_gives_the_full_curated_catalogue(monkeypatch):
    class Down(_Overpass):
        async def get(self, *args, **kwargs):
            raise RuntimeError("overpass unavailable")

    monkeypatch.setattr(hi.httpx, "AsyncClient", Down([]))
    agent = HospitalIntelligenceAgent()
    hospitals = asyncio.run(agent._fetch_live_hospitals(12.97, 77.70))
    assert {h["curated_id"] for h in hospitals} == set(agent._load_capabilities())
