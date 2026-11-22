"""Staff sign-in: one PIN per role, unit and hospital, scoped sessions.

A staff session says who the browser is acting as: the dispatcher, one unit
(for example AMB-01) or one hospital. Actions check that scope on the server;
the role and unit never come from the request body (OWASP A01).

Staff sign-in is on whenever the contacts directory holds a staff PIN. With
none (the committed example file), it is off and staff actions stay open, as
in the local demo. An operator-token session (see security.py) may do
everything, so scripts and the existing operator mode keep working.

PINs are 6 digits. To keep guessing slow without letting a stranger lock the
dispatcher out on stage, failures are counted per account with an increasing
delay (capped at 60 s) and per client, but never against a global ceiling.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

import contacts
import security
from logsafe import log_safe

logger = logging.getLogger("geoagentic.staff")

COOKIE_NAME = "rapidgrid_staff"
SESSION_SECONDS = 8 * 60 * 60
Role = Literal["dispatcher", "unit", "hospital"]
_SUBJECT = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# Per-account backoff: the first few failures are free, then each one doubles
# the wait, up to MAX_DELAY_S. Per-client: at most CLIENT_FAILURES per window.
FREE_FAILURES = 3
MAX_DELAY_S = 60.0
CLIENT_FAILURES = 20
CLIENT_WINDOW_S = 10 * 60

_lock = threading.Lock()
_accounts: dict[str, tuple[int, float]] = {}  # key -> (failures, not_before)
_clients: dict[str, list[float]] = {}


def _secret() -> bytes:
    """Signs staff sessions and keys PIN comparison. Set RAPIDGRID_SECRET on
    the server; without it a random one is made per process, so sessions end
    at every restart (safe, merely inconvenient)."""
    global _ephemeral
    value = os.getenv("RAPIDGRID_SECRET", "")
    if value:
        return value.encode()
    with _lock:
        if _ephemeral is None:
            _ephemeral = secrets.token_bytes(32)
            logger.warning("RAPIDGRID_SECRET is not set; staff sessions end when the server restarts")
        return _ephemeral


_ephemeral: Optional[bytes] = None


def enabled() -> bool:
    return contacts.staff_pins_configured()


def _digest(pin: str) -> bytes:
    return hmac.new(_secret(), ("staff-pin:" + pin).encode(), hashlib.sha256).digest()


def reset_throttle() -> None:
    with _lock:
        _accounts.clear()
        _clients.clear()


def _wait(account: str, client: str) -> int:
    now = time.monotonic()
    with _lock:
        _failures, not_before = _accounts.get(account, (0, 0.0))
        recent = [t for t in _clients.get(client, []) if now - t < CLIENT_WINDOW_S]
        _clients[client] = recent
        waits = [not_before - now] if not_before > now else []
        if len(recent) >= CLIENT_FAILURES:
            waits.append(CLIENT_WINDOW_S - (now - recent[0]))
    return int(max(waits)) + 1 if waits else 0


def _fail(account: str, client: str) -> None:
    now = time.monotonic()
    with _lock:
        failures, _ = _accounts.get(account, (0, 0.0))
        failures += 1
        delay = 0.0 if failures <= FREE_FAILURES else min(MAX_DELAY_S, 2.0 ** (failures - FREE_FAILURES))
        _accounts[account] = (failures, now + delay)
        _clients.setdefault(client, []).append(now)
        if len(_clients) > 2000:
            for key in [k for k, v in _clients.items() if not v or now - v[-1] >= CLIENT_WINDOW_S]:
                del _clients[key]


def _succeed(account: str) -> None:
    with _lock:
        _accounts.pop(account, None)


# --- Sessions -----------------------------------------------------------------
# Cookie: "<issued>.<session id>.<role>.<subject>.<signature>". Revocation
# reuses the operator-session store in security.py.

def _sign(*parts: str) -> str:
    return hmac.new(_secret(), ("staff-session:" + ":".join(parts)).encode(), hashlib.sha256).hexdigest()


def _cookie_value(role: str, subject: str) -> str:
    issued = str(int(time.time()))
    sid = secrets.token_urlsafe(18)
    return ".".join((issued, sid, role, subject, _sign(issued, sid, role, subject)))


def _parse(cookie: str) -> Optional[tuple[str, str, str, float]]:
    """(session id, role, subject, expiry) for a valid, unexpired cookie."""
    parts = cookie.split(".")
    if len(parts) != 5:
        return None
    issued, sid, role, subject, signature = parts
    if not (issued.isascii() and issued.isdigit() and len(issued) <= 12):
        return None
    if role not in ("dispatcher", "unit", "hospital") or not _SUBJECT.match(subject):
        return None
    if not 16 <= len(sid) <= 64 or not sid.isascii():
        return None
    age = time.time() - int(issued)
    if not 0 <= age < SESSION_SECONDS:
        return None
    if not hmac.compare_digest(signature.encode(), _sign(issued, sid, role, subject).encode()):
        return None
    return sid, role, subject, int(issued) + SESSION_SECONDS


def current(connection: HTTPConnection) -> Optional[tuple[str, str]]:
    """(role, subject) of this browser's staff session, or None.

    An operator-token session counts as ("operator", "*"): it may act as any
    staff member.
    """
    if security.authenticated(connection):
        return ("operator", "*")
    parsed = _parse(connection.cookies.get(COOKIE_NAME, ""))
    if parsed is None:
        return None
    sid, role, subject, _ = parsed
    if security.is_revoked(sid):
        return None
    # A PIN removed from the directory ends that account's sessions.
    if contacts.staff_pin(role, subject) is None:
        return None
    return role, subject


def require(connection: Optional[HTTPConnection], role: str, subject: Optional[str] = None) -> tuple[str, str]:
    """Allow the action for this role (and this unit or hospital, if given).

    Deny by default once staff sign-in is on. Off (no PINs configured), it
    allows everything, as the open local demo always has.
    """
    if not enabled():
        return ("open", "*")
    who = current(connection) if connection is not None else None
    if who is None:
        raise HTTPException(status_code=401, detail="Staff sign-in required")
    if who[0] == "operator":
        return who
    if who[0] != role or (subject is not None and who[1] != subject):
        raise HTTPException(status_code=403, detail="This sign-in cannot do that")
    # Cookie sessions: a state change must come from our own pages. Browsers
    # send Origin on such requests; a missing one is refused (CSRF, A01).
    method = connection.scope.get("method", "GET")
    if method not in ("GET", "HEAD", "OPTIONS") and (
        not connection.headers.get("origin") or not security.same_origin(connection)
    ):
        raise HTTPException(status_code=403, detail="Cross-origin or origin-less staff action refused")
    return who


# --- API ----------------------------------------------------------------------

router = APIRouter(prefix="/api/staff", tags=["Staff sign-in"])


class StaffLogin(BaseModel):
    role: Role
    subject: str = Field(default="dispatch", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    pin: str = Field(min_length=1, max_length=12)


@router.get("/directory")
async def staff_directory(response: Response):
    """Who can sign in: unit ids, types and stations, hospital names. Never
    phone numbers or PINs."""
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": enabled(), "units": contacts.public_units(), "hospitals": contacts.public_hospitals()}


@router.get("/session")
async def staff_session(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    who = current(request) if enabled() else None
    return {"enabled": enabled(), "role": who[0] if who else None, "subject": who[1] if who else None}


@router.post("/session")
async def staff_login(payload: StaffLogin, request: Request, response: Response):
    if not enabled():
        raise HTTPException(status_code=404, detail="Staff sign-in is not configured")
    subject = "dispatch" if payload.role == "dispatcher" else payload.subject
    account = f"{payload.role}:{subject}"
    client = security.client_key(request)
    wait = _wait(account, client)
    if wait:
        raise HTTPException(status_code=429, detail="Too many wrong PINs. Try again shortly.",
                            headers={"Retry-After": str(wait)})
    expected = contacts.staff_pin(payload.role, subject)
    # Compare digests in constant time; an unknown account gets the same
    # answer and the same cost as a wrong PIN.
    reference = _digest(expected) if expected is not None else _digest(secrets.token_hex(8))
    ok = hmac.compare_digest(_digest(payload.pin), reference) and expected is not None
    if not ok:
        _fail(account, client)
        logger.warning("Wrong staff PIN for %s", log_safe(account))
        raise HTTPException(status_code=401, detail="Wrong sign-in details")
    _succeed(account)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE_NAME, _cookie_value(payload.role, subject), max_age=SESSION_SECONDS,
                        httponly=True, secure=secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    logger.info("Staff sign-in: %s", log_safe(account))
    return {"enabled": True, "role": payload.role, "subject": subject}


@router.delete("/session")
async def staff_logout(request: Request, response: Response):
    parsed = _parse(request.cookies.get(COOKIE_NAME, ""))
    if parsed is not None:
        sid, _role, _subject, expiry = parsed
        try:
            security.revoke_session(sid, expiry)
        except security.RevocationUnavailable:
            return Response(status_code=503, content='{"detail":"Sign-out could not be recorded. Try again."}',
                            media_type="application/json", headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": enabled(), "role": None, "subject": None}
