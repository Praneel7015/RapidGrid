# Secrets and contacts: what goes where

Nothing here is committed. Real values live only on the server (Render) or in a
git-ignored local file.

## 1. Environment variables (secrets)

Set these on Render (Environment tab) for production, or in `backend/.env` locally
(copy from `backend/.env.example`; `.env` is git-ignored).

| Variable | What it is | Required? | How to make it |
|---|---|---|---|
| `RAPIDGRID_SECRET` | Signs staff sessions and keys the PIN checks. Must be at least 32 characters. | **Yes, for production.** Without it the staff sign-in is weak. | `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `RAPIDGRID_CONTACTS` | The whole contacts JSON (see part 2). A hosting secret. | **Yes, for staff sign-in on Render.** A missing value locks staff sign-in. | Paste the JSON as one value. |
| `RAPIDGRID_CONTACTS_FILE` | Alternative to `RAPIDGRID_CONTACTS`: a path to a JSON file. | Optional. | Path on the server. |
| `RAPIDGRID_MODE` | `demo` for the local synthetic demo, `operator` for a hosted showcase. | Yes (`operator` on Render). | Type it. |
| `RAPIDGRID_OPERATOR_TOKEN` | Token that unlocks operator mode in the browser. | Yes on a hosted showcase. | `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `RAPIDGRID_DATA_DIR` | Where the database lives. Use a persistent disk path on Render. | Yes on Render with a disk. | e.g. `/var/data/rapidgrid` |
| `GOOGLE_ROUTES_API_KEY` | Google Routes (live traffic ETAs). | Optional. Leave empty for the finale (see the plan). | From Google Cloud console. |
| `OPENWEATHER_API_KEY` | Weather for the city model. | Optional. | From OpenWeather. |
| `GOOGLE_MAPS_JS_API_KEY` | Google Maps JavaScript. | Optional (frontend). | From Google Cloud console. |
| `RAPIDGRID_REPORT_LIMIT` | Emergency reports per client per 10 minutes. | Set to `60` for the event (shared venue address). | Type it. |
| `RAPIDGRID_ALLOWED_HOSTS` | Host names the demo answers to. | Only for a LAN demo. | e.g. `demo.lan` |

Never paste a secret into a chat, an issue, a pull request, or a file in the repo.

## 2. Contacts (the directory)

The contacts file lists who the system can reach. Format: `backend/contacts.example.json`
(placeholders only). Copy it to `backend/contacts.json` locally (git-ignored), or put the
JSON in `RAPIDGRID_CONTACTS` on Render.

What goes in it:

- `hotline_112`: the 112 emergency line (`name`, `phone`).
- `dispatch`: the city dispatch desk (`name`, `phone`, `staff_pin`, `tracker_key`).
- `units`: one entry per vehicle. `unit_id` (e.g. `AMB-01`), `type` (Ambulance, Fire Engine, Police Cruiser, Disaster Rescue), `station`, `crew` (name of the crew or team), `phone` (the crew's phone), `staff_pin` (6 to 12 digits), `tracker_key` (for the background tracker app).
- `hospitals`: one entry per hospital. `hospital_id` (e.g. `blr-007`), `name`, `emergency_phone`, `icu_phone`, `staff_pin`.

Rules the code enforces:

- Staff PINs: 6 to 12 digits. Leave a PIN empty to keep that person out of sign-in.
- Unit IDs must be unique.
- Tracker keys: 24 to 128 letters, digits, `-` or `_`, and random. Make one with `openssl rand -hex 16`.
- Any staff PIN switches staff sign-in on. With no PINs at all, the app runs as an open local demo.

## 3. Checklist before the event

1. Set `RAPIDGRID_SECRET` (32+ characters) on Render.
2. Set `RAPIDGRID_CONTACTS` on Render with real numbers and PINs, and check that staff sign-in works.
3. Set `RAPIDGRID_DATA_DIR` to a persistent disk, or accept that data resets on restart.
4. Set `RAPIDGRID_REPORT_LIMIT=60`.
5. Set `RAPIDGRID_MODE=operator` and `RAPIDGRID_OPERATOR_TOKEN`.
6. Confirm the GOOGLE_ROUTES_API_KEY decision (leave empty for the finale unless decided otherwise).
7. Run the browser tests against the deployed preview before merging to `main`.
