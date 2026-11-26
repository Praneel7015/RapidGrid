"""Fetch every OpenStreetMap hospital in Bangalore once, and tag each with its
area, into data/bangalore_hospitals.json. The demo reads that file, so the map
still shows hospitals when venue Wi-Fi (or Overpass) is down.

Run from backend/:  python scripts/fetch_bangalore_hospitals.py
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import httpx

OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Whole city, south-west to north-east.
BBOX = (12.80, 77.45, 13.15, 77.80)
# Named areas: a hospital is tagged with the nearest one within its radius.
AREAS = [
    {"id": "whitefield", "name": "Whitefield", "lat": 12.9698, "lng": 77.7500, "radius_m": 6000},
    {"id": "mathikere", "name": "Mathikere", "lat": 13.0330, "lng": 77.5640, "radius_m": 3000},
    # The venue: The Chancery Pavilion, Residency Road (OpenStreetMap point).
    {"id": "venue", "name": "The Chancery Pavilion", "lat": 12.9658944, "lng": 77.5986765, "radius_m": 3000},
]
OUT = Path(__file__).resolve().parent.parent / "data" / "bangalore_hospitals.json"


def _distance_m(a_lat, a_lng, b_lat, b_lng) -> float:
    r = 6_371_000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _area_for(lat: float, lng: float) -> str:
    best, best_d = "bangalore", float("inf")
    for area in AREAS:
        if area["lat"] is None:
            continue
        d = _distance_m(lat, lng, area["lat"], area["lng"])
        if d <= area["radius_m"] and d < best_d:
            best, best_d = area["id"], d
    return best


def main() -> int:
    s, w, n, e = BBOX
    query = (
        f'[out:json][timeout:90];('
        f'node["amenity"="hospital"]({s},{w},{n},{e});'
        f'way["amenity"="hospital"]({s},{w},{n},{e});'
        f');out center tags;'
    )
    elements = None
    for attempt in range(4):
        for url in OVERPASS_MIRRORS:
            try:
                with httpx.Client(timeout=120.0) as client:
                    res = client.post(
                        url,
                        data={"data": query},
                        headers={"User-Agent": "GeoAgentic/1.0 (emergency-routing-research)"},
                    )
                res.raise_for_status()
                payload = res.json()
                if payload.get("remark") or "elements" not in payload:
                    raise ValueError(f"Overpass reported: {payload.get('remark', 'no elements')}")
                elements = payload["elements"]
                break
            except (httpx.HTTPError, ValueError) as exc:
                print(f"{url}: {exc}", file=sys.stderr)
        if elements is not None:
            break
        time.sleep(20 * (attempt + 1))
    if elements is None:
        print("Overpass unavailable on every mirror; nothing written.", file=sys.stderr)
        return 1

    hospitals = []
    for el in elements:
        tags = el.get("tags", {})
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        if el["type"] == "node":
            lat, lng = el["lat"], el["lon"]
        else:
            center = el.get("center") or {}
            if "lat" not in center:
                continue
            lat, lng = center["lat"], center["lon"]
        hospitals.append({
            "osm_id": f'{el["type"]}/{el["id"]}',
            "name": name,
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "area": _area_for(lat, lng),
            "operator_type": tags.get("operator:type") or tags.get("operator_type"),
            "emergency": tags.get("emergency"),
        })

    hospitals.sort(key=lambda h: (h["area"], h["name"]))
    payload = {
        "source": "OpenStreetMap (ODbL), via Overpass API",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bbox": {"south": s, "west": w, "north": n, "east": e},
        "areas": [{k: a[k] for k in ("id", "name", "lat", "lng", "radius_m")} for a in AREAS],
        "count": len(hospitals),
        "hospitals": hospitals,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    by_area: dict[str, int] = {}
    for h in hospitals:
        by_area[h["area"]] = by_area.get(h["area"], 0) + 1
    print(f"wrote {len(hospitals)} hospitals to {OUT}: {by_area}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
