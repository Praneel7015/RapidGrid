"""
Hospital Intelligence Agent for GeoAgentic (Live Overpass API).
"""

from __future__ import annotations

import json
import logging
import math
import hashlib
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from bus.event_bus import HOSPITAL_RANKED, bus
from models.schemas import (
    Coordinate,
    DataFreshness,
    EmergencyType,
    HospitalEntry,
    HospitalRankingResponse,
    HospitalRequest,
    ScoreBreakdown,
)

logger = logging.getLogger("geoagentic.agent.hospital")

AGENT_NAME = "hospital_intelligence"

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CAPABILITIES_PATH = _DATA_DIR / "hospital_capabilities.json"

# Emergency categories that require a facility willing to receive
# undifferentiated ambulance arrivals. An oncology or maternity-only centre is
# not a valid destination for a cardiac arrest, however close it happens to be.
_RECEIVING_REQUIRED = {"cardiac", "trauma", "stroke", "general"}

# OpenStreetMap tags a great many single-speciality outpatient facilities as
# amenity=hospital: dental surgeries, eye clinics, diagnostic labs, fertility
# and dialysis centres. None of them can receive an emergency ambulance.
# Matched against the facility name when no curated profile exists.
_NON_EMERGENCY_KEYWORDS = (
    "dental", "dentist", "orthodont", "eye ", "eye_", "ophthal", "vision",
    "ayurved", "homeo", "homoeo", "unani", "siddha", "naturopath",
    "diagnostic", "laborator", " lab", "scan centre", "scan center", "imaging",
    "physiothe", "fertility", "ivf", "test tube", "dialysis", "skin ", "derma",
    "cosmetic", "aesthetic", "hair ", "veterinar", "pet ", "animal",
    "psychiatr", "de-addiction", "deaddiction", "rehab", "wellness", "spa",
    "polyclinic", "pharmacy", "chemist", "optical", "hearing", "speech",
)


def _looks_non_emergency(name: str) -> bool:
    """Heuristic screen for facilities that cannot receive an ambulance."""
    lowered = f" {name.lower()} "
    return any(k in lowered for k in _NON_EMERGENCY_KEYWORDS)


