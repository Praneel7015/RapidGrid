# RapidGrid — GeoAgentic emergency vehicle routing

A dispatch-workflow simulator for Bengaluru. A citizen reports an emergency,
keyword triage suggests a category, hospitals are ranked on what they can
actually **treat** and on real road time, a dispatcher approves or overrides,
and the driver and the receiving ER follow the case — with the reasoning
shown at every step.

**The thesis:** the nearest hospital is often the wrong one. A cardiac arrest
needs a cath lab. Sending the patient to a closer emergency room that has to
transfer them onward costs far more time than driving past it.

> **On honesty:** parts of this system use live data and parts are modelled.
> Every screen distinguishes the two, and so does this README — see
> [What is real and what is modelled](#what-is-real-and-what-is-modelled).
> Nothing is presented as measured unless it is.

> **Hosted showcase:** https://rapidgrid.highcortisol.xyz runs as an open
> demo for the hackathon: no sign-in, synthetic data only. Do not enter real
> names or phone numbers there. Operator mode (below) locks a deployment
> behind an access token when one is needed.

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

### Illustrates the cost of a secondary transfer

An **illustrative** estimate of call-to-treatment time against guideline
targets (90 min to balloon, 60 min to needle, the first hour for trauma). It
uses fixed timing assumptions plus the modelled road time and excludes the
unit's drive to the patient, so it is not a measured or clinically validated
outcome. Its point is the shape: a hospital that cannot deliver the
intervention costs a whole second journey.

Example with an 8-minute drive to the patient (the in-app card leaves this
leg out, because the plan does not know the dispatching unit yet):

```
                                  PCI CENTRE      NEAREST ER
  dispatch and turnout                   2.5             2.5
  drive to patient (example)             8.0             8.0
  on scene                               7.0             7.0
  drive to hospital                     16.0             5.0
  in-hospital workup                    27.2            34.6
  secondary transfer                     0.0            52.0
  TOTAL call to balloon (est.)          60.7           109.1
  first contact to balloon              50.2            98.6
  within 90 min of first contact        True           False
```

Each target is checked against the interval its guideline defines: first
medical contact to balloon for cardiac, hospital door to needle for stroke,
and the whole first hour for trauma.

### Keeps working when the internet does not

Routing runs in three tiers, and never pretends:

| Tier | Source | Freshness | Used when |
| :-- | :-- | :-- | :-- |
| 1 | Google Routes API v2 | `LIVE` | key configured and reachable |
| 2 | A* over a cached 112,725-node OpenStreetMap graph | `CACHED` | quota, outage, or no key |
| 3 | Straight-line placeholder | `STALE` | both failed — explicitly *not dispatchable* |

Tier 2 produces real paths over real Bengaluru roads (measured locally: median
about 180 ms, 99th percentile about 1 s over 200 random routes). Tier 3 says
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

## How a report becomes a plan

The code is organised as modules called "agents", but the decision path is a
plain pipeline:

| Step | Module | What it really does |
| :-- | :-- | :-- |
| Triage | `triage.py`, `accessibility.py` | Weighted keyword matching on English text into emergency type and severity. No negation handling; other languages fall to `general`. A suggestion for the dispatcher, not clinical triage. |
| Hospital ranking | `hospital_intelligence.py` | Scores curated capability, road time over the offline graph, and only ER-desk-entered bed figures. Diverted hospitals are excluded; specialty-bypass gate for cardiac, stroke and trauma. **This step chooses the hospital.** |
| Routing | `route_optimization.py`, `local_router.py` | Google Routes when configured, otherwise A* over the offline graph (see tiers above). |
| Plan | `decision_fusion.py` | Packages the top-ranked hospital, the route and an explanation. Its five weighted factors describe the plan; they do not change which hospital is chosen. |
| City model | `city_state.py` | Hour, weather and drawn closures, applied to the offline graph. Also the source of the traffic factor shown in the plan. |

Supporting modules: `clinical_outcome.py` (illustrative estimate) and
`prediction.py` (weather adjustment when a key is set).
`traffic_intelligence.py` still loads a six-segment sample network at startup
for the route module's legacy tier; it is not used in decisions.

---

## Installable app

RapidGrid is a progressive web app. In Chrome, Edge or Android, use
**Install app** in the address bar or browser menu; on iOS, **Share → Add to
Home Screen**. It then opens in its own window with shortcuts to each portal.

- **Cached:** the app shell (HTML, JavaScript, CSS, icons), fonts, and map tiles
  you have already viewed (up to 400, kept for a week).
- **Never cached:** anything under `/api`, `/ws` or `/health`. Incidents, chat,
  routes and sessions always come from the server, and no caller details are
  stored in the browser cache.
- **Offline:** the app opens and shows a banner saying live data needs a
  connection; it retries the access check when the connection returns.
- **Updates:** a new version waits until the user taps **Reload**, so an open
  call is never interrupted.
- **Not preloaded:** the in-browser speech model (about 21 MB) downloads only
  when dictation is used on a browser without built-in speech recognition.

## What is real and what is modelled

This distinction is enforced in the UI — verified values are drawn solid,
modelled values carry hatched badges and dotted underlines.

**Real**

- Bengaluru road network — 112,725 nodes / 178,089 edges from OpenStreetMap
- Traffic-aware routing via Google Routes API v2, **when a key is configured**
- Weather via OpenWeather, factored into ETA, **when a key is configured**
- Hospital **capability** profiles, curated for 30 facilities
- Hospital **coordinates**, resolved against OpenStreetMap — each carries a
  `coord_source` of `openstreetmap`, `manual-reviewed` or `manual-unverified`
- A* pathfinding, honouring closures and congestion

**Modelled — and labelled as such everywhere**

- ICU beds and blood units. No hospital HMIS or HL7 feed is integrated. Bed
  counts are simulated for display and **never used to rank hospitals**; only
  a figure an ER desk enters on its portal affects the ranking.
- Traffic conditions. There is no live traffic feed; the traffic factor is
  modelled from the hour, the weather and closures drawn by the dispatcher.
- The clinical outcome comparison (see above) — illustrative assumptions.
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

- Per-person authentication and roles. Operator mode protects the whole API
  with one shared access token; demo mode is open. Within either mode any
  user can act in any role and read every caller's details. Real roles are
  the first thing a production deployment needs.
- Notifications. There are no SMS or push messages; crews and hospitals see
  a dispatch on their screen, which refreshes every few seconds.
- Sign-language input and non-English triage.
- Working without a connection. The installed app opens offline (see
  below), but reporting, dispatch, chat and routing all need the server.
- Multi-incident resource contention. Each incident assumes an available unit.

---

## Architecture

```
  Caller (/citizen)
        │  text or voice, confirmed location
        ▼
  ┌─────────────┐   ┌──────────────────────┐   ┌─────────────┐   ┌────────────┐
  │   Triage    │──▶│   Hospital ranking   │──▶│   Routing   │──▶│    Plan    │
  │ keywords →  │   │ capability gate,     │   │ Google live │   │ runner-up, │
  │ type and    │   │ weighted score,      │   │ or A* over  │   │ trade-off, │
  │ severity    │   │ road-time re-rank    │   │ OSM graph   │   │ outcome    │
  └─────────────┘   └──────────▲───────────┘   └──────▲──────┘   └─────┬──────┘
                               │                      │                │
                    ┌──────────┴──────────────────────┴──────┐         ▼
                    │  City model (structural causal model)   │   Dispatcher approves
                    │  hour · weather · closures · displacement│   (/dispatcher)
                    └─────────────────────────────────────────┘         │
                                              ┌─────────────────────────┴──────────┐
                                              ▼                                    ▼
                                    Field unit (/driver)              Emergency dept (/hospital)
                                    hub → patient → hospital          incoming, beds, divert
                                              └────────── incident chat ───────────┘
```

Every incident is planned on the same city model, so a closure or a hospital
divert can change the plans that follow, not only the one that caused it. A
divert removes that hospital from every new plan. A closure changes hospital
ranking, and dispatched routes too when they come from the offline router
(with a Google Routes key, dispatched routes do not see drawn closures).

---

## Repository layout

```
RapidGrid/
├── DEMO.md                              # Demo runbook — every command verified
├── backend/
│   ├── main.py                          # FastAPI entry point, lifespan, WebSocket
│   ├── agents/
│   │   ├── accessibility.py             # Normalises report text for triage
│   │   ├── city_state.py                # Structural causal model of the city
│   │   ├── clinical_outcome.py          # Time-to-treatment estimation
│   │   ├── decision_fusion.py           # Plan assembly + explanation
│   │   ├── hospital_intelligence.py     # Two-stage ranking, specialty gate
│   │   ├── local_router.py              # A* over the OpenStreetMap graph
│   │   ├── prediction.py                # ETA + weather adjustment
│   │   ├── route_optimization.py        # 3-tier routing + quota cache
│   │   ├── traffic_intelligence.py      # Sample network (startup only)
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
│   │   └── validate_against_apis.py     # Cross-check data vs Google + OSM
│   ├── security.py                      # Access modes, sessions, throttle, headers
│   ├── storage.py                       # SQLite: one row per incident and message
│   ├── tests/                           # ~170 backend tests (pytest)
│   ├── requirements.txt
│   └── requirements-dev.txt
└── frontend/
    └── src/
        ├── App.jsx                      # Routes + operator unlock screen
        ├── index.css                    # Design tokens, provenance styling
        ├── context/AuthContext.jsx      # Role/session state
        ├── components/
        │   ├── AIRationale.jsx          # Trade-off + outcome panel
        │   ├── CityControls.jsx         # Disruption interventions
        │   ├── LiveEmergencyChat.jsx    # Per-incident channel
        │   ├── MapOverlay.jsx           # Routes, markers, closures, congestion
        │   ├── PwaStatus.jsx            # Offline banner, update prompt
        │   ├── SharedShell.jsx          # Portal frame + live health
        │   └── ui.jsx                   # Shared primitives
        └── views/                       # Landing, Login + 4 role portals
```

---

## Getting started

**Prerequisites:** Python 3.11+, Node.js 22+

For the protected hosted showcase, persistent storage, and browser regression
checks, follow [Showcase setup and validation](docs/SHOWCASE.md). The default
local demo uses synthetic data and does not verify individual identities.

### Backend

```bash
cd backend && python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your keys:

```
GOOGLE_ROUTES_API_KEY=...      # server-side Routes API key; not a browser-referrer key
OPENWEATHER_API_KEY=...
GOOGLE_MAPS_JS_API_KEY=...     # optional legacy setting; current Leaflet UI does not use it
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
cd backend && python scripts/build_road_graph.py
```

Verify everything before you rely on it:

```bash
cd backend && python scripts/preflight.py
```

Checks all three keys, both routing tiers, the graph, the capability table and
OpenAPI generation. The app runs without any keys — it degrades to the offline
router and says so.

To check the data rather than the wiring:

```bash
cd backend && python scripts/validate_against_apis.py
```

Cross-validates every hospital and hub coordinate against both the local
OpenStreetMap graph and Google Routes — snap distance to the nearest real road,
whether Google can route there, and whether the two networks agree on distance.
Currently 0 problems across 30 hospitals and 11 hubs, with distance agreement
of 1–15%.

```bash
cd backend && python -m uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend && npm install && npm run dev
```

Runs at `http://localhost:5173`. API calls go through the Vite proxy
(`/api`, `/health`, `/ws`), so there is no backend URL hardcoded in the client.

### Tests

```bash
cd backend && pip install -r requirements-dev.txt && python -m pytest tests/ -q
```

Tests cover triage, offline routing, hospital capability, incident transitions,
durable storage, chat, provider readiness and showcase access. No live API keys
are required. [Browser regressions](docs/SHOWCASE.md#validation) also cover the
complete report-to-handover workflow, page refreshes, chat pointer access,
hospital capacity failures and mobile layouts.

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

Interactive docs at `http://localhost:8000/docs`. In operator mode they, and the detail in `/health`, appear only after unlocking.

**Session** `GET /api/session` · `POST /api/session` · `DELETE /api/session`
(operator-mode sign-in with the shared access token)

**Incidents** `POST /api/incidents` · `GET /api/incidents/` · `GET /api/incidents/{id}` ·
`POST /api/incidents/{id}/approve` · `POST /api/incidents/{id}/reject` ·
`POST /api/incidents/{id}/arrived` ·
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

**Inspection** `POST /api/fusion/combine` · `POST /api/route/compute` ·
`POST /api/hospital/rank` — run one pipeline step on its own, for the demo
script and debugging.

**Platform** `GET /health` — routing tier status, quota telemetry and city-model
state · `GET /health/live` — process liveness for the host's health check.

---

## Technology

**Frontend** React 18, Vite, Tailwind CSS v4, Leaflet, Lucide
**Backend** FastAPI, Pydantic v2, asyncio, Uvicorn, httpx
**Data** OpenStreetMap (Overpass), Google Routes API v2, OpenWeather
**State** SQLite in `RAPIDGRID_DATA_DIR`: one row per incident and per chat
message, written off the event loop; a failed write is rolled back. Finished
calls beyond `RAPIDGRID_KEEP_FINISHED` (default 100) are pruned. Run exactly
one application process per data directory.

---

## Access and security

- **Two modes.** *Demo* (the local default) is open, with synthetic data, and
  its API answers only to local host names. *Operator* (the default on Render)
  locks every API call and the chat socket behind a session started with a
  shared access token.
- **Sessions** are HttpOnly, `SameSite=Strict` cookies that expire after 8
  hours. Signing out revokes the session on the server and closes its chat
  connections, so a copied cookie stops working.
- **Wrong tokens are throttled:** 10 per client and 200 in total per 10
  minutes, for the unlock form and `Bearer` headers alike.
- **Hidden until unlock** in operator mode: the API docs and the routing and
  city detail in `/health`.
- **Browser headers:** no framing, `nosniff`, a referrer policy, a
  permissions policy, and HSTS over HTTPS.
- **Input limits** on every field; reports outside Bengaluru are refused;
  hospital and road names are always rendered as text.
- **Checks on every pull request:** about 170 backend and 21 browser tests,
  CodeQL, a gitleaks secret scan and Dependabot.

Details and deployment settings: [docs/SHOWCASE.md](docs/SHOWCASE.md).

---

## What we would build next

1. **Calibrate the city model** against real Bengaluru traffic counts, and
   publish the error.
2. **Plan incidents jointly.** When calls compete for ambulances, assign
   units and hospitals together to minimise total time to treatment.
3. **Show counterfactuals on screen.** When a road closes or a hospital
   diverts, show the old and new plan side by side with the causal reason.
4. **Reposition idle ambulances** ahead of predicted congestion.
5. **One live hospital feed** (HL7 FHIR) in place of the neutral bed value.
6. **Better triage:** negation, Kannada and Hindi detection.
7. **Per-person accounts and roles,** with an audit trail.

---

## Known limitations

Stated plainly, because a system that hides these is harder to trust:

1. **No per-person identity or roles.** One shared token in operator mode;
   open in demo mode.
2. **One process per data directory.** State is held in memory and written
   through to SQLite; a second process would not see the first one's changes.
3. **Traffic coefficients are uncalibrated**, and there is no live traffic feed.
4. **No live hospital data.** Capability is curated; bed counts are simulated
   unless an ER desk enters them. Capability *scores* are judgement calls
   without a per-hospital citation.
5. **Triage is English keyword matching** without negation handling.
6. **No notifications.** No SMS or push; screens refresh every few seconds.
7. **Single-incident assumption.** No contention for a shared fleet.
8. **With a Google key, dispatched routes do not see drawn closures**, while
   hospital ranking (on the offline graph) does. Leave the key unset to keep
   the two consistent.

---

## License

Developed for the RapidGrid / GeoAgentic emergency response platform.
