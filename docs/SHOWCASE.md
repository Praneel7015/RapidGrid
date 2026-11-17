# Showcase setup and validation

RapidGrid is an emergency-dispatch demonstration. Fleet positions, hospital
availability and notification delivery are simulated. It is not connected to
emergency services, hospital admission systems, or an SMS gateway. Do not use
real patient information in the showcase. Real emergencies require the normal
emergency services channel.

## Local development

Use Python 3.11+ and Node.js 22+. From the repository root on Linux/macOS:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements-dev.txt
```

On Windows, activate `.venv\Scripts\activate` instead. Copy `backend/.env.example`
to `backend/.env` if configuring provider keys, and start the API from `backend`:

```sh
cd backend
python -m uvicorn main:app --reload --port 8000
```

In another terminal:

```sh
cd frontend
npm ci --legacy-peer-deps
npm run dev
```

Open http://localhost:5173. Vite proxies API, health and WebSocket calls to the
local backend. An unset mode defaults to a local synthetic-data demo. Explicit
`RAPIDGRID_MODE=demo` also enables that mode. Local demo sessions share their
data; they are not isolated visitors or separately authorized users. Global
incident/city clear operations still require operator authentication.

Google Routes and OpenWeather keys are optional for local tests. The committed
Bengaluru graph supports offline routing. Offline routing still requires a
running backend; it does not make a disconnected browser able to submit reports.
The current Leaflet map does not use the legacy Maps JavaScript key.

## Protected hosted showcase

The reviewed `render.yaml` keeps the React build and FastAPI API on one origin.
It declares **one paid starter instance and a 1 GB persistent disk**. Applying
that manifest may change billing. Creating a PR does not apply it or provision
resources. Review the plan and disk settings before deploying.

Set these server-side variables before using the protected showcase:

| Variable | Value |
| --- | --- |
| `RAPIDGRID_MODE` | `operator` |
| `RAPIDGRID_OPERATOR_TOKEN` | A generated high-entropy secret |
| `RAPIDGRID_DATA_DIR` | `/var/data` on the persistent disk |
| `GOOGLE_ROUTES_API_KEY` | Existing working server-side Routes key, if using live traffic |
| `OPENWEATHER_API_KEY` | Existing working weather key, if using weather observations |

Generate a token in a private terminal with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Store it in
Render's environment settings. Never put it in a `VITE_` variable, source file,
PR, screenshot, or build log. The browser unlock form submits it to the backend
and receives an expiring HttpOnly, SameSite cookie. HTTPS sessions use Secure
cookies. Logging out expires the browser cookie; rotating the server token
invalidates all existing sessions.

Operator mode protects all `/api` operations and chat sockets. It uses one
shared showcase credential: it is **not** individual identity, driver assignment
authorization, or hospital membership/RBAC. Role/profile selection remains a
demonstration feature. Do not treat this mode as production dispatch security.
When Render is detected and no mode is supplied, access defaults to operator
mode; missing operator secrets fail closed. Keep local demo mode off for a
hosted showcase containing anything beyond synthetic data.

Same-origin browser calls need no CORS setting. If a deliberate separate
development origin is needed, list exact origins in
`RAPIDGRID_ALLOWED_ORIGINS`. Broad origins are not enabled by default.

Demo mode is open, so its API answers only to local host names (`localhost`,
`127.0.0.1`, `::1`). This stops a hostile web page from reaching a locally
running demo through DNS rebinding. To share a demo on a network, add its host
names to `RAPIDGRID_ALLOWED_HOSTS` (comma-separated). Operator mode does not
pin hosts, because every call already needs a session.

## Storage and deployment

Incidents, chat messages and hospital capacity overrides are atomic SQLite
snapshots in `RAPIDGRID_DATA_DIR/rapidgrid.sqlite3`. Without that variable, the
default is `backend/data`, which is suitable for local development but is not
durable on a default Render filesystem. Use **one application process/worker
and one service instance**. SQLite transactions prevent partial snapshot writes;
in-memory state is not coordinated between workers or instances.

Before migrating an existing deployment, back up its incident JSON and any
needed history. A legacy `incidents_db.json` beside the new database is imported
only when the incident collection does not already exist. Merely setting a new
data directory does not copy files from the old container into the disk.
Backups must include all SQLite WAL state; use SQLite's backup facility or stop
the service before copying database files.

An interrupted in-process report is marked failed on restart so it can be
retried visibly. Reports are not durable resumable jobs. Multiple instances,
recoverable jobs and real identity-based access require a larger datastore/job
architecture before operational use.

`/health/live` reports process liveness. `/health` reports aggregate routing
readiness without spending provider quota. A configured key is unverified until
a real route succeeds; recent failures and stale observations are shown
separately. A provider outage can retain offline routing while liveness remains
healthy.

Notification receipts use `simulated` rather than claiming external delivery.
To demonstrate a non-blocking SMS failure deliberately, set
`RAPIDGRID_SIMULATE_SMS_FAILURE=1`. Other simulated channels still complete.
Leave it unset for a normal demonstration.

## Validation

From the repository root with the Python environment activated:

```sh
python -m pytest backend/tests -q
cd frontend
npm ci --legacy-peer-deps
npm run build
cd ..
pip install -r tests/browser/requirements.txt
python -m playwright install chromium
python -m pytest tests/browser -q
```

On Linux CI, `python -m playwright install --with-deps chromium` installs browser
system dependencies as well. If using an existing Chromium executable, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` to its absolute path.

Browser tests start a separate local protected instance with temporary storage,
no live provider keys, seeded hospital data and the real offline routing engine.
They never mutate the public showcase. They verify unlock, citizen refresh,
dispatcher approval, pointer-based chat submission, hospital visibility,
pickup/refresh/handover, independent fire/backup-ambulance progress, failed
capacity updates, capacity persistence and narrow layouts. The suite blocks
service workers so network failure injection is deterministic; it does not
claim to validate offline PWA caching. Failure screenshots are written under
ignored `test-results/`.

The GitHub Actions showcase regression workflow runs the backend tests, frontend
production build and browser tests on PRs. It does not deploy or require secrets.

## Review context

See [review findings and fixes](REVIEW-FIXES.md) for the independent review
process, the repaired failures and remaining boundaries. An existing open PR
also changes CI and hospital routes; rebase and resolve overlapping changes if
that PR merges first. This change uses its own showcase regression workflow.

The existing Render service was configured to build `thotaDeepika/RapidGrid`,
while this PR is proposed to `Praneel7015/RapidGrid`. Merging the fork's PR alone
will not update that upstream deployment. After review, either bring the accepted
change into the configured upstream repository or deliberately change Render's
repository source before deploying it.