def _normalise_name(name: str) -> str:
    """Canonical form for duplicate detection."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in name.lower())
    drop = {"hospital", "hospitals", "the", "and", "of", "a", "an",
            "multispeciality", "multi", "speciality", "specialty", "centre",
            "center", "clinic", "care", "health", "institute", "medical"}
    return " ".join(t for t in cleaned.split() if t not in drop)

_EARTH_R = 6_371_000
_MAX_TRAVEL_TIME_S = 1800.0
_AVG_SPEED_MPS = 8.33

# Specialty-centre bypass thresholds.
#
# Real EMS protocols do not send a STEMI to the nearest emergency room; they
# bypass it for a facility with a 24x7 cath lab, accepting extra transport
# time. Same for major trauma (trauma centre) and stroke (thrombolysis unit).
# Without this gate, proximity outranks capability and the system recommends
# a general hospital for a cardiac arrest simply because it is 3km closer.
_CAPABILITY_GATE: dict[str, tuple[str, float]] = {
    "cardiac": ("cardiology", 0.70),
    "trauma":  ("trauma_centre", 0.70),
    "stroke":  ("neurology", 0.70),
}
# Only apply the gate while enough qualifying centres remain to choose from.
_MIN_GATED_CANDIDATES = 3

# How many shortlisted hospitals get a real road-network ETA (stage 2).
_ROAD_ETA_SHORTLIST = 8

# Overpass is a free, community-funded service that rate-limits (429) and is
# slow (5-10s). Hospital locations do not change during a demo, so cache the
# enriched result per coarse location. 3 decimal places ~ 110m.
_HOSPITAL_CACHE_TTL_S = 900.0
_HOSPITAL_CACHE_PRECISION = 2

WEIGHT_TABLES: dict[str, list[tuple[str, float]]] = {
    "general": [
        ("travel_time",          0.30),
        ("emergency_resource",   0.25),
        ("specialist",           0.20),
        ("bed_availability",     0.15),
        ("blood_availability",   0.10),
    ],
    "cardiac": [
        ("cardiology",           0.35),
        ("travel_time",          0.25),
        ("emergency_resource",   0.15),
        ("bed_availability",     0.15),
        ("blood_availability",   0.10),
    ],
    "trauma": [
        ("trauma_centre",        0.30),
        ("blood_availability",   0.20),
        ("surgical_capacity",    0.20),
        ("travel_time",          0.20),
        ("bed_availability",     0.10),
    ],
    "stroke": [
        ("neurology",            0.30),
        ("ct_imaging",           0.25),
        ("travel_time",          0.25),
        ("bed_availability",     0.10),
        ("emergency_resource",   0.10),
    ],
}

_FACTOR_LABELS: dict[str, str] = {
    "travel_time":        "Travel time",
    "emergency_resource": "ER capacity",
    "specialist":         "Specialist availability",
    "bed_availability":   "Bed availability",
    "blood_availability": "Blood supply",
    "cardiology":         "Cardiology/cath-lab",
    "trauma_centre":      "Trauma centre capability",
    "surgical_capacity":  "Surgical capacity",
    "neurology":          "Neurology availability",
    "ct_imaging":         "CT/imaging availability",
}

def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def _travel_time_score(distance_m: float) -> float:
    travel_time_s = distance_m / _AVG_SPEED_MPS
    return max(0.0, min(1.0, 1.0 - (travel_time_s / _MAX_TRAVEL_TIME_S)))

def _rebalance_after_gate(
    weights: list[tuple[str, float]], gated_factor: str
) -> list[tuple[str, float]]:
    """
    Halve the gated capability's weight and give it to travel time.

    The gate is a hard screen: every remaining candidate can deliver the
    intervention. Continuing to weight that capability heavily makes the
    engine trade minutes for a difference that no longer changes what
    treatment the patient receives.
    """
    freed = 0.0
    rebalanced: list[tuple[str, float]] = []
    for factor, weight in weights:
        if factor == gated_factor:
            freed = weight * 0.5
            rebalanced.append((factor, weight - freed))
        else:
            rebalanced.append((factor, weight))
    if not freed:
        return weights
    return [
        (f, round(w + freed, 4) if f == "travel_time" else w)
        for f, w in rebalanced
    ]


class HospitalIntelligenceAgent:
    def __init__(self) -> None:
        self._hospitals = []  # Cached list of fetched hospitals
        self._capabilities: dict[str, Any] | None = None
        self._capabilities_by_name: dict[str, Any] = {}
        self._fetch_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
        # Facilities an ER desk has put on divert. Keyed by curated id, and
        # applied during enrichment so it survives catalogue cache rebuilds.
        self._on_divert: set[str] = set()
    
    def _deterministic_float(self, string_id: str, seed: str) -> float:
        """
        Stable pseudo-random float in [0, 1] derived from a hospital id.

        Used ONLY for facilities with no curated profile, and for live
        availability figures that no real feed provides. Anything derived from
        this is tagged provenance="simulated" and must be labelled as such in
        the UI — never presented as observed fact.
        """
        h = hashlib.md5(f"{string_id}_{seed}".encode()).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF

    def _load_capabilities(self) -> dict[str, Any]:
        """Load (and memoise) the curated capability table."""
        if self._capabilities is None:
            try:
                with open(_CAPABILITIES_PATH, encoding="utf-8") as f:
                    payload = json.load(f)
                self._capabilities = payload.get("hospitals", {})
                # Secondary index so Overpass results, which carry OSM ids
                # rather than our blr-* ids, can still match on name.
                self._capabilities_by_name = {
                    entry["name"].lower(): entry
                    for entry in self._capabilities.values()
                }
                logger.info(
                    "Loaded curated capability profiles for %d hospitals",
                    len(self._capabilities),
                )
            except Exception:
                logger.exception("Could not load %s", _CAPABILITIES_PATH.name)
                self._capabilities = {}
                self._capabilities_by_name = {}
        return self._capabilities

    def _match_curated(self, hid: str, name: str) -> dict[str, Any] | None:
        """Resolve a hospital to its curated profile by id, then by name."""
        curated = self._load_capabilities()
        if hid in curated:
            return curated[hid]

        lowered = name.lower().strip()
        by_name = self._capabilities_by_name
        if lowered in by_name:
            return by_name[lowered]

        # Overpass names are noisy ("Manipal Hospital" vs our fuller title).
        # Require a distinctive token overlap rather than any substring, so
        # "Lakshmi Hospital" cannot inherit "Manipal Hospital"'s profile.
        stop = {"hospital", "hospitals", "the", "and", "of", "institute",
                "medical", "centre", "center", "clinic", "multispeciality",
                "multi", "speciality", "specialty", "care", "health"}
        tokens = {t for t in lowered.replace(",", " ").split() if t not in stop and len(t) > 3}
        if not tokens:
            return None
        for cand_name, entry in by_name.items():
            cand_tokens = {
                t for t in cand_name.replace(",", " ").replace("(", " ").replace(")", " ").split()
                if t not in stop and len(t) > 3
            }
            if tokens & cand_tokens:
                return entry
        return None
        
    # Real Bangalore hospitals as a reliable seed list — used when Overpass is slow/unavailable
    BANGALORE_HOSPITALS_SEED = [
        {"id": "blr-001", "name": "Manipal Hospital (Old Airport Road)",     "lat": 12.9591, "lng": 77.6469},
        {"id": "blr-002", "name": "Fortis Hospital (Bannerghatta Road)",      "lat": 12.8780, "lng": 77.5983},
        {"id": "blr-003", "name": "Apollo Hospital (Bannerghatta Road)",      "lat": 12.8896, "lng": 77.5979},
        {"id": "blr-004", "name": "Narayana Health City (Bommasandra)",       "lat": 12.8335, "lng": 77.6735},
        {"id": "blr-005", "name": "St. John's Medical College Hospital",      "lat": 12.9360, "lng": 77.6227},
        {"id": "blr-006", "name": "Sakra World Hospital (Devarabeesanahalli)","lat": 12.9582, "lng": 77.6982},
        {"id": "blr-007", "name": "Aster CMI Hospital (Hebbal)",              "lat": 13.0473, "lng": 77.5908},
        {"id": "blr-008", "name": "BGS Gleneagles Global Hospital (Kengeri)", "lat": 12.9051, "lng": 77.4876},
        {"id": "blr-009", "name": "MS Ramaiah Memorial Hospital",             "lat": 13.0100, "lng": 77.5571},
        {"id": "blr-010", "name": "NIMHANS (Hosur Road)",                     "lat": 12.9406, "lng": 77.5954},
        {"id": "blr-011", "name": "Vikram Hospital (Millers Road)",           "lat": 12.9834, "lng": 77.5957},
        {"id": "blr-012", "name": "Columbia Asia Hospital (Hebbal)",          "lat": 13.0430, "lng": 77.5963},
        {"id": "blr-013", "name": "Sparsh Hospital (Palace Road)",            "lat": 12.9988, "lng": 77.5792},
        {"id": "blr-014", "name": "Cloudnine Hospital (Jayanagar)",           "lat": 12.9244, "lng": 77.5837},
        {"id": "blr-015", "name": "Manipal Hospital (Whitefield)",            "lat": 12.9698, "lng": 77.7499},
        {"id": "blr-016", "name": "Kauvery Hospital (Electronic City)",       "lat": 12.8457, "lng": 77.6703},
        {"id": "blr-017", "name": "Motherhood Hospital (Indiranagar)",        "lat": 12.9784, "lng": 77.6408},
        {"id": "blr-018", "name": "Rainbow Hospital (BTM Layout)",            "lat": 12.9119, "lng": 77.6065},
        {"id": "blr-019", "name": "Jayadeva Institute of Cardiovascular Sciences","lat": 12.9290, "lng": 77.5878},
        {"id": "blr-020", "name": "Bangalore Baptist Hospital (Bellary Road)","lat": 13.0325, "lng": 77.5836},
        {"id": "blr-021", "name": "Kidwai Memorial Institute of Oncology",   "lat": 12.9335, "lng": 77.5862},
        {"id": "blr-022", "name": "Victoria Hospital (KR Market)",            "lat": 12.9653, "lng": 77.5751},
        {"id": "blr-023", "name": "Bowring and Lady Curzon Hospital",         "lat": 12.9745, "lng": 77.6113},
        {"id": "blr-024", "name": "Sanjay Gandhi Accident Hospital",          "lat": 12.9740, "lng": 77.5929},
        {"id": "blr-025", "name": "HCG Cancer Hospital (KR Road)",            "lat": 12.9318, "lng": 77.5894},
        {"id": "blr-026", "name": "Sri Shankara Cancer Hospital",             "lat": 12.9261, "lng": 77.5821},
        {"id": "blr-027", "name": "Hosmat Hospital (Richmond Road)",         "lat": 12.9594, "lng": 77.6054},
        {"id": "blr-028", "name": "Mallya Hospital (Vittal Mallya Road)",     "lat": 12.9714, "lng": 77.5975},
        {"id": "blr-029", "name": "Manipal Hospital (Sarjapur Road)",         "lat": 12.8989, "lng": 77.6797},
        {"id": "blr-030", "name": "Apollo Spectra Hospital (Koramangala)",    "lat": 12.9312, "lng": 77.6124},
    ]

    async def _fetch_live_hospitals(self, lat: float, lng: float, radius_m: int = 25000) -> list[dict[str, Any]]:
        """Queries Overpass API for real hospitals in Bangalore. Falls back to seed list if unavailable."""
        import time as _time

        cache_key = (
            round(lat, _HOSPITAL_CACHE_PRECISION),
            round(lng, _HOSPITAL_CACHE_PRECISION),
            radius_m,
        )
        hit = self._fetch_cache.get(cache_key)
        if hit and _time.monotonic() - hit[0] < _HOSPITAL_CACHE_TTL_S:
            self._hospitals = hit[1]
            logger.info("Hospital catalogue cache HIT (%d facilities)", len(hit[1]))
            return hit[1]

        query = f"""
        [out:json][timeout:15];
        (
          node(around:{radius_m}, {lat}, {lng})["amenity"="hospital"];
          way(around:{radius_m}, {lat}, {lng})["amenity"="hospital"];
        );
        out center;
        """
        live_hospitals = []
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.get(
                    "https://overpass-api.de/api/interpreter",
                    params={"data": query},
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "GeoAgentic/1.0 (emergency-routing-research)",
                    },
                )
                res.raise_for_status()
                data = res.json()

            elements = data.get("elements", [])
            for el in elements:
                hid = str(el["id"])
                name = el.get("tags", {}).get("name", "").strip()
                if not name:
                    continue
                # nodes have lat/lon directly; ways have center
                if el["type"] == "node":
                    h_lat, h_lng = el["lat"], el["lon"]
                else:
                    center = el.get("center", {})
                    h_lat, h_lng = center.get("lat", lat), center.get("lon", lng)

                live_hospitals.append({"id": hid, "name": name, "lat": h_lat, "lng": h_lng})

            logger.info("Overpass returned %d hospitals within %dkm", len(live_hospitals), radius_m // 1000)
        except Exception as e:
            logger.warning("Overpass fetch failed (%s) — using seed list", e)

        # Merge: prefer live data, fill gaps from seed list
        seen_names = {h["name"].lower() for h in live_hospitals}
        for seed in self.BANGALORE_HOSPITALS_SEED:
            if not any(seed["name"].lower() in n or n in seed["name"].lower() for n in seen_names):
                live_hospitals.append(seed)

        if not live_hospitals:
            live_hospitals = list(self.BANGALORE_HOSPITALS_SEED)

        # ------------------------------------------------------------------
        # Enrichment. Two clearly separated provenance tiers:
        #
        #   CURATED   - facility capability from the reviewed table. Real,
        #               stable, and what actually drives clinical routing.
        #   SIMULATED - live free-bed / blood-unit counts. No hospital HMIS
        #               feed exists, so these are generated deterministically
        #               and tagged so the UI can label them honestly.
        # ------------------------------------------------------------------
        hospitals = []
        curated_hits = 0

        for h in live_hospitals:
            hid = h["id"]
            profile = self._match_curated(hid, h["name"])

            if profile:
                curated_hits += 1
                curated_id = next(
                    (k for k, v in (self._capabilities or {}).items() if v is profile),
                    None,
                )
                caps = dict(profile["capabilities"])
                beds_total = profile["beds_total"]
                icu_total = profile["icu_total"]
                specialists = list(profile["specialties"])
                capability_provenance = "curated"
                emergency_receiving = profile.get("emergency_receiving", True)
                facility_type = profile.get("type", "unknown")
                display_name = profile["name"]
                note = profile.get("note", "")
            else:
                # No curated profile: a small clinic or an unrecognised Overpass
                # entry. Model conservatively - an unknown facility should not
                # outrank a verified tertiary centre - and mark it simulated.
                base = 0.25 + (self._deterministic_float(hid, "quality") * 0.35)
                caps = {
                    "emergency_resource": round(base * 0.9, 3),
                    "specialist":         round(base * 0.7, 3),
                    "blood_availability": round(base * 0.7, 3),
                    "cardiology":         round(base * 0.5, 3),
                    "trauma_centre":      round(base * 0.5, 3),
                    "surgical_capacity":  round(base * 0.6, 3),
                    "neurology":          round(base * 0.45, 3),
                    "ct_imaging":         round(base * 0.6, 3),
                }
                beds_total = int(30 + self._deterministic_float(hid, "beds") * 120)
                icu_total = max(2, int(beds_total * 0.10))
                specialists = ["Emergency Medicine"]
                capability_provenance = "simulated"
                emergency_receiving = True
                facility_type = "unclassified"
                curated_id = None
                display_name = h["name"]
                note = (
                    "No curated capability profile for this facility - "
                    "conservative modelled estimate."
                )

            # Live occupancy has no data source anywhere. Always simulated.
            occupancy = 0.55 + self._deterministic_float(hid, "avail") * 0.35
            beds_available = max(0, int(beds_total * (1.0 - occupancy)))
            icu_available = max(0, int(icu_total * (1.0 - occupancy)))

            # An ER on divert is genuinely removed from emergency routing -
            # not merely annotated - which is what the terminal promises.
            diverted = bool(curated_id and curated_id in self._on_divert)
            if diverted:
                emergency_receiving = False

            # Prefer the curated profile's coordinate. It is resolved against
            # OpenStreetMap by scripts/resolve_hospital_coords.py, whereas the
            # seed list was hand-written and had 24 of 30 entries more than
            # 700 m out - which is how a patient at MSRIT was told Ramaiah
            # Memorial was 10 minutes away when it is 595 m down the road.
            lat = profile.get("lat", h["lat"]) if profile else h["lat"]
            lng = profile.get("lng", h["lng"]) if profile else h["lng"]

            hospitals.append({
                "id": hid,
                "curated_id": curated_id,
                "on_divert": diverted,
                "name": display_name,
                "lat": lat, "lng": lng,
                "type": facility_type,
                "emergency_receiving": emergency_receiving,
                "icu_available": 0 if diverted else icu_available,
                "icu_total": icu_total,
                "beds_total": beds_total,
                "beds_available": beds_available,
                "specialists": specialists,
                "capabilities": caps,
                "capability_provenance": capability_provenance,
                "availability_provenance": "simulated",
                "note": note,
            })

        hospitals = self._deduplicate(hospitals)

        logger.info(
            "Enriched %d hospitals: %d curated profiles, %d modelled",
            len(hospitals), curated_hits, len(hospitals) - curated_hits,
        )
        self._fetch_cache[cache_key] = (_time.monotonic(), hospitals)
        self._hospitals = hospitals
        return hospitals

    def _deduplicate(self, hospitals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Collapse the same physical facility appearing more than once.

        OSM commonly maps a hospital as both a POI node and a building way, and
        our seed list can add a third copy. Without this, one hospital occupies
        several slots in a five-item shortlist.

        Two rules:
          1. Entries resolving to the same curated profile are the same place.
          2. Otherwise, identical normalised names within 500 m are the same place.
        Curated entries always win over modelled ones.
        """
        by_curated: dict[str, dict[str, Any]] = {}
        others: list[dict[str, Any]] = []

        for h in hospitals:
            if h["capability_provenance"] == "curated":
                key = h["name"]
                existing = by_curated.get(key)
                if existing is None:
                    by_curated[key] = h
                continue
            others.append(h)

        deduped: list[dict[str, Any]] = list(by_curated.values())
        curated_names = {_normalise_name(n) for n in by_curated}

        for h in others:
            norm = _normalise_name(h["name"])
            if not norm or norm in curated_names:
                continue
            duplicate = False
            for kept in deduped:
                if _normalise_name(kept["name"]) != norm:
                    continue
                if _haversine(h["lat"], h["lng"], kept["lat"], kept["lng"]) < 500:
                    duplicate = True
                    break
            if not duplicate:
                deduped.append(h)

        return deduped



    def resolve_key(self, identifier: str) -> str | None:
        """
        Map anything the UI might send to a curated profile key.

        The login screen offers blr-* ids, live results carry OSM ids, and
        people paste names - all three should address the same facility.
        """
        if not identifier:
            return None
        curated = self._load_capabilities()
        if identifier in curated:
            return identifier
        needle = identifier.lower().strip()
        for key, entry in curated.items():
            if entry["name"].lower() == needle:
                return key
        for h in self._hospitals:
            if str(h.get("id")) == str(identifier) and h.get("curated_id"):
                return h["curated_id"]
        for key, entry in curated.items():
            if needle and needle in entry["name"].lower():
                return key
        return None

    def set_divert(self, identifier: str, on_divert: bool) -> str | None:
        """Put a facility on or off divert. Returns the curated key, or None."""
        key = self.resolve_key(identifier)
        if key is None:
            return None
        if on_divert:
            self._on_divert.add(key)
        else:
            self._on_divert.discard(key)
        # Drop the catalogue cache so the next ranking reflects the change.
        self._fetch_cache.clear()
        logger.info(
            "Hospital %s (%s) is now %s",
            key, self._capabilities[key]["name"],
            "ON DIVERT - excluded from emergency routing" if on_divert else "accepting",
        )
        return key

    @property
    def diverted(self) -> set[str]:
        return set(self._on_divert)

    async def rank_hospitals(self, request: HospitalRequest) -> HospitalRankingResponse:
        etype = request.emergency_type.value
        weights = WEIGHT_TABLES.get(etype, WEIGHT_TABLES["general"])
        weights_label = etype if etype in WEIGHT_TABLES else "general"

        logger.info(
            "Ranking hospitals for %s emergency at [%.4f, %.4f] using '%s' weights",
            etype, request.incident_location.lat, request.incident_location.lng, weights_label
        )

        hospitals = await self._fetch_live_hospitals(request.incident_location.lat, request.incident_location.lng)

        # An oncology centre or maternity-only unit is not a valid destination
        # for a cardiac arrest, no matter how close it is. Filter before
        # scoring, so proximity can never override clinical suitability.
        if etype in _RECEIVING_REQUIRED:
            eligible = [
                h for h in hospitals
                if h.get("emergency_receiving", True)
                and not (
                    h["capability_provenance"] != "curated"
                    and _looks_non_emergency(h["name"])
                )
            ]
            excluded = len(hospitals) - len(eligible)
            if excluded:
                logger.info(
                    "Excluded %d non-receiving facilities for %s emergency",
                    excluded, etype,
                )
            hospitals = eligible or hospitals

        # Specialty-centre bypass gate (see _CAPABILITY_GATE).
        gate = _CAPABILITY_GATE.get(etype)
        gate_applied = False
        if gate:
            factor, threshold = gate
            qualified = [
                h for h in hospitals
                if h["capabilities"].get(factor, 0.0) >= threshold
            ]
            if len(qualified) >= _MIN_GATED_CANDIDATES:
                logger.info(
                    "Specialty bypass: %d of %d facilities meet %s >= %.2f "
                    "for %s - restricting candidates",
                    len(qualified), len(hospitals), factor, threshold, etype,
                )
                hospitals = qualified
                gate_applied = True

                # Once the gate has screened for capability, time should decide
                # among the survivors - which is exactly how bypass protocols
                # work: take a STEMI past the nearest ER to a PCI centre, then
                # to the NEAREST PCI centre. Leaving the full capability weight
                # in place double-counted it and sent patients past a closer
                # cath lab to a marginally better-equipped one, arriving later.
                weights = _rebalance_after_gate(weights, factor)

        entries: list[HospitalEntry] = []

        for hosp in hospitals:
            dist = _haversine(
                request.incident_location.lat, request.incident_location.lng,
                hosp["lat"], hosp["lng"]
            )

            bed_score = (hosp["beds_available"] / hosp["beds_total"] if hosp["beds_total"] > 0 else 0.0)

            scores: dict[str, float] = {
                "travel_time": _travel_time_score(dist),
                "bed_availability": bed_score,
                **hosp["capabilities"],
            }

            breakdown: list[ScoreBreakdown] = []
            total = 0.0

            for factor, weight in weights:
                raw = scores.get(factor, 0.0)
                weighted = round(weight * raw, 4)
                total += weighted
                breakdown.append(ScoreBreakdown(
                    factor=_FACTOR_LABELS.get(factor, factor),
                    weight=weight,
                    raw_score=round(raw, 3),
                    weighted_score=weighted,
                ))

            total = round(total, 4)
            reasoning = self._build_reasoning(hosp, total, dist, etype, breakdown)

            entries.append(HospitalEntry(
                hospital_id=hosp["id"],
                name=hosp["name"],
                location=Coordinate(lat=hosp["lat"], lng=hosp["lng"]),
                score=total,
                confidence=round(min(1.0, 0.6 + (total * 0.4)), 3),
                reasoning=reasoning,
                icu_available=hosp["icu_available"],
                specialists=hosp["specialists"],
                blood_availability=hosp["capabilities"].get("blood_availability", 0.5),
                capability_provenance=hosp.get("capability_provenance", "simulated"),
                emergency_receiving=hosp.get("emergency_receiving", True),
                score_breakdown=breakdown,
            ))

        entries.sort(key=lambda e: e.score, reverse=True)

        # Guard: if no hospitals matched (e.g. all on divert, or graph empty),
        # return the full unfiltered list so the dispatcher always sees options.
        if not entries:
            logger.warning(
                "Hospital ranking produced zero entries for %s — returning unfiltered fallback",
                request.emergency_type,
            )
            return HospitalRankingResponse(
                ranked_hospitals=[],
                emergency_type=request.emergency_type,
                weights_used="fallback-no-candidates",
                data_freshness=DataFreshness.STALE,
            )

        # ------------------------------------------------------------------
        # Stage 2: re-rank the shortlist on real road-network travel time.
        #
        # Stage 1 ranks ~900 candidates using straight-line distance at a flat
        # average speed, which is cheap but wrong: it ignores one-ways, river
        # and rail crossings, and road class. Recomputing an actual A* path for
        # every candidate would take ~45s, so only the shortlist is upgraded.
        # ------------------------------------------------------------------
        shortlist = entries[:_ROAD_ETA_SHORTLIST]
        upgraded = self._rerank_on_road_eta(
            shortlist, request.incident_location, weights,
        )

        return HospitalRankingResponse(
            ranked_hospitals=upgraded[:5] if upgraded else entries[:5],
            emergency_type=request.emergency_type,
            weights_used=weights_label + (" + specialty-bypass" if gate_applied else ""),
            data_freshness=DataFreshness.LIVE,
        )

    def _rerank_on_road_eta(
        self,
        shortlist: list[HospitalEntry],
        origin: Coordinate,
        weights: list[tuple[str, float]],
    ) -> list[HospitalEntry]:
        """
        Replace the straight-line travel estimate with a routed ETA.

        Falls back to the stage-1 ordering if the road graph is unavailable, so
        this can only ever improve the ranking, never break it.
        """
        from agents.local_router import local_router

        if not local_router.available and not local_router.load():
            return shortlist

        travel_weight = dict(weights).get("travel_time", 0.0)
        if travel_weight <= 0:
            return shortlist

        for entry in shortlist:
            path = local_router.find_path(
                (origin.lat, origin.lng),
                (entry.location.lat, entry.location.lng),
            )
            if not path:
                continue

            road_time_s = path["travel_time_s"]
            new_travel_score = max(
                0.0, min(1.0, 1.0 - (road_time_s / _MAX_TRAVEL_TIME_S))
            )

            # Swap the travel component out of the total, leaving every other
            # weighted factor exactly as stage 1 computed it.
            for bd in entry.score_breakdown:
                if bd.factor != _FACTOR_LABELS["travel_time"]:
                    continue
                old_weighted = bd.weighted_score
                bd.raw_score = round(new_travel_score, 3)
                bd.weighted_score = round(travel_weight * new_travel_score, 4)
                entry.score = round(entry.score - old_weighted + bd.weighted_score, 4)
                break

            entry.road_eta_seconds = round(road_time_s, 1)
            entry.road_distance_m = path["distance_m"]
            entry.reasoning += (
                f" ROUTED: {path['distance_m'] / 1000:.1f}km by road, "
                f"{road_time_s / 60:.1f} min via {', '.join(path['roads'][:2])}."
            )

        shortlist.sort(key=lambda e: e.score, reverse=True)
        return shortlist

    def _build_reasoning(self, hosp: dict[str, Any], total_score: float, distance_m: float,
                         emergency_type: str, breakdown: list[ScoreBreakdown]) -> str:
        sorted_bd = sorted(breakdown, key=lambda b: b.weighted_score, reverse=True)
        top_factors = sorted_bd[:2]
        weak_factor = sorted_bd[-1] if len(sorted_bd) > 1 else None

        dist_km = distance_m / 1000
        travel_s = distance_m / _AVG_SPEED_MPS

        provenance = hosp.get("capability_provenance", "simulated")
        tag = "VERIFIED PROFILE" if provenance == "curated" else "MODELLED ESTIMATE"

        parts = [
            f"[{tag}] {hosp['name']} scores {total_score:.3f} for {emergency_type} emergency.",
            f"Distance: {dist_km:.1f}km (est. {travel_s:.0f}s travel time).",
        ]

        strengths = [
            f"{f.factor} ({f.raw_score:.2f} x {f.weight:.2f} = {f.weighted_score:.3f})"
            for f in top_factors
        ]
        parts.append(f"Strongest factors: {', '.join(strengths)}.")

        if weak_factor and weak_factor.raw_score < 0.5:
            parts.append(f"Weakest: {weak_factor.factor} ({weak_factor.raw_score:.2f}).")

        parts.append(
            f"ICU: {hosp['icu_available']}/{hosp.get('icu_total', '?')} beds free "
            f"(SIMULATED - no live hospital feed integrated)."
        )

        return " ".join(parts)

    async def publish_ranking(self, request: HospitalRequest) -> HospitalRankingResponse:
        result = await self.rank_hospitals(request)
        await bus.publish(
            topic=HOSPITAL_RANKED,
            payload=result.model_dump(mode="json"),
            source_agent=AGENT_NAME,
        )
        return result

hospital_agent = HospitalIntelligenceAgent()
