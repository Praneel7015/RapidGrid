"""A real fix is matched from the position along the route, not the segment's
first vertex (CodeRabbit, PR 22: the window origin)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import monitor

M_PER_DEG_LNG = 111_320.0 * 0.9744  # cos(13 deg)


def _east(metres: float) -> dict:
    return {"lat": 13.0, "lng": 77.0 + metres / M_PER_DEG_LNG}


# Vertices at 0, 1000 and 2000 metres along a straight route.
ROUTE = [_east(0), _east(1000), _east(2000)]


def test_the_matched_distance_is_the_position_along_the_segment():
    assert abs(monitor.matched_m(ROUTE, _east(1100), 1) - 1100) < 2
    assert abs(monitor.matched_m(ROUTE, _east(1000), 0) - 1000) < 2


def test_the_window_starts_at_the_matched_position_so_the_next_segment_is_reachable():
    # Matched at 1000 m (the vertex, and segment 0's end). Fixes after it are
    # on segment 1; the window must not be anchored at segment 0's start.
    origin = monitor.matched_m(ROUTE, _east(1000), 0)
    off, index = monitor.project_near(ROUTE, _east(1100), 0, monitor.REACH_MIN_M, origin_m=origin)
    assert index == 1 and off < 5


def test_without_the_matched_position_the_same_fix_is_out_of_reach():
    # The window from the start of segment 0 ends 250 m along the route, and only
    # that part of the segment counts: the fix at 1100 m is 850 m out of reach.
    off, index = monitor.project_near(ROUTE, _east(1100), 0, monitor.REACH_MIN_M)
    assert index == 0 and abs(off - 850) < 5


def test_the_reach_limit_still_holds_from_the_matched_position():
    # Four vertices; matched at 1000 m, reach 250 m: the window ends at 1250 m.
    # Segment 1 counts only up to 1250 m, so the fix at 2600 m is 1350 m away.
    route = [_east(0), _east(1000), _east(2000), _east(3000)]
    origin = monitor.matched_m(route, _east(1000), 0)
    off, index = monitor.project_near(route, _east(2600), 0, monitor.REACH_MIN_M, origin_m=origin)
    assert index == 1 and abs(off - 1350) < 5
