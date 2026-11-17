"""Dispatched routes respect the city model even with a Google key (audit F6)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.route_optimization as ro
from agents.city_state import city_state
from agents.local_router import local_router
from agents.route_optimization import RouteOptimizationAgent
from models.schemas import Coordinate, DataFreshness, RouteRequest

HEBBAL = (13.0350, 77.5970)
JAYADEVA = (12.9290, 77.5878)


def request():
    return RouteRequest(origin=Coordinate(lat=HEBBAL[0], lng=HEBBAL[1]),
                        destination=Coordinate(lat=JAYADEVA[0], lng=JAYADEVA[1]))


@pytest.fixture
def city():
    if not local_router.available and not local_router.load():
        pytest.skip("offline road graph not available")
    city_state.attach(local_router)
    city_state.clear()
    yield city_state
    city_state.clear()


def test_with_a_closure_drawn_the_dispatched_route_avoids_it_without_calling_google(city):
    agent = RouteOptimizationAgent()
    agent.api_key = "configured"
    before = local_router.find_path(HEBBAL, JAYADEVA)
    bellary = [e for e in before["edge_ids"]
               if "Bellary" in local_router.road_names[local_router.edge_name[e]]]
    assert bellary, "the test route should use Bellary Road"
    city.close_road(bellary, "Bellary Road")

    with patch.object(ro.httpx, "Client") as google:
        route = agent.compute_route(request())
        google.assert_not_called()
    assert route.data_freshness == DataFreshness.CACHED
    assert not set(route.edge_ids) & set(bellary)
    assert route.eta > before["travel_time_s"]


def test_without_interventions_google_is_still_used(city):
    agent = RouteOptimizationAgent()
    agent.api_key = "configured"
    with patch.object(ro.httpx, "Client") as google:
        google.return_value.__enter__.return_value.post.side_effect = RuntimeError("stubbed")
        agent.compute_route(request())
        google.assert_called_once()


def test_google_timeout_is_short():
    assert ro.GOOGLE_TIMEOUT_S <= 5
