"""Snapping onto the mutually reachable road core (no one-way dead ends)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.local_router import local_router


@pytest.fixture(scope="module")
def graph():
    if not local_router.available and not local_router.load():
        pytest.skip("offline road graph not available")
    return local_router


def test_most_of_the_city_is_in_the_core(graph):
    assert sum(graph._in_core) / len(graph._in_core) > 0.9


def test_a_location_in_a_one_way_pocket_still_gets_a_route(graph):
    # 12.985, 77.594 snaps (by plain distance) into a pocket of one-way roads
    # that can be entered but not left; this used to give "no route".
    out = graph.find_path((12.985, 77.594), (12.9834, 77.5957))
    assert out is not None and out["distance_m"] > 0
    assert graph.find_path((12.985, 77.594), (13.0, 77.6)) is not None


def test_snapped_points_are_on_the_core(graph):
    for lat, lng in [(12.985, 77.594), (13.035, 77.597), (12.9716, 77.5946)]:
        assert any(graph._in_core[n] for n in graph.nearest_nodes(lat, lng))


def test_any_two_core_points_connect(graph):
    points = [(13.0473, 77.5908), (12.9585, 77.6483), (12.9634, 77.5746), (12.90, 77.60), (13.05, 77.65)]
    for a in points:
        for b in points:
            if a != b:
                assert graph.find_path(a, b) is not None, (a, b)


def _tiny(edges, n):
    """A small synthetic graph: nodes 0..n-1, directed edges (a, b)."""
    from agents.local_router import LocalGraphRouter

    g = LocalGraphRouter()
    g.lat = [12.9 + i * 0.001 for i in range(n)]
    g.lng = [77.6] * n
    g.edge_to = [b for _a, b in edges]
    edge_from = [a for a, _b in edges]
    g._build_adjacency(edge_from)
    g._build_core(edge_from)
    return g


def test_core_finds_the_largest_mutually_reachable_set():
    # 0<->1<->2<->0 is a cycle (the core); 3 can be entered from 2 but not
    # left (a pocket); 4 can leave to 0 but not be entered.
    g = _tiny([(0, 1), (1, 2), (2, 0), (2, 3), (4, 0)], 5)
    assert list(g._in_core) == [1, 1, 1, 0, 0]


def test_core_on_an_empty_graph():
    assert list(_tiny([], 0)._in_core) == []


def test_a_far_core_node_is_never_used(graph):
    import random
    from agents.local_router import haversine

    random.seed(7)
    for _ in range(400):
        lat, lng = random.uniform(12.85, 13.1), random.uniform(77.48, 77.76)
        nearest = graph.nearest_node(lat, lng)
        if nearest is None:
            continue
        base = haversine(lat, lng, graph.lat[nearest], graph.lng[nearest])
        for node in graph.nearest_nodes(lat, lng):
            assert haversine(lat, lng, graph.lat[node], graph.lng[node]) <= max(base, graph.SNAP_LIMIT_M) + 1


def test_the_same_point_has_no_route(graph):
    assert graph.find_path((13.035, 77.597), (13.035, 77.597)) is None


def test_the_destination_pin_stays_near_where_it_was_put(graph):
    out = graph.find_path((13.0473, 77.5908), (12.985, 77.594))
    assert out["dest_snap_distance_m"] <= graph.SNAP_LIMIT_M
    assert out["snap_distance_m"] <= graph.SNAP_LIMIT_M


def test_an_unreachable_goal_costs_one_search(graph):
    import time

    from agents.local_router import haversine

    dest = (12.9834, 77.5957)
    near = {n for n in range(len(graph.lat))
            if abs(graph.lat[n] - dest[0]) < 0.006 and abs(graph.lng[n] - dest[1]) < 0.006
            and haversine(dest[0], dest[1], graph.lat[n], graph.lng[n]) <= 450}
    into = [e for e in range(len(graph.edge_to)) if graph.edge_to[e] in near]
    graph.block_edges(into)
    try:
        started = time.monotonic()
        assert graph.find_path((13.0473, 77.5908), dest) is None
        assert time.monotonic() - started < 4.0
    finally:
        graph.unblock_all()
