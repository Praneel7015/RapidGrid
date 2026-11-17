
# RapidGrid — demo runbook

A GeoAgentic framework for emergency vehicle movement in Bengaluru. This is the
script for demonstrating it end to end: every role, every API call, and the
evidence that nothing on screen is invented.

**The one-line thesis:** the nearest hospital is often the wrong one, and this
system can say exactly why it drove past it.

---

## 0. Before you start (10 minutes)

### Start the stack

```bash
cd backend && ./venv/bin/python -m uvicorn main:app --reload --port 8000
```

Commands in this runbook use `./venv/bin/python` (Linux and macOS). On
Windows, use `.\venv\Scripts\python.exe` instead.

```bash
cd frontend && npm run dev
```

Frontend on `http://localhost:5173`, backend on `http://localhost:8000`.
The frontend calls the backend through Vite's proxy (`/api`, `/health`, `/ws`),
so there is no hardcoded backend URL anywhere in the client.

### Preflight — run this every single time

```bash
cd backend && ./venv/bin/python scripts/preflight.py
```

Checks all three API keys, both routing tiers, the road graph, the hospital
capability table, and OpenAPI schema generation in about 15 seconds. **Green
before you present.** A warning on Google Routes is survivable (the offline
router covers it); a FAIL is not.

Then cross-check the data itself against the external APIs:

```bash
cd backend && ./venv/bin/python scripts/validate_against_apis.py
```

Preflight answers "is everything switched on". This answers "does what we
believe agree with Google and OpenStreetMap" — for all 30 hospitals and 11
emergency hubs it checks how far each coordinate sits from a real road, whether
Google Routes can actually drive there, and whether our A* distance agrees with
Google's. It exists because a coordinate can be present, well-formed and inside
Bengaluru and still be 2 km from the actual hospital. Expect **0 problems**;
one warning about BGS Gleneagles is known and harmless (the hospital sits on a
residential access road that the graph does not index).

### One-time, if the road graph is missing

```bash
cd backend && ./venv/bin/python scripts/build_road_graph.py
```

Pulls Bengaluru's drivable network from OpenStreetMap and writes a compact
1.6 MB graph: **112,725 nodes, 178,089 directed edges**.

### Reset to a clean slate

Clearing incidents needs the operator token even in demo mode, so the
simplest clean slate is a fresh data directory. Stop the running server
first (it holds port 8000), then from `backend/`:

