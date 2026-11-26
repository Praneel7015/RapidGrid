# Contributing to RapidGrid

RapidGrid is an emergency-routing demo built for the finals. This page is the
one place to start. The detailed change loop is in [DEVELOPMENT.md](DEVELOPMENT.md).

## Before you change anything

1. Read [DEVELOPMENT.md](DEVELOPMENT.md) (setup, tests, the change loop, rules for AI tools).
2. Write the problem statement: what breaks, for whom, and which objective it serves.
3. Work on a branch. Do not push to `main`; pushing to `main` deploys production.

## Setup and tests

- Backend: `cd backend && pip install -r requirements-dev.txt && python -m pytest -q`
- Browser: `pip install -r tests/browser/requirements.txt && python -m playwright install chromium && python -m pytest tests/browser -q` (from the repo root)
- Frontend: `cd frontend && npm ci && npm run build`

All three must pass before a pull request.

## Pull requests

Every change goes through a pull request. In the description, give the problem,
the change, and the test results. Then:

1. Get an independent review of the diff (correctness, security, fit to the problem).
2. Post `@coderabbitai review` as a PR comment and wait for CodeRabbit.
3. Fix every critical, major, and moderate CodeRabbit finding. Explain any you reject.
4. Re-request the review after each push.
5. Merge only when CI is green (backend, browser, CodeQL, secret scan) and CodeRabbit has no open critical or major findings. The maintainer gives the go for `main`.

## Commits

- Stage files by name (`git add <path>`), never `git add -A`.
- Check `git diff --cached --name-only` before every commit.
- Never commit `backend/data/`, `contacts.json`, `*.sqlite3`, `*.key`, or any `.env`.
  The one exception is the vetted static dataset `backend/data/bangalore_hospitals.json`,
  which is committed on purpose so the map works offline.

## Secrets and contacts

Real phone numbers, staff PINs, and signing keys never go in the repo. See
[docs/SECRETS-AND-CONTACTS.md](docs/SECRETS-AND-CONTACTS.md) for what each one is and where it goes.

## Security

Staff sign-in is deny-by-default once PINs are set. Any new route that changes
units, incidents, or hospitals needs a guard in `backend/staff.py`. Taking a unit
off duty is operator-only in the open demo. Report a suspected vulnerability to
the maintainer privately, not in a public issue.

## Labels for simulated data

Anything simulated (live bed counts, the simulated city, ambulances driven by the
simulator) must be labelled as simulated on every screen it appears on.
