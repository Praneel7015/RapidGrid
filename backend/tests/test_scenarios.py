"""
Behavioural tests for the current architecture.

The previous file in this slot tested a graph router that took string node ids
("N1" -> "N5"). That design no longer exists - routing is coordinate-based
across three tiers - so those tests could not pass and were not telling anyone
anything. These replace them.

Run with:
    cd backend && ./venv/Scripts/python.exe -m pytest tests/ -q

Nothing here needs the network: the offline road graph, the curated hospital
table and the triage model are all local. Tests that would need a live API are
skipped rather than silently passing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.city_state import CityState  # noqa: E402
from agents.clinical_outcome import compare, estimate  # noqa: E402
from agents.local_router import local_router  # noqa: E402
from agents.triage import classify  # noqa: E402
from models.schemas import EmergencyType  # noqa: E402

HEBBAL = (13.0350, 77.5970)
JAYADEVA = (12.9290, 77.5878)


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


class TestTriage:
    def test_bleeding_trauma_is_not_general(self):
        """The bug that sent a haemorrhaging patient to a general hospital."""
        r = classify("Two-wheeler hit by a truck, rider has heavy bleeding from the leg")
        assert r.emergency_type is EmergencyType.TRAUMA
        assert r.severity >= 0.8

    def test_cardiac_arrest_is_critical(self):
        r = classify("My father collapsed with severe chest pain and is barely conscious")
        assert r.emergency_type is EmergencyType.CARDIAC
        assert r.severity >= 0.9

    def test_stroke_recognised(self):
        r = classify("Elderly woman sudden slurred speech and right side weakness")
        assert r.emergency_type is EmergencyType.STROKE

    def test_de_escalators_lower_severity(self):
        minor = classify("Minor cut on hand, patient is stable and walking")
        major = classify("Deep wound, heavy bleeding, patient unresponsive")
        assert minor.severity < major.severity

    def test_thin_evidence_reports_low_confidence(self):
        """Over-confidence on one weak term is how these systems mislead."""
        assert classify("Someone fainted at the bus stop").confidence < 0.5

    def test_empty_input_does_not_crash(self):
        r = classify("")
        assert r.emergency_type is EmergencyType.GENERAL
        assert r.confidence < 0.5


# ---------------------------------------------------------------------------
# Offline routing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def router():
    if not local_router.load():
        pytest.skip("road graph not built - run scripts/build_road_graph.py")
    return local_router


class TestOfflineRouter:
    def test_finds_a_real_path(self, router):
        p = router.find_path(HEBBAL, JAYADEVA)
        assert p is not None
        assert p["distance_m"] > 5000
        assert len(p["coordinates"]) > 50
        assert any("Bellary" in n for n in p["roads"]), "expected a known corridor"

    def test_route_is_geographically_sane(self, router):
        """Road distance must exceed straight-line but not absurdly."""
        from agents.local_router import haversine

        p = router.find_path(HEBBAL, JAYADEVA)
        straight = haversine(*HEBBAL, *JAYADEVA)
        assert straight < p["distance_m"] < straight * 2.5

    def test_blocking_a_road_changes_the_route(self, router):
        base = router.find_path(HEBBAL, JAYADEVA)
        victims = [
            e for e in base["edge_ids"] if "Bellary" in router.road_names[router.edge_name[e]]
        ]
        assert victims, "fixture route no longer uses Bellary Road"
        router.block_edges(victims)
        try:
            rerouted = router.find_path(HEBBAL, JAYADEVA)
            assert rerouted is not None
            assert set(rerouted["edge_ids"]) != set(base["edge_ids"])
            assert rerouted["travel_time_s"] > base["travel_time_s"]
        finally:
            router.unblock_all()

    def test_unreachable_coordinates_return_none(self, router):
        # Middle of the Arabian Sea - far outside the graph's bbox.
        assert router.find_path((15.0, 70.0), (15.1, 70.1)) is None


# ---------------------------------------------------------------------------
# Causal city model
# ---------------------------------------------------------------------------


class TestCityState:
    @pytest.fixture
    def city(self, router):
        c = CityState()
        assert c.attach(router)
        yield c
        c.clear()
        router.unblock_all()

    def test_peak_hour_is_slower_than_night(self, city, router):
        city.set_hour(3)
        night = router.find_path(HEBBAL, JAYADEVA)["travel_time_s"]
        city.set_hour(18)
        peak = router.find_path(HEBBAL, JAYADEVA)["travel_time_s"]
        assert peak > night * 1.2

    def test_rain_slows_travel(self, city, router):
        city.set_hour(18)
        city.set_weather("Clear")
        clear = router.find_path(HEBBAL, JAYADEVA)["travel_time_s"]
        city.set_weather("Rain")
        wet = router.find_path(HEBBAL, JAYADEVA)["travel_time_s"]
        assert wet > clear

    def test_closure_displaces_traffic_onto_neighbours(self, city, router):
        """
        The load-bearing claim: a route that never used the closed road still
        gets slower, because displaced traffic congests the streets around it.
        """
        city.set_hour(18)
        parallel = ((13.0420, 77.5860), (12.9980, 77.5710))
        before = router.find_path(*parallel)["travel_time_s"]

        primary = router.find_path(HEBBAL, JAYADEVA)
        victims = [
            e for e in primary["edge_ids"] if "Bellary" in router.road_names[router.edge_name[e]]
        ]
        result = city.close_road(victims, "Bellary Road")
        assert result["displaced"] > 0

        after = router.find_path(*parallel)["travel_time_s"]
        assert after > before, "displacement did not reach the parallel corridor"

    def test_distant_route_is_unaffected(self, city, router):
        """Displacement must be local, or the model is just a global slowdown."""
        city.set_hour(18)
        far = ((12.9698, 77.7180), (12.9352, 77.6245))
        before = router.find_path(*far)["travel_time_s"]

        primary = router.find_path(HEBBAL, JAYADEVA)
        victims = [
            e for e in primary["edge_ids"] if "Bellary" in router.road_names[router.edge_name[e]]
        ]
        city.close_road(victims, "Bellary Road")
        after = router.find_path(*far)["travel_time_s"]
        assert after == pytest.approx(before, rel=0.02)

    def test_clear_restores_baseline(self, city, router):
        city.set_hour(18)
        baseline = router.find_path(HEBBAL, JAYADEVA)["travel_time_s"]
        primary = router.find_path(HEBBAL, JAYADEVA)
        city.close_road(primary["edge_ids"][:30], "test closure")
        city.clear()
        assert router.find_path(HEBBAL, JAYADEVA)["travel_time_s"] == pytest.approx(
            baseline, rel=0.01
        )

    def test_explain_names_the_mechanism(self, city, router):
        city.set_hour(18)
        primary = router.find_path(HEBBAL, JAYADEVA)
        city.close_road(primary["edge_ids"][:20], "test")
        overlay = city.congestion_overlay(limit=1)
        assert overlay
        chain = city.explain(overlay[0]["edge_id"])
        assert chain["provenance"] == "modelled"
        assert any(c["cause"] == "displacement" for c in chain["causes"])


# ---------------------------------------------------------------------------
# Clinical outcome
# ---------------------------------------------------------------------------


class TestClinicalOutcome:
    def test_transfer_penalty_dominates_a_short_drive(self):
        """Driving further can still reach treatment sooner."""
        near = {"name": "Local general", "capabilities": {"cardiology": 0.42}}
        pci = {"name": "PCI centre", "capabilities": {"cardiology": 1.00}}
        r = compare("cardiac", pci, near, 8 * 60, 16 * 60, 5 * 60)
        assert r is not None
        assert r["chosen"]["requires_transfer"] is False
        assert r["alternative"]["requires_transfer"] is True
        assert r["chosen"]["total_minutes"] < r["alternative"]["total_minutes"]
        assert r["outcome_delta_minutes"] > 30

    def test_capable_hospital_meets_the_target(self):
        pci = {"name": "PCI centre", "capabilities": {"cardiology": 1.00}}
        e = estimate("cardiac", pci, 8 * 60, 16 * 60)
        assert e.within_target
        assert e.target_minutes == 90

    def test_unknown_pathway_returns_none(self):
        """Better to say nothing than invent a clinical target."""
        assert estimate("general", {"name": "x", "capabilities": {}}, 60, 60) is None

    def test_totals_are_internally_consistent(self):
        h = {"name": "x", "capabilities": {"neurology": 0.9}}
        e = estimate("stroke", h, 5 * 60, 10 * 60)
        parts = (
            e.activation_minutes
            + e.to_patient_minutes
            + e.on_scene_minutes
            + e.to_hospital_minutes
            + e.in_hospital_minutes
            + e.transfer_minutes
        )
        assert e.total_minutes == pytest.approx(parts, abs=0.2)


# ---------------------------------------------------------------------------
# Hospital capability data
# ---------------------------------------------------------------------------


class TestHospitalData:
    @pytest.fixture(scope="class")
    @classmethod
    def profiles(cls):
        import json

        p = Path(__file__).resolve().parent.parent / "data" / "hospital_capabilities.json"
        if not p.exists():
            pytest.skip("capability table missing")
        return json.loads(p.read_text(encoding="utf-8"))["hospitals"]

    def test_every_profile_is_complete(self, profiles):
        required = {
            "name",
            "type",
            "emergency_receiving",
            "beds_total",
            "icu_total",
            "specialties",
            "capabilities",
        }
        for key, entry in profiles.items():
            assert required <= set(entry), f"{key} is missing fields"

    def test_capability_scores_are_normalised(self, profiles):
        for key, entry in profiles.items():
            for factor, score in entry["capabilities"].items():
                assert 0.0 <= score <= 1.0, f"{key}.{factor} out of range"

    def test_non_emergency_facilities_are_excluded(self, profiles):
        """An oncology centre must never be a valid cardiac destination."""
        for key in ("blr-021", "blr-025", "blr-014"):  # Kidwai, HCG, Cloudnine
            assert profiles[key]["emergency_receiving"] is False

    def test_specialty_centres_score_highest_in_their_specialty(self, profiles):
        assert profiles["blr-019"]["capabilities"]["cardiology"] >= 0.95  # Jayadeva
        assert profiles["blr-010"]["capabilities"]["neurology"] >= 0.95  # NIMHANS
        assert profiles["blr-024"]["capabilities"]["trauma_centre"] >= 0.9  # Sanjay Gandhi


# ---------------------------------------------------------------------------
# Hospital coordinates
# ---------------------------------------------------------------------------


class TestHospitalCoordinates:
    """
    Guards the bug where a patient with chest pain at MSRIT was told Ramaiah
    Memorial was 10 minutes away when it is 595 m down the road. The seeded
    coordinate was 2.4 km out, in Malleswaram - which is also why selecting
    Ramaiah surfaced a hospital from that neighbourhood instead.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def profiles(cls):
        import json

        p = Path(__file__).resolve().parent.parent / "data" / "hospital_capabilities.json"
        if not p.exists():
            pytest.skip("capability table missing")
        return json.loads(p.read_text(encoding="utf-8"))["hospitals"]

    def test_every_profile_carries_a_coordinate(self, profiles):
        for key, entry in profiles.items():
            assert "lat" in entry and "lng" in entry, f"{key} has no coordinate"

    def test_coordinates_are_inside_the_bengaluru_bbox(self, profiles):
        for key, entry in profiles.items():
            assert 12.70 <= entry["lat"] <= 13.20, f"{key} latitude outside Bengaluru"
            assert 77.35 <= entry["lng"] <= 77.85, f"{key} longitude outside Bengaluru"

    def test_coordinate_provenance_is_recorded(self, profiles):
        """Every coordinate says where it came from, so drift is auditable."""
        allowed = {"openstreetmap", "manual-reviewed", "manual-unverified"}
        for key, entry in profiles.items():
            assert entry.get("coord_source") in allowed, f"{key} has no coord_source"

    def test_ramaiah_is_next_to_msrit(self, profiles):
        """The reported bug, as an assertion."""
        from agents.local_router import haversine

        msrit = (13.0297, 77.5645)
        r = profiles["blr-009"]
        d = haversine(msrit[0], msrit[1], r["lat"], r["lng"])
        assert d < 1200, f"Ramaiah is {d:.0f} m from MSRIT - it is under 700 m"

    def test_no_two_hospitals_share_a_location(self, profiles):
        """Duplicate coordinates mean one of them is wrong."""
        from agents.local_router import haversine

        items = list(profiles.items())
        for i, (k1, a) in enumerate(items):
            for k2, b in items[i + 1 :]:
                d = haversine(a["lat"], a["lng"], b["lat"], b["lng"])
                assert d > 50, f"{k1} and {k2} are {d:.0f} m apart"