```bash
RAPIDGRID_DATA_DIR=$(mktemp -d) ./venv/bin/python -m uvicorn main:app --port 8000
# Windows (PowerShell): $env:RAPIDGRID_DATA_DIR = (New-Item -ItemType Directory "$env:TEMP\rg-$(Get-Random)").FullName; .\venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

With `RAPIDGRID_OPERATOR_TOKEN` set you can clear in place instead. The
token is needed for both requests in operator mode; in demo mode the city
reset works without it.

```bash
curl -s -X POST -H "Authorization: Bearer $RAPIDGRID_OPERATOR_TOKEN" http://127.0.0.1:8000/api/incidents/clear
curl -s -X POST -H "Authorization: Bearer $RAPIDGRID_OPERATOR_TOKEN" http://127.0.0.1:8000/api/city/clear
```

### Presenting on the hosted site

For the finals the hosted showcase runs as an **open demo**
(`RAPIDGRID_MODE=demo`): no unlock screen, so every screen opens directly.
Use only made-up caller names and numbers; in demo mode anyone with the link
can see them. `RAPIDGRID_ALLOWED_HOSTS` must list the custom domain, or the
API refuses it.

To reset the hosted site before presenting, use **Reset the city** on the
dispatcher screen, and clear old incidents with the access token (still set
on the server; clearing incidents needs it even in demo mode):

```bash
curl -s -X POST -H "Authorization: Bearer $RAPIDGRID_OPERATOR_TOKEN" https://rapidgrid.highcortisol.xyz/api/incidents/clear
```

The commands in sections 1 and 2 that call `127.0.0.1:8000` are for a local
run. The API docs are at `/docs` on the hosted site too.

### Open four browser tabs

| Tab | Local | Hosted | Role |
|-----|-------|--------|------|
| 1 | `localhost:5173/citizen` | `rapidgrid.highcortisol.xyz/citizen` | Caller (Ananya Sharma, +91 98765 43210) |
| 2 | `localhost:5173/dispatcher` | `rapidgrid.highcortisol.xyz/dispatcher` | City dispatch |
| 3 | `localhost:5173/driver` | `rapidgrid.highcortisol.xyz/driver` | Field unit (AMB-UNIT-04) |
| 4 | `localhost:5173/hospital` | `rapidgrid.highcortisol.xyz/hospital` | Emergency department |

Arrange them so you can switch quickly. Tab 2 is the one judges will look at
longest.

> **Say this early:** for the demo, anyone can open any role. There are no
> per-person accounts yet; a deployment can be locked behind an access token
> (operator mode). Naming that before anyone asks buys you credibility for
> everything that follows.

---

## 1. The flow (about 6 minutes)

### Beat 1 — the citizen raises an emergency (45s)

**Tab 1.** Press the SOS button, choose **Medical**, and type something real:

> `My father collapsed with severe chest pain and is barely conscious`

Press **Send emergency report**.

**Point out:** the progress steps name what the system is actually doing —
reading the report, assessing severity, checking traffic, ranking hospitals,
routing. There is no fake telemetry. An earlier build cycled
`SIGNAL STRENGTH: EXCELLENT / ACCURACY: 3 METERS`; that was theatre and it is gone.

Behind it:

```bash
curl -s -X POST http://127.0.0.1:8000/api/incidents -H "Content-Type: application/json" -d '{"citizen_text":"My father collapsed with severe chest pain and is barely conscious","citizen_name":"Ananya Sharma","citizen_phone":"+91 98765 43210","location":{"lat":13.0350,"lng":77.5970},"vehicle_required":"Ambulance"}'
```

Returns `202 Accepted` immediately with a `poll_url` — the agents run in the
background rather than blocking the caller.

### Beat 2 — triage, and why it is not a black box (30s)

The call is classified **cardiac, severity 0.99, P1 CRITICAL**.

Show that the classifier is transparent and auditable, not a claimed ML model:

```bash
cd backend && ./venv/bin/python -c "import sys; sys.path.insert(0,'.'); from agents.triage import classify; r=classify('Two-wheeler hit by a truck, rider has heavy bleeding from the leg'); print(r.emergency_type.value, r.severity, r.confidence); print(r.as_note())"
```

**Say this:** it is a weighted-keyword model and we call it that. It reports
low confidence when the evidence is thin — "someone fainted at the bus stop"
comes back at 0.38, not 0.88 — because over-confidence is how these systems
mislead people.

### Beat 3 — the dispatch console (90s) ← *the important one*

**Tab 2.** The call is in the queue with a full plan. Walk through, in order:

**a) The ranked destinations.** Five hospitals, each with a real road ETA and a
`VERIFIED` or `SIMULATED` provenance badge. Note that a cardiac call is ranked
against cath-lab capability, and that oncology, maternity and day-surgery units
were excluded from the candidate list entirely.

**b) The trade-off card.** Two columns — selected vs runner-up — showing the
factor each one wins on and the road-minute cost. This is a genuine
counterfactual: the system states which hospital it rejected and why.

**c) Time to definitive treatment.** The stacked bar breaks the clock into
dispatch → drive to patient → on scene → transport → in-hospital workup →
secondary transfer. The 90-minute target is checked from first medical
contact (the crew reaching the patient), not from the hospital door.

**Say this:** minutes on the road are a proxy. The outcome is time to
treatment. A nearer hospital without a cath lab does not cost a few minutes —
it costs a whole second journey, because the patient has to be transferred
onward. That is why driving further can arrive sooner.

Prove it live:

```bash
cd backend && ./venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from agents.clinical_outcome import compare
near = {'name':'Local general hospital','capabilities':{'cardiology':0.42}}
pci  = {'name':'Jayadeva Institute of Cardiovascular Sciences','capabilities':{'cardiology':1.00}}
r = compare('cardiac', pci, near, 8*60, 16*60, 5*60)
print(r['verdict'])
print('PCI centre :', r['chosen']['total_minutes'], 'min | within target:', r['chosen']['within_target'])
print('nearest ER :', r['alternative']['total_minutes'], 'min | within target:', r['alternative']['within_target'])
"
```

> `Drove 11.0 min further to reach primary angioplasty 48 min sooner.`
>
> This is an **illustrative estimate** from fixed timing assumptions, not a
> measured outcome. Say so when you show it.
> `PCI centre : 60.7 min | within target: True`  ·  `nearest ER : 109.1 min | within target: False`
>
> Totals run from the call. The 90-minute target is checked from first medical
> contact (50.2 and 98.6 min), so the nearest ER misses it by about 9 minutes.

**d) Approve and dispatch.** The status flips to `EN ROUTE` and the call
propagates to the driver and hospital tabs.

### Beat 4 — the field unit (45s)

**Tab 3.** The call appears. Open it.

Two-stage navigation: **Stage 1** drives the unit from its station hub to the
patient (dashed line on the map), **Stage 2** carries the patient to the ER
(solid line). Tap **I have reached the patient** to advance.

Show **Change destination** — if the assigned ER diverts, the crew picks
another and the route plus the caller's screen update immediately.

### Beat 5 — the emergency department (30s)

**Tab 4.** The patient is inbound with a countdown, severity band and summary.

Flip **ICU capacity** to **On divert**. This is a real control, not a toggle
for show — it removes the facility from the routing engine's candidate list for
new calls.

```bash
curl -s -X PATCH http://127.0.0.1:8000/api/hospital/blr-005/resources -H "Content-Type: application/json" -d '{"icu_available": false}'
```

### Beat 6 — chat across all four roles (45s)

Every role shares one incident channel. Type in **Tab 1** as the caller:

> `He is breathing but very weak.`

It appears in the driver and hospital tabs within a second. Reply from **Tab 3**:

> `4 minutes out. Keep him on his side.`

**Point at the header:** it reads `Live` when the WebSocket is connected and
`Delayed` when it has dropped to HTTP polling. It never claims live when it is not.

Verify from the terminal:

```bash
curl -s http://127.0.0.1:8000/api/chat/INC-XXXX/messages
```

### Beat 7 — the city is alive (75s) ← *the memorable one*

**Tab 2, Disruptions panel.** Type `Bellary Road` and select it.

Watch: the closure draws on the map, a red congestion bloom spreads onto the
surrounding streets, and the counters show segments closed and displaced.

**Say this:** traffic on a closed road does not evaporate. It reappears on
parallel streets, which congest, which slows *a different ambulance somewhere
else in the city*. That second-order effect is the whole point.

Prove it with numbers:

```bash
cd backend && ./venv/bin/python -c "
import sys, logging; sys.path.insert(0,'.'); logging.basicConfig(level=logging.WARNING)
from agents.local_router import local_router
from agents.city_state import city_state
local_router.load(); city_state.attach(local_router); city_state.set_hour(18)
P=((13.0350,77.5970),(12.9290,77.5878)); N=((13.0420,77.5860),(12.9980,77.5710)); F=((12.9698,77.7180),(12.9352,77.6245))
t=lambda x:(local_router.find_path(*x) or {}).get('travel_time_s',0)/60
b={k:t(v) for k,v in dict(p=P,n=N,f=F).items()}
bell=[e for e in local_router.find_path(*P)['edge_ids'] if 'Bellary' in local_router.road_names[local_router.edge_name[e]]]
r=city_state.close_road(bell,'Bellary Road'); a={k:t(v) for k,v in dict(p=P,n=N,f=F).items()}
print(f\"closed {r['closed']} segments -> displaced onto {r['displaced']} neighbours\")
for k,lab in [('p','used the closed road'),('n','parallel corridor (untouched)'),('f','across town')]:
    print(f'  {lab:<32}{b[k]:5.1f} -> {a[k]:5.1f} min  ({a[k]-b[k]:+.1f})')
"
```

```
closed 52 segments -> displaced onto 97 neighbours
  used the closed road             28.8 -> 33.9 min  (+5.1)
  parallel corridor (untouched)    13.1 -> 16.0 min  (+2.9)   <- never used the road
  across town                      24.4 -> 24.4 min  (+0.0)   <- correctly unaffected
```

**The middle row is the demo.** A route that never touched the closure gets
slower. A route across town does not, because displacement is local.

Also show time of day and weather in the same panel — the same route runs
16.2 min at 03:00, 28.8 at evening peak, 38.9 in the rain.

Ask the causal model to explain any segment. **This needs a closure to be
active** — do the road closure above first, or the command below makes one, so
it works standalone:

```bash
curl -s -X POST http://127.0.0.1:8000/api/city/close -H "Content-Type: application/json" -d '{"road":"Bellary Road"}' -o /dev/null; EDGE=$(curl -s "http://127.0.0.1:8000/api/city/overlay?limit=5" | python -c "import sys,json;c=json.load(sys.stdin)['congestion'];print(c[0]['edge_id'] if c else '')"); [ -n "$EDGE" ] && curl -s http://127.0.0.1:8000/api/city/explain/$EDGE || echo "No congestion yet - close a road first"
```

It returns the causal chain for that segment — which mechanism slowed it and by
how much:

```json
{ "edge_id": 177923, "blocked": false,
  "causes": [
    { "cause": "time_of_day",   "detail": "06:00 traffic profile",                 "multiplier": 1.02 },
    { "cause": "displacement",  "detail": "traffic displaced from a nearby closure","multiplier": 1.85 }
  ],
  "total_multiplier": 1.89, "provenance": "modelled" }
```

Then reset:

```bash
curl -s -X POST http://127.0.0.1:8000/api/city/clear
```

### Beat 8 — break it on purpose (45s) ← *the flex*

**Turn off the wi-fi.** Or revoke the Google key. Raise another SOS.

The system keeps routing. The Google tier fails, an A* router falls back to the
cached OpenStreetMap graph, and the plan is labelled **`Degraded data — live
routing was unavailable`** with the confidence figure dropped accordingly.

**Say this:** most demos die without wi-fi. The failure mode we care about is
not "no route" — it is a system that keeps producing confident-looking numbers
when it has no idea. Watch what it says about itself instead.

```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool
```

`routing.offline_graph_available`, `billable_api_calls`, `cache_hits`,
`hit_rate` — plus the live city model state.

### Beat 9 — what is real and what is not (30s)

Scroll to the bottom of the landing page. Two columns, side by side.

**Say this:** every system like this models something. Most do not tell you
which parts. Ours draws modelled values differently everywhere they appear —
hatched badges, dotted underlines — so you never have to ask.

---

## 2. Proving there is no hardcoding

Judges will suspect the numbers are staged. These are the fastest disproofs.

### Change the location and the answer changes

```bash
curl -s -X POST http://127.0.0.1:8000/api/hospital/rank -H "Content-Type: application/json" -d '{"incident_location":{"lat":12.9698,"lng":77.7499},"emergency_type":"stroke"}' | python -c "import sys,json; [print(f\"  {h['score']:.3f}  {h['name']}\") for h in json.load(sys.stdin)['ranked_hospitals']]"
```

Run it again with `"emergency_type":"cardiac"` — a different ranking, because
the weight table and the specialty gate change.

### The same emergency at different times of day

Beat 7's script already shows 16.2 / 28.8 / 38.9 minutes for one route at
03:00, peak, and peak-with-rain.

### Hospital coordinates are sourced, not typed

```bash
cd backend && python -c "import json;d=json.load(open('data/hospital_capabilities.json',encoding='utf-8'))['hospitals'];[print(f\"  {v['coord_source']:<20} {v['lat']:.4f},{v['lng']:.4f}  {v['name'][:40]}\") for v in list(d.values())[:8]]"
```

Every hospital carries a `coord_source` — `openstreetmap` where the resolver
matched confidently, `manual-reviewed` where a human checked it against the OSM
extract, `manual-unverified` for the single facility (Columbia Asia Hebbal,
since rebranded) with no confident entry.

**Say this:** these were originally hand-written from memory and 24 of 30 were
more than 700 m out — one of them put a hospital in the wrong neighbourhood
entirely. They are now resolved from OpenStreetMap, and every one records where
it came from.

### The road graph is real OpenStreetMap data

```bash
cd backend && ./venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from agents.local_router import local_router
local_router.load()
p = local_router.find_path((13.0350,77.5970),(12.9290,77.5878))
print(f\"{p['distance_m']/1000:.2f} km, {p['travel_time_s']/60:.1f} min, {len(p['edge_ids'])} segments\")
print('via', ' -> '.join(p['roads'][:6]))
print(local_router.meta)
"
```

Real Bengaluru road names come back — Bellary Road, Mekhri Circle Underpass,
Hosur Road. You cannot fake that with a lookup table.

### Live traffic is genuinely live

The offline graph estimates Hebbal → Jayadeva at ~18 min. Google Routes with
live traffic returns ~41 min for the same pair. That gap is real congestion,
and it flows into every ETA and every hospital ranking.

### Interactive API docs

`http://localhost:8000/docs` — every endpoint, every schema, try any of them
live in front of the judges. On the hosted (operator-mode) site, unlock first;
the docs are hidden from visitors who have not.

---

## 3. The endpoints, by role

**Citizen** — `POST /api/incidents` · `GET /api/incidents/{id}`
**Dispatch** — `GET /api/incidents/` · `POST /api/incidents/{id}/approve`
**Field unit** — `GET /api/driver/active` · `POST /api/driver/arrived_pickup` · `POST /api/driver/change_hospital`
**Hospital** — `GET /api/hospital/{id}/incoming` · `PATCH /api/hospital/{id}/resources`
**Chat** — `GET /api/chat/{id}/messages` · `POST /api/chat/{id}/send` · `WS /ws/chat/{id}`
**City model** — `GET /api/city/state` · `POST /api/city/close` · `POST /api/city/conditions` · `GET /api/city/overlay` · `GET /api/city/explain/{edge}` · `POST /api/city/clear`
**Agents** — `POST /api/fusion/combine` · `POST /api/hospital/rank` · `POST /api/route/compute`
**Platform** — `GET /health` · `GET /docs`

---

## 4. Answers to the questions you will get

**"Are those ICU bed numbers real?"**
No, and we say so on screen. Hospital *capability* — cath lab, stroke unit,
trauma centre, blood bank — is curated for 30 Bengaluru facilities and is what
drives routing. Live *availability* is modelled, because no hospital HMIS or
HL7 feed is integrated. The two are drawn differently everywhere they appear.

**"Is this causal AI?"**
It is a *structural causal model*, not causal inference. We specify the
mechanisms rather than learning them, because we have no historical incident
corpus to identify effects from. What that buys us is interventions and
counterfactuals: in the real world you cannot run `do(close Bellary Road)`;
inside a model we own, you can.

**"Is the traffic model calibrated?"**
No. The coefficients are literature-informed estimates, not fitted to Bengaluru
traffic counts. Directionally they are sound — closing an arterial raises
congestion on parallel roads, rain slows everything, rush hour is worse than
3am — but the magnitudes are plausible rather than validated. That disclaimer
is printed in the Disruptions panel and returned in the API response.

**"What is the AI here — is it just an API call?"**
Four things the API does not do: a specialty-bypass gate that screens hospitals
on clinical capability before proximity; a two-stage ranking that recomputes
real road ETAs for the shortlist; an explainable fusion layer that states which
option it rejected and why; and a causal city model where a closure displaces
traffic onto neighbouring roads.

**"How do you know the hospital locations are right?"**
We did not, at first — they were hand-written and 24 of 30 were over 700 m out,
which sent a chest-pain patient at MSRIT to a hospital ten minutes away when
the right one was 600 m down the road. They are now resolved against
OpenStreetMap and each records its `coord_source`.
`scripts/validate_against_apis.py` re-checks all 30 hospitals and 11 hubs
against both our graph and Google Routes: 0 problems, with the two networks
agreeing on distance to within 1–15%.

**"How is this different from Google Maps?"**
Google Maps finds the fastest route to a place you chose. RapidGrid chooses
the place: for cardiac, stroke and trauma it first keeps only hospitals that
can deliver the treatment, then ranks them on capability and road time. The
cost of a secondary transfer is not part of that ranking; it is shown
afterwards, in the outcome comparison of the top two, to explain why the
capability gate matters. And its city model reacts to interventions: a
closure pushes traffic onto neighbouring roads, so hospital ranking and
offline routes change for other ambulances too. (With a Google Routes key,
dispatched routes come from Google and do not see drawn closures.)

**"What do you mean by better choices as a whole?"**
Every incident is planned on the same city model. A closure drawn for one
call can change the costs for every other call (hospital ranking always uses
the city model; dispatched routes do when they come from the offline router), and a hospital going on divert
drops out of every new plan at once while crews already heading there are
warned. Planning several incidents *jointly* (who gets which ambulance) is
the next step.

**"What happens if Google goes down?"**
Beat 8. Turn off the wi-fi and watch.

**"Is it secure?"**
For a showcase, yes; for real dispatch, not yet. Today's demo is open on
purpose, with synthetic data. Operator mode locks a deployment behind a
shared access token: sessions expire after 8 hours and end on the server at
sign-out, wrong tokens are throttled, the API docs are hidden until unlock,
and every response carries browser security headers. CodeQL reports
no open alerts and an independent audit's findings are fixed. What it lacks is
per-person accounts: inside the lock, anyone can act in any role. Real
authentication and
authorisation is the first thing a production deployment needs.

**"What would you build next?"**
Calibrate the displacement coefficients against real traffic counts; integrate
one hospital's live bed feed to replace the modelled availability; and
multi-incident resource contention, so two simultaneous calls compete for the
same ambulance instead of each assuming an infinite fleet.

---

## 5. If something breaks mid-demo

| Symptom | Cause | Fix |
|---|---|---|
| Console shows `Backend offline` | uvicorn died | restart it; the badge clears itself |
| `Degraded data` banner | Google Routes unreachable | expected — this *is* Beat 8, lean into it |
| Map tiles grey | OpenStreetMap rate limit | reload; tiles are cached after first load |
| Queue empty | incidents cleared | raise an SOS from Tab 1 |
| Everything slow | first Overpass call | hospital catalogue caches for 15 min after the first fetch |
| Port 5173 busy | an old Vite still running | `lsof -i :5173` (Windows: `Get-NetTCPConnection -LocalPort 5173 -State Listen`) gives the PID |
| Unlock says "Too many wrong tokens" | ten wrong tokens from this network in 10 minutes | wait for the time shown, then paste the token |
| A hospital looks misplaced | coordinate drift | `python scripts/validate_against_apis.py` names it; `resolve_hospital_coords.py --write` re-resolves |

**Record a backup video the night before.** Venue wi-fi is the one failure the
offline tier cannot save you from — if the laptop cannot reach the projector,
no amount of graceful degradation helps.

---

## 6. The five sentences that matter

1. The nearest hospital is often the wrong one — a cardiac call needs a cath
   lab, not proximity.
2. In the illustrative estimate, driving 11 minutes further reaches treatment
   about 48 minutes sooner, because the nearer hospital would have had to
   transfer the patient onward. The figure is modelled, not measured.
3. Close one road and a different ambulance across the corridor gets slower —
   traffic displaces, it does not evaporate.
4. When live routing fails, it falls back to a real A* router over 112,725
   OpenStreetMap nodes and labels itself degraded instead of guessing.
5. Every number on screen is either measured or marked as modelled — and we
   will show you which is which before you ask.
