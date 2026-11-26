# Development guide (humans and AI tools)

This file is the contract for anyone changing RapidGrid, including AI coding
assistants. Read it before you start.

## Setup

- Backend: `cd backend && pip install -r requirements-dev.txt`
- Frontend: `cd frontend && npm ci && npm run build`
- Browser tests: `pip install -r tests/browser/requirements.txt && python -m playwright install chromium`

## Tests (all must pass before a PR)

- `cd backend && python -m pytest -q` (unit and API tests)
- `python -m pytest tests/browser -q` (Playwright, from the repo root)
- `cd frontend && npm run build` (the build must succeed)

## The change loop (required for every change)

1. Write the problem statement first: what breaks, for whom, and the objective it serves.
2. Implement with tests. Keep changes small and on a branch.
3. Run the full test set above locally.
4. Get an independent review of the diff (a second agent or person) for correctness, security (OWASP Top 10), and fit to the problem statement. Fix what it finds.
5. Stage files by name (`git add <path>`, never `git add -A`). Check `git diff --cached --name-only` before committing. Never commit `backend/data/`, `contacts.json`, `*.sqlite3`, `*.key`, or any `.env`.
6. Open a pull request. In the PR description, write the problem statement and the test results.
7. Post a PR comment with `@coderabbitai review`. Wait for the review.
8. Fix every CodeRabbit finding that is critical, major, or moderate. Fix or explain every other valid finding. Re-request the review after each push (CodeRabbit auto-review may be paused).
9. Merge only when CI is green (backend, browser, CodeQL, secret scan) and CodeRabbit has no open critical or major findings. Use a rebase merge so commit authorship is kept.

Do not merge to `main` without the maintainer's go. Pushing to `main` deploys to production.

## Docker

- Local: `docker compose up -d --build`, then open http://localhost:8080 (demo
  mode, synthetic data). State lives in the `rapidgrid-data` volume at
  `/var/data` (incidents, `secret.key`); `docker compose down -v` deletes it.
- Anything public: set `RAPIDGRID_MODE=operator` and mount the contacts file
  read-only (see the commented lines in `docker-compose.yml`) with
  `RAPIDGRID_CONTACTS_FILE` pointing at it. Never bake contacts into the image.
- At start the container runs `backend/scripts/preflight.py --startup`. It never
  blocks boot, but in operator mode (or on Render) with no staff PINs it prints
  `WARNING: OPEN DEPLOYMENT: no staff PINs ...` to the logs: anyone can then act
  as dispatch, crew or ER desk. A contacts file that is set but invalid does not
  warn: the app locks staff sign-in instead. Demo mode with
  `RAPIDGRID_ALLOWED_HOSTS`/`RAPIDGRID_ALLOWED_ORIGINS` set also warns.
- One process only (no `--workers`): fleet, chat and sessions are in memory.
- The browser needs internet for map tiles, fonts and the Whisper voice model.
- VPS behind Caddy (`reverse_proxy 127.0.0.1:8080`): set
  `RAPIDGRID_CLIENT_IP_HEADER=x-forwarded-for` and `RAPIDGRID_CLIENT_IP_HOPS=1`,
  and check them signed in as operator with `GET /api/session/forwarding`
  (send a made-up `X-Forwarded-For: 203.0.113.7`).

## Rules for AI tools

- Follow the change loop above. Do not skip the review or CodeRabbit steps.
- Do not modify production data or the production deployment.
- Do not commit secrets or real contact details. Use `contacts.example.json` for examples.
- Staff sign-in must stay deny-by-default when PINs are configured. Any new route that changes units, incidents, or hospitals needs a guard in `backend/staff.py` (`require`).
- Anything in the open demo that changes live state must be operator-only if it is destructive (for example, taking a unit off duty).
- Label simulated data as simulated on every screen.

## Where things are

- `backend/agents/`: city simulation, routing, monitoring, hospital and city-life agents
- `backend/routers/`: HTTP routes
- `frontend/src/views/`: dashboards for citizens, drivers, dispatchers, and the control room
- `tests/browser/`: end-to-end tests against a local server
- `docs/`: team and demo notes
