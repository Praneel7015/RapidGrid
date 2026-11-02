# RapidGrid — GeoAgentic emergency vehicle routing

[![CI](https://github.com/Praneel7015/RapidGrid/actions/workflows/ci.yml/badge.svg)](https://github.com/Praneel7015/RapidGrid/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Praneel7015/RapidGrid/actions/workflows/codeql.yml/badge.svg)](https://github.com/Praneel7015/RapidGrid/actions/workflows/codeql.yml)
[![codecov](https://codecov.io/gh/Praneel7015/RapidGrid/branch/main/graph/badge.svg)](https://codecov.io/gh/Praneel7015/RapidGrid)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/Praneel7015/RapidGrid/badge)](https://scorecard.dev/viewer/?uri=github.com/Praneel7015/RapidGrid)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Praneel7015/RapidGrid/main.svg)](https://results.pre-commit.ci/latest/github/Praneel7015/RapidGrid/main)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A multi-agent system that routes emergency vehicles in Bengaluru on what a
hospital can actually **treat**, not just how close it is — and shows the
dispatcher exactly what each decision traded away.

**The thesis:** the nearest hospital is often the wrong one. A cardiac arrest
needs a cath lab. Sending the patient to a closer emergency room that has to
transfer them onward costs far more time than driving past it.

> **On honesty:** parts of this system use live data and parts are modelled.
> Every screen distinguishes the two, and so does this README — see
> [What is real and what is modelled](#what-is-real-and-what-is-modelled).
> Nothing is presented as measured unless it is.

Try our Application ---- https://rapidgrid.onrender.com

---

## What it does

### Routes on clinical capability, not proximity

Hospital capability profiles are curated for 30 Bengaluru facilities — cath
lab, stroke unit, trauma centre, blood bank. A **specialty-bypass gate**
screens candidates on capability before proximity, mirroring real EMS
protocol: take a STEMI past the nearest ER to a PCI centre, then to the
*nearest* PCI centre. Oncology, maternity and day-surgery units are excluded
from emergency routing entirely.

### Explains the decision, including what it rejected

The Decision Fusion Engine scores five weighted dimensions and emits a genuine
counterfactual — which hospital lost, on which factor, and what the choice
cost in road minutes:

> *St John's scores better on travel time (0.85 vs 0.64), but Fortis wins on
> cath-lab capability (0.90 vs 0.76) and takes it by 0.017. By road, Fortis is
> 6.2 min further.*

### Measures outcome, not minutes

Time-to-definitive-treatment against door-to-balloon (90 min), door-to-needle
(60 min) and golden-hour targets. The dominant term is the **secondary
transfer** — a hospital that cannot deliver the intervention costs a whole
second journey:

```
                          PCI CENTRE      NEAREST ER
  drive to hospital             16.0             5.0
  in-hospital workup            27.2            34.6
  secondary transfer             0.0            52.0
  TOTAL to balloon              60.7           109.1
  within 90 min target          True           False
```

### Keeps working when the internet does not

Routing runs in three tiers, and never pretends:

| Tier | Source | Freshness | Used when |
| :-- | :-- | :-- | :-- |
| 1 | Google Routes API v2 | `LIVE` | key configured and reachable |
| 2 | A* over a cached 112,725-node OpenStreetMap graph | `CACHED` | quota, outage, or no key |
| 3 | Straight-line placeholder | `STALE` | both failed — explicitly *not dispatchable* |

Tier 2 produces real paths over real Bengaluru roads in ~100 ms. Tier 3 says
`NO ROUTE AVAILABLE — do not dispatch on this geometry` rather than returning
a confident-looking zero.

### Models the city causally

`agents/city_state.py` is a **structural causal model** over the road graph —
mechanisms are specified from domain knowledge, not learned from data. We have
no historical incident corpus to identify effects from, and the module says so.

What that buys is intervention and counterfactual reasoning: in the real world
you cannot run `do(close Bellary Road)`; inside a model we own, you can.

```
TimeOfDay ─┐
Weather ───┼──▶ Congestion(edge) ──▶ ETA(route) ──▶ TimeToTreatment
Closure ───┘         │  ▲
   │                 │  └── displacement ──┐
   └── do() ─────────┘                     │
                                neighbouring edges
```

The load-bearing edge is **displacement**. Traffic on a closed road does not
evaporate — it reappears on parallel streets, which congest, which slows a
*different* ambulance elsewhere:

```
do(close Bellary Road) -> 52 segments closed, displaced onto 97 neighbours

  used the closed road             28.8 -> 33.9 min  (+5.1)
  parallel corridor (untouched)    13.1 -> 16.0 min  (+2.9)   <- never used it
  across town                      24.4 -> 24.4 min  (+0.0)   <- correctly unaffected
```

---

## Agents

| Agent | Responsibility |
| :-- | :-- |
| **Triage** (`triage.py`) | Weighted-keyword classification of free-text reports into emergency type and severity. Transparent and auditable — not a learned model, and never described as one. |
| **Accessibility** | Normalises multi-modal reports (text, voice, sign language, SOS button) into a standard incident contract. |
| **Traffic Intelligence** | Segment congestion, incidents, closures, weather risk. |
| **Route Optimization** | Three-tier routing with a 90 s response cache to protect API quota. |
| **Local Router** (`local_router.py`) | A* over the cached OpenStreetMap graph, implementing the `route-scoring.md` cost model. |
| **City State** (`city_state.py`) | Causal model: time of day, weather, closures, and traffic displacement. |
| **Hospital Intelligence** | Two-stage ranking — cheap scoring across ~900 candidates, then real road ETAs for the shortlist. Specialty-bypass gate. |
| **Clinical Outcome** (`clinical_outcome.py`) | Time-to-treatment against guideline targets, including transfer penalties. |
| **Decision Fusion** | Five-dimension weighted synthesis with an explicit counterfactual. |
| **Prediction** | ETA with weather adjustment and a confidence interval. |
| **Communication** | Dispatch notifications (SMS, hospital API, traffic control channels). |
| **Emergency Coordinator** | Severity assessment and workflow activation. |
| **Learning** | **Stub.** Logs decisions; does not yet feed anything back. `/health` reports it as `stub_active`. |

Agents communicate over an in-process async event bus (`bus/event_bus.py`).

---

## What is real and what is modelled

This distinction is enforced in the UI — verified values are drawn solid,
modelled values carry hatched badges and dotted underlines.

**Real**

- Bengaluru road network — 112,725 nodes / 178,089 edges from OpenStreetMap
- Live traffic routing via Google Routes API v2
- Live weather via OpenWeather, factored into ETA
- Hospital **capability** profiles, curated for 30 facilities
- Hospital **coordinates**, resolved against OpenStreetMap — each carries a
  `coord_source` of `openstreetmap`, `manual-reviewed` or `manual-unverified`
- A* pathfinding, honouring closures and congestion

**Modelled — and labelled as such everywhere**

- Live ICU beds and blood units. No hospital HMIS or HL7 feed is integrated.
- Ambulance fleet positions. Station hubs are fixed, not GPS-tracked.
- Triage classification — transparent keyword scoring, not a trained model.
- Traffic displacement coefficients. Literature-informed estimates, **not
  calibrated** against Bengaluru traffic counts. Directionally sound;
  magnitudes unvalidated.
- Clinical process times — from published guideline targets, not measured
  from live hospital systems.
- One hospital coordinate — Columbia Asia Hebbal, since rebranded to Manipal
  Hebbal — has no confident OpenStreetMap entry and is tagged
  `manual-unverified`. It is the least-trustworthy of the 30 and labelled so.
- Capability **scores** themselves (cath-lab strength, trauma rating) are
  informed judgement, not sourced per-hospital. Directionally sound; the
  coordinates have citable provenance, these do not yet.

**Not implemented**

- Authentication. Selecting a role grants it, with no credential check. The
  login screen states this. Real auth is the first thing a production
  deployment needs.
- Multi-incident resource contention. Each incident assumes an available unit.

---

## Architecture

```
[ Citizen SOS ]
      │
      ▼
[ Accessibility ] ──▶ [ Triage ] ──▶ [ Emergency Coordinator ]
                                              │
                                              ▼
                                  [ Decision Fusion Engine ]
              ┌───────────────┬───────────────┼───────────────┐
              ▼               ▼               ▼               ▼
        [ Traffic ]     [ Route Opt ]   [ Hospital ]   [ Clinical  ]
        [  Intel   ]    [  3 tiers  ]   [  Intel    ]  [  Outcome  ]
              │               │               │               │
              │        [ City State ]         │               │
              │        [  causal    ]         │               │
              └───────────────┴───────────────┴───────────────┘
                                              ▼
                                  [ Dispatcher Approval ]
                                              │
                    ┌─────────────────────────┴─────────────────────────┐
                    ▼                                                   ▼
        [ Driver field app ]                                [ Hospital ER terminal ]
        [ Stage 1 → Stage 2 ]                               [ capacity / divert    ]
                    └────────────────[ incident channel ]────────────────┘
```

---

## Repository layout

```
RapidGrid/
├── DEMO.md                              # Demo runbook — every command verified
├── backend/
│   ├── main.py                          # FastAPI entry point, lifespan, WebSocket
│   ├── agents/
│   │   ├── accessibility.py             # Multi-modal input normalisation
│   │   ├── city_state.py                # Structural causal model of the city
│   │   ├── clinical_outcome.py          # Time-to-treatment estimation
│   │   ├── communication.py             # Multi-channel dispatch
│   │   ├── coordinator.py               # Incident classification
│   │   ├── decision_fusion.py           # 5-factor fusion + counterfactual
│   │   ├── hospital_intelligence.py     # Two-stage ranking, specialty gate
│   │   ├── learning.py                  # Stub
│   │   ├── local_router.py              # A* over the OpenStreetMap graph
│   │   ├── prediction.py                # ETA + weather adjustment
│   │   ├── route_optimization.py        # 3-tier routing + quota cache
│   │   ├── traffic_intelligence.py      # Segment congestion
│   │   └── triage.py                    # Weighted-keyword classifier
│   ├── bus/event_bus.py                 # Async in-process pub/sub
│   ├── models/schemas.py                # Pydantic contracts
│   ├── routers/                         # FastAPI routers, one per domain
│   │   ├── chat.py  city.py  driver.py  hospital.py  incident.py  …
│   ├── data/
│   │   ├── bengaluru_road_graph.json.gz # 112k-node graph (built, committed)
│   │   ├── hospital_capabilities.json   # Curated capability profiles
│   │   └── sample_road_network.json
│   ├── scripts/
│   │   ├── build_road_graph.py          # Fetch + compact the OSM graph
│   │   ├── preflight.py                 # Pre-demo health check
│   │   ├── resolve_hospital_coords.py   # Resolve coordinates against OSM
│   │   ├── validate_against_apis.py     # Cross-check data vs Google + OSM
│   │   └── simulate_workflow.py
│   ├── tests/test_scenarios.py          # 30 behavioural tests
│   ├── requirements.txt
│   └── requirements-dev.txt
└── frontend/
    └── src/
        ├── App.jsx                      # Routes + demo role gate (not auth)
        ├── index.css                    # Design tokens, provenance styling
        ├── context/AuthContext.jsx      # Role/session state
        ├── components/
        │   ├── AIRationale.jsx          # Trade-off + outcome panel
        │   ├── CityControls.jsx         # Disruption interventions
        │   ├── LiveEmergencyChat.jsx    # Per-incident channel
        │   ├── MapOverlay.jsx           # Routes, markers, closures, congestion
        │   ├── SharedShell.jsx          # Portal frame + live health
        │   └── ui.jsx                   # Shared primitives
        └── views/                       # Landing, Login + 4 role portals
```

---

## Getting started

**Prerequisites:** Python 3.11+, Node.js 18+

### Backend

```bash
cd backend && python -m venv venv && ./venv/Scripts/activate && pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your keys:

```
GOOGLE_ROUTES_API_KEY=...      # backend key — Application restrictions MUST be None
OPENWEATHER_API_KEY=...
GOOGLE_MAPS_JS_API_KEY=...     # browser key — restrict to http://localhost:5173/*
```

> The Routes key must **not** carry an HTTP-referrer restriction — that
> silently rejects every server-side call. Enable **Routes API** in Google
> Cloud Console and link a billing account; it 403s without one even on the
> free tier.

Only **Routes API** is required. Geocoding, Places, Distance Matrix and Roads
are not used and currently return `REQUEST_DENIED` on this project — coordinate
verification runs against OpenStreetMap instead, via
`scripts/resolve_hospital_coords.py`.

Build the road graph once (~30 s):

```bash
cd backend && ./venv/Scripts/python.exe scripts/build_road_graph.py
```

Verify everything before you rely on it:

```bash
cd backend && ./venv/Scripts/python.exe scripts/preflight.py
```

Checks all three keys, both routing tiers, the graph, the capability table and
OpenAPI generation. The app runs without any keys — it degrades to the offline
router and says so.

To check the data rather than the wiring:

```bash
cd backend && ./venv/Scripts/python.exe scripts/validate_against_apis.py
```

Cross-validates every hospital and hub coordinate against both the local
OpenStreetMap graph and Google Routes — snap distance to the nearest real road,
whether Google can route there, and whether the two networks agree on distance.
Currently 0 problems across 30 hospitals and 11 hubs, with distance agreement
of 1–15%.

```bash
cd backend && ./venv/Scripts/python.exe -m uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend && npm install && npm run dev
```

Runs at `http://localhost:5173`. API calls go through the Vite proxy
(`/api`, `/health`, `/ws`), so there is no backend URL hardcoded in the client.

### Tests

```bash
cd backend && pip install -r requirements-dev.txt && ./venv/Scripts/python.exe -m pytest tests/ -q
```

Lint and format with [ruff](https://docs.astral.sh/ruff/) (`ruff check . && ruff format --check .`
from the repo root), or install the git hooks once with `pip install pre-commit && pre-commit install`.
CI runs ruff, mypy (advisory), the test suite with coverage, a frontend build, CodeQL and a gitleaks
secret scan on every pull request.

Tests covering triage, offline routing, causal displacement, outcome
estimation, the capability table and hospital coordinate integrity. None
require network access.

---

## Portals

| Portal | Path | Purpose |
| :-- | :-- | :-- |
| Citizen | `/citizen` | Report an emergency, track both response phases, message the crew |
| City dispatch | `/dispatcher` | Review the plan, see the trade-off, override, dispatch, inject disruptions |
| Field unit | `/driver` | Two-stage navigation, mid-transport hospital change |
| Emergency department | `/hospital` | Inbound patients, ICU capacity and divert |

---

## API

Interactive docs at `http://localhost:8000/docs`.

**Incidents** `POST /api/incidents` · `GET /api/incidents/` · `GET /api/incidents/{id}` ·
`POST /api/incidents/{id}/approve` · `POST /api/incidents/{id}/arrived` ·
`POST /api/incidents/{id}/request_ambulance` · `POST /api/incidents/clear`

**Driver** `GET /api/driver/active` · `POST /api/driver/claim` ·
`POST /api/driver/arrived_pickup` · `POST /api/driver/change_hospital`

**Hospital** `POST /api/hospital/rank` · `GET /api/hospital/list` ·
`GET /api/hospital/{id}/incoming` · `PATCH /api/hospital/{id}/resources`
(divert genuinely removes the facility from routing)

**City model** `GET /api/city/state` · `GET /api/city/overlay` ·
`GET /api/city/roads` · `POST /api/city/close` · `POST /api/city/incident` ·
`POST /api/city/conditions` · `GET /api/city/explain/{edge_id}` ·
`POST /api/city/clear`

**Chat** `GET /api/chat/{id}/messages` · `POST /api/chat/{id}/send` ·
`WS /ws/chat/{id}` — one channel per incident, shared by all roles.
WebSocket delivers; HTTP is the durable write path and the fallback.

**Agents** `POST /api/fusion/combine` · `POST /api/route/compute` ·
`POST /api/traffic/analyse` · `POST /api/accessibility/report` ·
`POST /api/coordinator/coordinate` · `POST /api/communication/dispatch` ·
`POST /api/learning/dispatch_decision`

**Platform** `GET /health` — includes routing tier status, quota telemetry and
live city-model state.

---

## Technology

**Frontend** React 18, Vite, Tailwind CSS v4, Leaflet, Lucide
**Backend** FastAPI, Pydantic v2, asyncio, Uvicorn, httpx
**Data** OpenStreetMap (Overpass), Google Routes API v2, OpenWeather
**State** In-memory with JSON snapshotting (`incidents_db.json`, gitignored —
it is per-demo runtime state, not source). Not a production datastore; there
is no concurrency control.

---

## Known limitations

Stated plainly, because a system that hides these is harder to trust:

1. **No authentication.** Role selection grants the role.
2. **Incident state is in-memory**, snapshotted to JSON. No locking, no
   transactions, lost on a crash between writes.
3. **Traffic coefficients are uncalibrated.**
4. **No live hospital data.** Capability is curated; availability is modelled.
   Coordinates are OSM-resolved, but capability *scores* are still judgement
   calls without a per-hospital citation.
5. **The Learning Agent is a stub.**
6. **Single-incident assumption.** No contention for a shared fleet.
7. **`compute_route` is synchronous** inside an async pipeline; a slow upstream
   call blocks the event loop. The response cache limits the exposure.

---

## License

Developed for the RapidGrid / GeoAgentic emergency response platform.
