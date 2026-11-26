"""Staged closures, accidents and the weather survive a restart."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.city_state import CityState, city_state
from agents.local_router import local_router


@pytest.fixture
def graph():
    if not local_router.available and not local_router.load():
        pytest.skip("offline road graph not available")
    city_state.attach(local_router)
    city_state.clear()
    yield
    city_state.clear()


def _restarted() -> CityState:
    fresh = CityState()  # a new process: nothing in memory
    fresh.attach(local_router)
    fresh.restore()
    return fresh


def test_a_closure_an_accident_and_the_weather_come_back_after_a_restart(graph):
    city_state.close_road([10, 11, 12], label="Demo: Bellary Road closed")
    city_state.report_incident([500], label="Demo: accident")
    city_state.set_weather("Rain")
    after = _restarted()
    assert after.closed_edges == {10, 11, 12}
    assert after.incident_edges == {500}
    assert after.weather == "Rain"
    assert [i.target for i in after.interventions if i.kind == "close_road"] == ["Demo: Bellary Road closed"]


def test_a_cleared_city_stays_clear_after_a_restart(graph):
    city_state.close_road([10], label="x")
    city_state.clear()  # the demo reset clears it
    after = _restarted()
    assert not after.closed_edges and not after.incident_edges


def test_edges_missing_from_a_rebuilt_graph_are_skipped(graph):
    import storage

    storage.save_collection(CityState._STORE, {"weather": "Clear", "applied": [["close", [10, 10 ** 9], "x"]]})
    assert _restarted().closed_edges == {10}
