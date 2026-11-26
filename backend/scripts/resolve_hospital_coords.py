"""
Resolve curated hospital coordinates against OpenStreetMap.

Pipeline (run from backend/)
----------------------------
    python scripts/fetch_bangalore_hospitals.py   # network: OSM extract -> data/bangalore_hospitals.json
    python scripts/resolve_hospital_coords.py     # offline: match curated entries -> data/hospital_capabilities.json

The resolver reads only the local extract, so it is deterministic and works on
venue Wi-Fi with Overpass down. No coordinate is typed in by hand: every curated
point is either an OSM node/way picked by the matcher, or an OSM feature pinned
by its osm_id in PINNED below.

Why this exists
---------------
The hospital coordinates were originally hand-written from memory, and an audit
found 24 of 30 were more than 700 m out - St John's by 18 km, Sanjay Gandhi by
4.2 km, Ramaiah by 2.4 km. A patient with chest pain standing at MSRIT was told
Ramaiah Memorial was 10 minutes away when it is 595 m down the road.

Matching is deliberately conservative
-------------------------------------
A loose matcher is worse than none: an early version paired "Apollo Hospital
(Bannerghatta Road)" with *Fortis* Bannerghatta, and "Mallya Hospital" with a
diagnostics centre of the same name. Candidates are scored on rare-token
overlap - "jayadeva" and "kidwai" identify a facility, "hospital" and
"bangalore" do not - and anything below the confidence floor is reported for a
human to check rather than silently written.

Entries whose curated name shares fewer than half its distinctive tokens with
the real name (e.g. "Narayana Health City" vs "Narayana Hrudayalaya") cannot be
resolved by the matcher safely; those are pinned by osm_id after checking the
OSM record by hand, and the pin is recorded in the output.

    python scripts/resolve_hospital_coords.py            # report only
    python scripts/resolve_hospital_coords.py --write    # update the JSON
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAPS = ROOT / "data" / "hospital_capabilities.json"
OSM_EXTRACT = ROOT / "data" / "bangalore_hospitals.json"

# Words that appear in half the hospital names in the city and identify nothing.
GENERIC = {
    "hospital", "hospitals", "the", "and", "of", "for", "institute", "medical",
    "centre", "center", "clinic", "clinics", "multispeciality", "multi",
    "speciality", "specialty", "care", "health", "healthcare", "sciences",
    "science", "college", "research", "memorial", "road", "bengaluru",
    "bangalore", "new", "old", "sri", "shri", "dr", "st", "saint",
}

# A match must clear this share of the curated name's distinctive tokens.
MIN_TOKEN_SHARE = 0.5
# ...and land within this radius of the existing hint, unless it is a perfect
# name match. Guards against grabbing a same-named branch across the city.
MAX_DRIFT_M = 6000
# No match is accepted beyond this, even on a perfect name match - a same-named
# facility on the other side of the city is a different facility.
HARD_DRIFT_M = 8000

# Curated facilities the matcher cannot resolve, pinned to the OSM feature a
# person checked. The coordinate is read from the extract, never typed here.
PINNED = {
    # Narayana Health City campus at Bommasandra: OSM "Narayana Hrudayalaya".
    "blr-004": ("way/188320000", "OSM: Narayana Hrudayalaya, Bommasandra campus"),
    # St John's: OSM "St. Johns hospital Emergency ward". The matcher latched
    # onto "St John's Health Centre" 18 km away.
    "blr-005": ("node/8428403226", "OSM: St. Johns hospital Emergency ward"),
    # NIMHANS: OSM names only campus sub-units; the Emergency Block is the
    # clinically relevant entrance for ambulance arrivals.
    "blr-010": ("node/5265649228", "OSM: Emergency Block, NIMHANS"),
    # Kauvery: "Electronic City" is a suffix, so only 1/3 of the name matches.
    "blr-016": ("node/13955109101", "OSM: Kauvery hospital, Electronic City"),
}

# No confident OSM feature exists. These keep the coordinate already in the
# table, flagged as unverified; the script does not change them.
UNMATCHED_NOTE = {
    # Columbia Asia was rebranded Manipal Hebbal; OSM has no confident entry
    # (the nearest "Columbiaa Hospital" is 4 km away in Whitefield).
    "blr-012": "UNVERIFIED - rebranded, no confident OSM entry; coordinate kept from the table",
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


def load_osm() -> list[tuple[str, float, float, str]]:
    """(name, lat, lng, osm_id) for every named hospital in the local extract."""
    payload = json.loads(OSM_EXTRACT.read_text(encoding="utf-8"))
    return [
        (h["name"].strip(), float(h["lat"]), float(h["lng"]), h["osm_id"])
        for h in payload["hospitals"]
        if h.get("name") and h.get("lat") is not None and h.get("lng") is not None
    ]


def build_rarity(osm) -> dict[str, float]:
    """Rare tokens identify a facility; common ones do not."""
    counts = Counter()
    for entry in osm:
        counts.update(tokens(entry[0]))
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
    for name, lat, lng, osm_id in osm:
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
            best = (score, weighted, share, dist, name, lat, lng, osm_id)

    return best


def main() -> int:
    write = "--write" in sys.argv

    payload = json.loads(CAPS.read_text(encoding="utf-8"))
    profiles = payload["hospitals"]
    osm = load_osm()
    rarity = build_rarity(osm)
    by_osm_id = {entry[3]: entry for entry in osm}

    print(f"OSM extract: {len(osm)} named hospitals ({OSM_EXTRACT.name})\n")
    print(f"{'CURATED HOSPITAL':<44}{'DRIFT':>8}  {'CONF':>5}  MATCHED IN OSM")
    print("-" * 112)

    resolved, flagged = {}, []
    for hid, profile in profiles.items():
        old_lat, old_lng = profile["lat"], profile["lng"]

        if hid in PINNED:
            osm_id, note = PINNED[hid]
            entry = by_osm_id.get(osm_id)
            if entry is None:
                flagged.append((hid, profile["name"], f"pinned {osm_id} not in extract"))
                continue
            _, lat, lng, _ = entry
            dist = haversine(old_lat, old_lng, lat, lng)
            print(f"{profile['name'][:42]:<44}{dist:>6.0f} m  {'pin':>5}  {entry[0][:38]}")
            resolved[hid] = (lat, lng, entry[0], osm_id, dist, "openstreetmap-pinned", note)
            continue

        if hid in UNMATCHED_NOTE:
            print(f"{profile['name'][:42]:<44}{'--':>8}  {'--':>5}  kept (unverified)")
            flagged.append((hid, profile["name"], "no OSM match; coordinate kept from table"))
            continue

        best = resolve(profile["name"], old_lat, old_lng, osm, rarity)
        if best is None:
            print(f"{profile['name'][:42]:<44}{'--':>8}  {'--':>5}  no confident match")
            flagged.append((hid, profile["name"], "no OSM match cleared the floor"))
            continue

        _, weighted, share, dist, osm_name, lat, lng, osm_id = best
        mark = " <-- CHECK" if weighted < 0.40 else ""
        print(f"{profile['name'][:42]:<44}{dist:>6.0f} m  {weighted:>5.2f}  {osm_name[:38]}{mark}")
        if weighted < 0.40:
            flagged.append((hid, profile["name"], f"weak match: {osm_name}"))
            continue
        resolved[hid] = (lat, lng, osm_name, osm_id, dist, "openstreetmap", None)

    print("\n" + "=" * 112)
    print(f"{len(resolved)} resolved from OSM, {len(flagged)} without a confident OSM match")
    for hid, name, why in flagged:
        print(f"  {hid}  {name[:44]:<46} {why}")

    if not write:
        print("\nReport only. Re-run with --write to update hospital_capabilities.json.")
        return 0

    for hid, (lat, lng, osm_name, osm_id, _dist, source, note) in resolved.items():
        p = profiles[hid]
        p["lat"] = round(lat, 6)
        p["lng"] = round(lng, 6)
        p["coord_source"] = source
        p["osm_id"] = osm_id
        p["osm_name"] = osm_name
        if note:
            p["coord_note"] = note
        else:
            p.pop("coord_note", None)

    # Every profile without a confident match is flagged, including pinned
    # features missing from this extract: keep its coordinates, drop the OSM link.
    for hid, _name, reason in flagged:
        if hid in profiles:
            profiles[hid]["coord_source"] = "manual-unverified"
            profiles[hid]["coord_note"] = f"not confirmed: {reason}"
            profiles[hid].pop("osm_id", None)
            profiles[hid].pop("osm_name", None)
    for hid, note in UNMATCHED_NOTE.items():
        if hid in profiles:
            profiles[hid]["coord_source"] = "manual-unverified"
            profiles[hid]["coord_note"] = note
            profiles[hid].pop("osm_id", None)
            profiles[hid].pop("osm_name", None)

    payload["meta"]["coordinates"] = (
        "Resolved from the OpenStreetMap extract (data/bangalore_hospitals.json) by "
        "scripts/resolve_hospital_coords.py. Each entry records its osm_id. Entries "
        "without a confident OSM match keep their table coordinate, flagged "
        "manual-unverified."
    )
    CAPS.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {len(resolved)} OSM coordinates into {CAPS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
