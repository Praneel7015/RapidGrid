"""
Cross-validate our data against the external APIs, before a demo.

preflight.py answers "is everything switched on". This answers the harder
question: "does what we believe agree with what Google and OpenStreetMap
believe". A coordinate can be present, well-formed and inside Bengaluru and
still be 2 km from the actual hospital - which is exactly the bug that sent a
chest-pain patient at MSRIT to a hospital ten minutes away.

Three independent checks per hospital:

  1. GRAPH SNAP    how far the coordinate is from the nearest road in our
                   OpenStreetMap graph. A hospital 500 m from any road is
                   almost certainly in the wrong place.
  2. GOOGLE REACH  whether Google Routes can actually drive there. Google
                   snaps to its own road network, so a coordinate it cannot
                   route to is one our router should not trust either.
  3. AGREEMENT     our A* road distance against Google's for the same pair.
                   Two independent networks disagreeing by more than ~40%
                   means one of them is routing somewhere else.

Only the Routes API is enabled on this project (Geocoding, Places, Distance
Matrix and Roads all return REQUEST_DENIED), so name-based verification is not
available here - that is what scripts/resolve_hospital_coords.py does against
OpenStreetMap instead.

    python scripts/validate_against_apis.py            # hospitals + hubs
    python scripts/validate_against_apis.py --quick    # skip Google, graph only
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.local_router import haversine, local_router  # noqa: E402

CAPS = ROOT / "data" / "hospital_capabilities.json"

# Fixed reference point for the comparison routes: MSRIT, the location that
# surfaced the original bug.
ORIGIN = (13.0297, 77.5645)

# Thresholds. Deliberately loose - this is looking for coordinates that are
# wrong, not coordinates that are imprecise.
SNAP_WARN_M = 250
SNAP_FAIL_M = 600
DISAGREE_WARN = 0.40  # 40% divergence between the two networks
MIN_COMPARE_M = 1500  # short routes diverge for boring reasons; skip them


def google_route(origin, dest, key):
    """Distance and duration from Google Routes, or None."""
    try:
        r = httpx.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
            },
            json={
                "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
                "destination": {
                    "location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}
                },
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
            },
            timeout=25,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        routes = r.json().get("routes") or []
        if not routes:
            return None, "no route"
        d = float(routes[0].get("distanceMeters", 0))
        secs = float(str(routes[0].get("duration", "0s")).rstrip("s") or 0)
        return (d, secs), None
    except Exception as exc:
        return None, type(exc).__name__


def main() -> int:
    load_dotenv(ROOT / ".env")
    key = os.getenv("GOOGLE_ROUTES_API_KEY")
    quick = "--quick" in sys.argv
    use_google = bool(key) and not quick

    if not local_router.load():
        print("Road graph not built. Run scripts/build_road_graph.py first.")
        return 1

    profiles = json.loads(CAPS.read_text(encoding="utf-8"))["hospitals"]

    from agents.route_optimization import EMERGENCY_HUBS

    hubs = [(f"{k}: {h['name']}", h["lat"], h["lng"]) for k, v in EMERGENCY_HUBS.items() for h in v]

    targets = [(p["name"], p["lat"], p["lng"], hid) for hid, p in profiles.items()]

    print("=" * 104)
    print(
        "  Cross-validation against OpenStreetMap graph"
        + (" and Google Routes" if use_google else " (Google skipped)")
    )
    print("=" * 104)
    if not use_google:
        print("  Google checks skipped - no key, or --quick.\n")

    print(f"{'FACILITY':<44}{'SNAP':>8}{'OURS':>10}{'GOOGLE':>10}{'DIFF':>8}  NOTES")
    print("-" * 104)

    problems, warnings, checked = [], [], 0

    for name, lat, lng, hid in targets:
        notes = []

        node = local_router.nearest_node(lat, lng)
        snap = (
            haversine(lat, lng, local_router.lat[node], local_router.lng[node])
            if node is not None
            else None
        )

        if snap is None:
            problems.append((hid, name, "does not snap to the road graph at all"))
            print(f"{name[:42]:<44}{'FAIL':>8}{'--':>10}{'--':>10}{'--':>8}  no graph node")
            continue
        if snap > SNAP_FAIL_M:
            notes.append(f"{snap:.0f} m from any road")
            problems.append((hid, name, f"{snap:.0f} m from the nearest road"))
        elif snap > SNAP_WARN_M:
            notes.append("far from a road")
            warnings.append((hid, name, f"{snap:.0f} m snap"))

        ours = local_router.find_path(ORIGIN, (lat, lng))
        our_m = ours["distance_m"] if ours else None
        if ours is None:
            problems.append((hid, name, "our router cannot reach it"))
            notes.append("unreachable by A*")

        goog_m = goog_s = None
        if use_google:
            res, err = google_route(ORIGIN, (lat, lng), key)
            checked += 1
            if res is None:
                notes.append(f"Google: {err}")
                problems.append((hid, name, f"Google cannot route to it ({err})"))
            else:
                goog_m, goog_s = res
            time.sleep(0.12)

        diff = ""
        if our_m and goog_m and max(our_m, goog_m) > MIN_COMPARE_M:
            ratio = abs(our_m - goog_m) / max(our_m, goog_m)
            diff = f"{ratio * 100:>6.0f}%"
            if ratio > DISAGREE_WARN:
                notes.append("networks disagree")
                warnings.append((hid, name, f"{ratio * 100:.0f}% distance divergence"))

        print(
            f"{name[:42]:<44}"
            f"{snap:>6.0f} m"
            f"{(our_m / 1000 if our_m else 0):>9.1f}k"
            f"{(goog_m / 1000 if goog_m else 0):>9.1f}k"
            f"{diff:>8}  {', '.join(notes)}"
        )

    # --- Emergency hubs ------------------------------------------------------
    print(f"\n{'EMERGENCY HUB':<44}{'SNAP':>8}  NOTES")
    print("-" * 104)
    for label, lat, lng in hubs:
        node = local_router.nearest_node(lat, lng)
        snap = (
            haversine(lat, lng, local_router.lat[node], local_router.lng[node])
            if node is not None
            else None
        )
        note = ""
        if snap is None:
            note = "no graph node"
            problems.append(("hub", label, "does not snap to the road graph"))
        elif snap > SNAP_FAIL_M:
            note = "far from any road"
            problems.append(("hub", label, f"{snap:.0f} m from the nearest road"))
        print(f"{label[:42]:<44}{(snap or 0):>6.0f} m  {note}")

    # --- Routing agreement on a fixed corridor set ---------------------------
    if use_google:
        print(f"\n{'CORRIDOR':<44}{'OURS':>10}{'GOOGLE':>10}{'DIFF':>8}")
        print("-" * 104)
        corridors = [
            ("Hebbal -> Jayadeva", (13.0350, 77.5970), (12.9185, 77.5993)),
            ("Whitefield -> Victoria", (12.9698, 77.7499), (12.9635, 77.5737)),
            ("Electronic City -> NIMHANS", (12.8457, 77.6703), (12.9406, 77.5954)),
            ("MSRIT -> Ramaiah", ORIGIN, (13.0282, 77.5698)),
        ]
        for label, a, b in corridors:
            ours = local_router.find_path(a, b)
            res, err = google_route(a, b, key)
            if not ours or not res:
                print(f"{label:<44}{'--':>10}{'--':>10}  {err or 'no A* path'}")
                continue
            om, (gm, _gs) = ours["distance_m"], res
            ratio = abs(om - gm) / max(om, gm)
            # A percentage on a 700 m route is noise - one extra turn swings it
            # 30%. Only compare corridors long enough for the number to mean
            # something, the same floor the per-hospital check uses.
            comparable = max(om, gm) > MIN_COMPARE_M
            flag = "  <-- diverges" if (comparable and ratio > DISAGREE_WARN) else ""
            shown = f"{ratio * 100:>7.0f}%" if comparable else f"{'short':>8}"
            print(f"{label:<44}{om / 1000:>9.1f}k{gm / 1000:>9.1f}k{shown}{flag}")
            if comparable and ratio > DISAGREE_WARN:
                warnings.append(("corridor", label, f"{ratio * 100:.0f}% divergence"))

    # --- Verdict -------------------------------------------------------------
    print("\n" + "=" * 104)
    print(
        f"  {len(targets)} hospitals, {len(hubs)} hubs checked"
        + (f", {checked} Google calls" if use_google else "")
    )
    print(f"  {len(problems)} problems, {len(warnings)} warnings")
    if problems:
        print("\n  PROBLEMS")
        for hid, name, why in problems:
            print(f"    {hid:<10} {name[:44]:<46} {why}")
    if warnings:
        print("\n  WARNINGS")
        for hid, name, why in warnings:
            print(f"    {hid:<10} {name[:44]:<46} {why}")
    if not problems and not warnings:
        print("\n  Everything agrees. Safe to demo.")
    print("=" * 104)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
