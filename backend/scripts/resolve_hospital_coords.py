"""
Resolve curated hospital coordinates against OpenStreetMap.

Why this exists
---------------
The hospital coordinates were originally hand-written from memory, and an audit
found 24 of 30 were more than 700 m out - St John's by 18 km, Sanjay Gandhi by
4.2 km, Ramaiah by 2.4 km. That is not a rounding problem: a patient with chest
pain standing at MSRIT was told Ramaiah Memorial was 10 minutes away when it is
595 m down the road, because the system was routing to a point in Malleswaram.

Hand-correcting 24 coordinates from memory would reintroduce the same class of
error. This resolves them from OSM instead, and writes the result into
data/hospital_capabilities.json so the coordinates live with the profile.

Matching is deliberately conservative
-------------------------------------
A loose matcher is worse than none: an early version paired "Apollo Hospital
(Bannerghatta Road)" with *Fortis* Bannerghatta, and "Mallya Hospital" with a
diagnostics centre of the same name. Candidates are scored on rare-token
overlap - "jayadeva" and "kidwai" identify a facility, "hospital" and
"bangalore" do not - and anything below the confidence floor is reported for a
human to check rather than silently written.

    python scripts/resolve_hospital_coords.py            # report only
    python scripts/resolve_hospital_coords.py --write    # update the JSON
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CAPS = ROOT / "data" / "hospital_capabilities.json"
# Overpass is free and community-funded; it rate-limits hard. Cache the bulk
# fetch so re-running the matcher costs nothing.
CACHE = ROOT / "data" / ".osm_healthcare_cache.json"

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

BBOX = (12.80, 77.45, 13.10, 77.78)

# Words that appear in half the hospital names in the city and identify nothing.
GENERIC = {
    "hospital",
    "hospitals",
    "the",
    "and",
    "of",
    "for",
    "institute",
    "medical",
    "centre",
    "center",
    "clinic",
    "clinics",
    "multispeciality",
    "multi",
    "speciality",
    "specialty",
    "care",
    "health",
    "healthcare",
    "sciences",
    "science",
    "college",
    "research",
    "memorial",
    "road",
    "bengaluru",
    "bangalore",
    "new",
    "old",
    "sri",
    "shri",
    "dr",
    "st",
    "saint",
}

# A match must clear this share of the curated name's distinctive tokens.
MIN_TOKEN_SHARE = 0.5
# ...and land within this radius of the existing hint, unless it is a perfect
# name match. Guards against grabbing a same-named branch across the city.
MAX_DRIFT_M = 6000
# No match is accepted beyond this, even on a perfect name match - a same-named
# facility on the other side of the city is a different facility.
HARD_DRIFT_M = 8000


# Facilities the automatic matcher will not resolve, checked by hand against
# the OSM extract. Each records what was found and why it was accepted, so the
# next person can re-verify rather than trust it.
MANUAL = {
    # Seed already sits 40-90 m from the NIMHANS campus buildings; OSM only
    # names the sub-units ("NIMHANS Physiotherapy Center"), not the hospital.
    "blr-010": (12.9406, 77.5954, "seed verified against NIMHANS campus nodes"),
    # OSM: "Sanjay Gandhi Accident and Trauma Hospital". The seeded point was
    # 4.25 km north, in the wrong part of the city entirely.
    "blr-024": (12.9358, 77.5939, "OSM: Sanjay Gandhi Accident and Trauma Hospital"),
    # OSM: "Narayana Hrudayalaya" - the Health City campus at Bommasandra.
    "blr-004": (12.8078, 77.6951, "OSM: Narayana Hrudayalaya, Bommasandra campus"),
    # OSM: "St. Johns hospital Emergency ward", 750 m from the seed. The
    # matcher had latched onto "St John's Health Centre" 18 km away.
    "blr-005": (12.9306, 77.6186, "OSM: St. Johns hospital Emergency ward"),
    # OSM: "Apollo Hospital" on Bannerghatta Road. The matcher rejected it for
    # not repeating the road name, having earlier matched Fortis instead.
    "blr-003": (12.8963, 77.5983, "OSM: Apollo Hospital, Bannerghatta Road"),
    # OSM: "Kauvery hospital", 1.3 km from the seed.
    "blr-016": (12.8551, 77.6632, "OSM: Kauvery hospital, Electronic City"),
    # Columbia Asia Hebbal was rebranded Manipal Hospital Hebbal and OSM has no
    # confident entry. Seed retained and flagged - the least-verified of the 30.
    "blr-012": (13.0430, 77.5963, "UNVERIFIED - rebranded, no confident OSM entry"),
}


def haversine(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def tokens(name: str) -> set[str]:
    clean = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return {t for t in clean.split() if t not in GENERIC and len(t) > 2}


def fetch_osm(refresh: bool = False) -> list[tuple[str, float, float]]:
    if CACHE.exists() and not refresh:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"  using cached OSM extract ({len(cached)} features) - pass --refresh to re-fetch\n")
        return [(n, la, lo) for n, la, lo in cached]

    south, west, north, east = BBOX
    query = f"""[out:json][timeout:180];
    (
      node({south},{west},{north},{east})["amenity"~"^(hospital|clinic|doctors)$"]["name"];
      way({south},{west},{north},{east})["amenity"~"^(hospital|clinic|doctors)$"]["name"];
      node({south},{west},{north},{east})["healthcare"]["name"];
      way({south},{west},{north},{east})["healthcare"]["name"];
    );
    out center;"""

    for mirror in MIRRORS:
        host = mirror.split("/")[2]
        try:
            print(f"  querying {host} ...")
            resp = httpx.get(
                mirror,
                params={"data": query},
                headers={"User-Agent": "RapidGrid/1.0 (routing-research)"},
                timeout=220,
            )
            resp.raise_for_status()
            out = []
            for el in resp.json().get("elements", []):
                name = (el.get("tags", {}).get("name") or "").strip()
                lat = el.get("lat") or el.get("center", {}).get("lat")
                lng = el.get("lon") or el.get("center", {}).get("lon")
                if name and lat is not None and lng is not None:
                    out.append((name, float(lat), float(lng)))
            if out:
                CACHE.write_text(json.dumps(out), encoding="utf-8")
                print(f"  -> {len(out)} named healthcare features (cached)\n")
                return out
        except Exception as exc:
            print(f"  !! {host}: {type(exc).__name__}")
            time.sleep(4)
    return []


def build_rarity(osm) -> dict[str, float]:
    """Rare tokens identify a facility; common ones do not."""
    counts = Counter()
    for name, _, _ in osm:
        counts.update(tokens(name))
    total = max(1, len(osm))
    return {t: math.log(total / (1 + c)) for t, c in counts.items()}


def resolve(curated_name, hint_lat, hint_lng, osm, rarity):
    want = tokens(curated_name)
    if not want:
        return None

    want_weight = sum(rarity.get(t, 4.0) for t in want) or 1.0

    # The brand token is the facility's identity - "apollo", "jayadeva",
    # "kidwai". A candidate lacking it is a different hospital, however many
    # location words it shares: without this, "Apollo Hospital (Bannerghatta
    # Road)" matches *Fortis* Bannerghatta on the road name alone.
    #
    # Curated names are written "Brand Hospital (Location)", so the brand is
    # whatever precedes the parenthesis. Taking the globally rarest token
    # instead picks the location - "airport" is rarer than "manipal" - and
    # then rejects the correct hospital for not repeating the suburb.
    head = curated_name.split("(")[0]
    brand_pool = tokens(head) or want
    brand = max(brand_pool, key=lambda t: rarity.get(t, 4.0))

    best = None
    for name, lat, lng in osm:
        have = tokens(name)
        shared = want & have
        if not shared or brand not in have:
            continue

        weighted = sum(rarity.get(t, 4.0) for t in shared) / want_weight
        share = len(shared) / len(want)
        dist = haversine(hint_lat, hint_lng, lat, lng)
        exact = want == have

        # A perfect name match still has to be in the right part of the city.
        # "St John's Medical College Hospital" matched "St John's Health
        # Centre" 18 km away on an exact token match alone.
        if dist > HARD_DRIFT_M:
            continue
        if not exact and dist > MAX_DRIFT_M:
            continue
        if not exact and share < MIN_TOKEN_SHARE:
            continue

        # Prefer strong name agreement; break ties by proximity to the hint.
        score = weighted + (0.35 if exact else 0.0) - (dist / 100000)
        if best is None or score > best[0]:
            best = (score, weighted, share, dist, name, lat, lng)

    return best


def main() -> int:
    write = "--write" in sys.argv

    payload = json.loads(CAPS.read_text(encoding="utf-8"))
    profiles = payload["hospitals"]

    sys.path.insert(0, str(ROOT))
    from agents.hospital_intelligence import HospitalIntelligenceAgent

    hints = {h["id"]: h for h in HospitalIntelligenceAgent.BANGALORE_HOSPITALS_SEED}

    osm = fetch_osm(refresh="--refresh" in sys.argv)
    if not osm:
        print("Could not reach any Overpass mirror. Nothing written.")
        return 1
    rarity = build_rarity(osm)

    print(f"{'CURATED HOSPITAL':<44}{'DRIFT':>8}  {'CONF':>5}  MATCHED IN OSM")
    print("-" * 112)

    resolved, flagged = {}, []
    for hid, profile in profiles.items():
        hint = hints.get(hid)
        if not hint:
            flagged.append((hid, profile["name"], "no seed coordinate to anchor on"))
            continue

        best = resolve(profile["name"], hint["lat"], hint["lng"], osm, rarity)
        if best is None:
            print(f"{profile['name'][:42]:<44}{'--':>8}  {'--':>5}  no confident match")
            flagged.append((hid, profile["name"], "no OSM match cleared the floor"))
            continue

        _, weighted, share, dist, osm_name, lat, lng = best
        mark = " <-- CHECK" if weighted < 0.40 else ""
        print(f"{profile['name'][:42]:<44}{dist:>6.0f} m  {weighted:>5.2f}  {osm_name[:38]}{mark}")
        if weighted < 0.40:
            flagged.append((hid, profile["name"], f"weak match: {osm_name}"))
            continue
        resolved[hid] = (round(lat, 6), round(lng, 6), osm_name, dist)

    print("\n" + "=" * 112)
    print(f"{len(resolved)} resolved confidently, {len(flagged)} need a human")
    for hid, name, why in flagged:
        print(f"  {hid}  {name[:44]:<46} {why}")

    if not write:
        print("\nReport only. Re-run with --write to update hospital_capabilities.json.")
        return 0

    for hid, (lat, lng, osm_name, dist) in resolved.items():
        profiles[hid]["lat"] = lat
        profiles[hid]["lng"] = lng
        profiles[hid]["coord_source"] = "openstreetmap"
        profiles[hid]["osm_name"] = osm_name

    for hid, (lat, lng, note) in MANUAL.items():
        if hid not in profiles:
            continue
        profiles[hid]["lat"] = lat
        profiles[hid]["lng"] = lng
        profiles[hid]["coord_source"] = (
            "manual-unverified" if note.startswith("UNVERIFIED") else "manual-reviewed"
        )
        profiles[hid]["coord_note"] = note
    print(f"Applied {len(MANUAL)} hand-reviewed coordinates")

    payload["meta"]["coordinates"] = (
        "Resolved against OpenStreetMap by scripts/resolve_hospital_coords.py. "
        "Entries without a lat/lng fell below the match confidence floor and "
        "keep their reviewed manual coordinate."
    )
    CAPS.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(resolved)} coordinates into {CAPS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
