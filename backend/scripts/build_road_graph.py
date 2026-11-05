"""
Build a routable road graph for the Bengaluru metro area from OpenStreetMap.

Run once (or whenever you want to refresh the network):

    python scripts/build_road_graph.py

Writes ``data/bengaluru_road_graph.json`` — the offline routing tier used by
the Route Optimization Agent when the Google Routes API is unavailable,
rate-limited, or (as during development) not enabled on the Cloud project.

The graph is deliberately limited to the drivable *skeleton* (motorway ->
tertiary, plus their link roads).  Residential streets would multiply the
node count for very little routing value at emergency-vehicle scale.
"""

from __future__ import annotations

import gzip
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

# Bengaluru metro bounding box — covers every emergency hub and hospital
# in the seed lists, from Hebbal in the north to Electronic City in the south.
BBOX = (12.80, 77.45, 13.10, 77.78)  # south, west, north, east

# Public Overpass instances rate-limit aggressively (503/504). Try mirrors in
# order rather than failing the whole build on one busy endpoint.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Free-flow speeds (km/h) by OSM highway class, tuned for Indian urban traffic
# rather than the optimistic defaults most routers ship with.
ROAD_SPEEDS_KMH: dict[str, float] = {
    "motorway": 80.0,
    "motorway_link": 45.0,
    "trunk": 60.0,
    "trunk_link": 35.0,
    "primary": 45.0,
    "primary_link": 30.0,
    "secondary": 35.0,
    "secondary_link": 25.0,
    "tertiary": 30.0,
    "tertiary_link": 20.0,
}

# Emergency vehicles get priority passage — sirens, right of way, contraflow.
# Applied uniformly here; a future refinement is to vary it by road class.
EMERGENCY_SPEED_FACTOR = 1.25