class TestRoutingRegression:
    def test_cardiac_at_msrit_picks_the_hospital_next_door(self, monkeypatch):
        """
        End to end: the nearest capable hospital should win when it is 600 m
        away and every alternative is kilometres off.
        """
        import asyncio

        import httpx

        from agents.hospital_intelligence import hospital_agent
        from agents.local_router import local_router
        from models.schemas import Coordinate, EmergencyType, HospitalRequest

        if not local_router.load():
            pytest.skip("road graph not built")

        # No network: rank against the seed list, with a private catalogue cache.
        async def _no_network(*_args, **_kwargs):
            raise httpx.ConnectError("network disabled in tests")

        monkeypatch.setattr(httpx.AsyncClient, "get", _no_network)
        monkeypatch.setattr(hospital_agent, "_fetch_cache", {})
        monkeypatch.setattr(hospital_agent, "_hospitals", [])

        res = asyncio.run(
            hospital_agent.rank_hospitals(
                HospitalRequest(
                    incident_location=Coordinate(lat=13.0297, lng=77.5645),
                    emergency_type=EmergencyType.CARDIAC,
                )
            )
        )
        top = res.ranked_hospitals[0]
        assert "Ramaiah" in top.name, f"expected Ramaiah, got {top.name}"
        if top.road_eta_seconds:
            assert top.road_eta_seconds < 300, "should be a few minutes, not ten"