_EARTH_R = 6_371_000
_SNAP_PRECISION = 6  # ~0.1 m — dedupes shared way endpoints into one node

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "bengaluru_road_graph.json.gz"


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_ways() -> list[dict]:
    """Pull drivable ways with full geometry from Overpass."""
    classes = "|".join(c for c in ROAD_SPEEDS_KMH if not c.endswith("_link"))
    south, west, north, east = BBOX
    query = f"""
    [out:json][timeout:180];
    way({south},{west},{north},{east})
       [highway~"^({classes})(_link)?$"];
    out geom;
    """
    print(f"Querying Overpass for drivable ways in {BBOX} ...")
    headers = {"User-Agent": "GeoAgentic/1.0 (emergency-routing-research)"}

    last_error: Exception | None = None
    for attempt, mirror in enumerate(OVERPASS_MIRRORS, start=1):
        host = mirror.split("/")[2]
        try:
            started = time.time()
            with httpx.Client(timeout=240.0) as client:
                resp = client.get(mirror, params={"data": query}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            elements = [e for e in data.get("elements", []) if e.get("type") == "way"]
            if not elements:
                raise ValueError("mirror returned zero ways")
            print(f"  -> {len(elements)} ways from {host} in {time.time() - started:.1f}s")
            return elements
        except Exception as exc:
            last_error = exc
            reason = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                reason = f"HTTP {exc.response.status_code}"
            print(f"  !! {host} failed ({reason})", end="")
            if attempt < len(OVERPASS_MIRRORS):
                backoff = 5 * attempt
                print(f" - retrying next mirror in {backoff}s")
                time.sleep(backoff)
            else:
                print()

    print(f"All {len(OVERPASS_MIRRORS)} Overpass mirrors failed: {last_error}")
    return []


def node_key(lat: float, lng: float) -> str:
    return f"{round(lat, _SNAP_PRECISION)},{round(lng, _SNAP_PRECISION)}"


def build_graph(ways: list[dict]) -> dict:
    """Turn OSM ways into a node/edge graph with emergency travel times."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    degree: dict[str, int] = defaultdict(int)

    def ensure_node(lat: float, lng: float) -> str:
        key = node_key(lat, lng)
        if key not in nodes:
            nodes[key] = {"id": key, "lat": lat, "lng": lng}
        return key

    skipped = 0
    for way in ways:
        geometry = way.get("geometry") or []
        if len(geometry) < 2:
            skipped += 1
            continue

        tags = way.get("tags", {})
        highway = tags.get("highway", "tertiary")
        speed_kmh = ROAD_SPEEDS_KMH.get(highway, 30.0) * EMERGENCY_SPEED_FACTOR
        speed_ms = speed_kmh / 3.6

        # Honour one-way restrictions; "-1" means the way is digitised backwards.
        oneway_tag = str(tags.get("oneway", "no")).lower()
        oneway = oneway_tag in ("yes", "true", "1", "-1")
        reversed_way = oneway_tag == "-1"

        name = tags.get("name") or tags.get("ref") or highway.replace("_", " ").title()
        way_id = way.get("id")

        for a, b in zip(geometry, geometry[1:]):
            length_m = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            if length_m < 1.0:
                continue

            from_id = ensure_node(a["lat"], a["lon"])
            to_id = ensure_node(b["lat"], b["lon"])
            if from_id == to_id:
                continue

            if reversed_way:
                from_id, to_id = to_id, from_id

            base_travel_time_s = length_m / speed_ms

            # Multipliers are the contract the Traffic Intelligence Agent writes
            # to at runtime (see references/route-scoring.md). They start neutral.
            edge = {
                "id": f"E{len(edges)}",
                "from": from_id,
                "to": to_id,
                "length_m": round(length_m, 2),
                "base_travel_time_s": round(base_travel_time_s, 2),
                "highway": highway,
                "name": name,
                "osm_way_id": way_id,
                "congestion_multiplier": 1.0,
                "incident_multiplier": 1.0,
                "weather_multiplier": 1.0,
                "blocked": False,
            }
            edges.append(edge)
            degree[from_id] += 1
            degree[to_id] += 1

            if not oneway:
                reverse = dict(edge)
                reverse["id"] = f"E{len(edges)}"
                reverse["from"], reverse["to"] = to_id, from_id
                edges.append(reverse)
                degree[from_id] += 1
                degree[to_id] += 1

    print(
        f"  -> {len(nodes)} nodes, {len(edges)} directed edges ({skipped} degenerate ways skipped)"
    )

    # --- Compact, index-based serialisation ---------------------------------
    # Columnar arrays instead of per-edge dicts: node ids become integer
    # indices, and road names are interned into a lookup table. Cuts the file
    # from ~54 MB to something small enough to commit.
    node_index = {nid: i for i, nid in enumerate(nodes)}
    lats = [round(nodes[nid]["lat"], 6) for nid in nodes]
    lngs = [round(nodes[nid]["lng"], 6) for nid in nodes]

    names: list[str] = []
    name_index: dict[str, int] = {}
    classes: list[str] = []
    class_index: dict[str, int] = {}

    def intern(value: str, table: list[str], index: dict[str, int]) -> int:
        if value not in index:
            index[value] = len(table)
            table.append(value)
        return index[value]

    e_from, e_to, e_time, e_len, e_name, e_class = [], [], [], [], [], []
    for e in edges:
        e_from.append(node_index[e["from"]])
        e_to.append(node_index[e["to"]])
        e_time.append(round(e["base_travel_time_s"], 1))
        e_len.append(round(e["length_m"], 1))
        e_name.append(intern(e["name"], names, name_index))
        e_class.append(intern(e["highway"], classes, class_index))

    return {
        "meta": {
            "source": "OpenStreetMap via Overpass API",
            "bbox": {"south": BBOX[0], "west": BBOX[1], "north": BBOX[2], "east": BBOX[3]},
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "road_classes": sorted(ROAD_SPEEDS_KMH),
            "emergency_speed_factor": EMERGENCY_SPEED_FACTOR,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "format": "columnar-v1",
        },
        "node_lat": lats,
        "node_lng": lngs,
        "road_names": names,
        "road_classes": classes,
        # Traffic multipliers are NOT stored per edge: they default to 1.0 and
        # are applied at runtime by the Traffic Intelligence Agent.
        "edge_from": e_from,
        "edge_to": e_to,
        "edge_time": e_time,
        "edge_len": e_len,
        "edge_name": e_name,
        "edge_class": e_class,
    }


def main() -> int:
    ways = fetch_ways()
    if not ways:
        print("No ways returned — aborting without overwriting the cache.")
        return 1

    graph = build_graph(ways)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT_PATH, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(graph, f, separators=(",", ":"))

    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Wrote {OUT_PATH.name} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
