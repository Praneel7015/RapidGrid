"""Citizen accounts: phone number and a 6-digit PIN.

An account lets a caller find their own reports again and lets staff reach
them; it is never required to report an emergency (an anonymous report
always works). Numbers are not verified by SMS, so an account proves only
that someone knows the PIN for that number, which the screens say.

Security (OWASP A02, A07):
- The PIN is stored as scrypt(HMAC(server secret, PIN), per-account salt), so
  a copied database alone does not give up 6-digit PINs to a quick search.
- Wrong PINs: three free, then an increasing per-number delay (capped at 60 s)
  plus a per-client limit, shared with staff sign-in, with no global ceiling.
- Unknown numbers and wrong PINs get the same answer and cost.
- The account id in the session cookie is a hash of the number, not the
  number itself; the cookie is signed, HttpOnly, SameSite=Strict.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from starlette.requests import HTTPConnection

import security
import staff
import storage

logger = logging.getLogger("geoagentic.citizens")

COOKIE_NAME = "rapidgrid_citizen"
SESSION_SECONDS = 30 * 24 * 60 * 60  # a caller should not have to sign in again mid-emergency
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def normalise_phone(value: str) -> str:
    """Digits only, with India's +91 made explicit for 10-digit numbers.
    Any Unicode decimal digit counts (as its ASCII value), and a leading 00
    international prefix is the same as a leading +."""
    text = (value or "").strip()
    digits = "".join(str(int(ch)) for ch in text if ch.isdecimal())
    if not text.startswith("+") and digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        raise ValueError("Enter a phone number with 10 to 15 digits")
    return "+" + digits


def citizen_id(phone: str) -> str:
    return hashlib.sha256(("citizen:" + phone).encode()).hexdigest()[:32]


def _pin_hash(pin: str, salt: bytes) -> str:
    keyed = hmac.new(staff._secret(), ("citizen-pin:" + pin).encode(), hashlib.sha256).digest()
    return hashlib.scrypt(keyed, salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT).hex()


# scrypt is deliberately costly (about 16 MB each); at most two at a time, so
# a flood of sign-ins cannot exhaust the server's memory or CPU.
_HASHING = asyncio.Semaphore(2)


async def _hash(pin: str, salt: bytes) -> str:
    """Hash in a worker thread, holding a permit until that thread is done,
    even if the request is cancelled meanwhile (the thread cannot be)."""
    await _HASHING.acquire()
    try:
        future = asyncio.get_running_loop().run_in_executor(None, _pin_hash, pin, salt)
    except BaseException:
        _HASHING.release()
        raise
    future.add_done_callback(lambda _f: _HASHING.release())
    return await asyncio.shield(future)


class Registration(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=6, max_length=24)
    pin: str = Field(pattern=r"^[0-9]{6}$")

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter your name")
        return value


class Login(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    pin: str = Field(min_length=1, max_length=12)


def _sign(*parts: str) -> str:
    return hmac.new(staff._secret(), ("citizen-session:" + ":".join(parts)).encode(), hashlib.sha256).hexdigest()


def _cookie(cid: str) -> str:
    issued = str(int(time.time()))
    sid = secrets.token_urlsafe(18)
    return ".".join((issued, sid, cid, _sign(issued, sid, cid)))


def _parse(cookie: str) -> Optional[tuple[str, str, float]]:
    parts = cookie.split(".")
    if len(parts) != 4:
        return None
    issued, sid, cid, signature = parts
    if not (issued.isascii() and issued.isdigit() and len(issued) <= 12):
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", cid) or not 16 <= len(sid) <= 64 or not sid.isascii():
        return None
    age = time.time() - int(issued)
    if not 0 <= age < SESSION_SECONDS:
        return None
    if not hmac.compare_digest(signature.encode(), _sign(issued, sid, cid).encode()):
        return None
    return sid, cid, int(issued) + SESSION_SECONDS


def _account(cid: str) -> Optional[dict]:
    raw = storage.load_citizen(cid, database=storage.data_path())
    return json.loads(raw) if raw else None


async def current_async(connection: Optional[HTTPConnection]) -> Optional[dict]:
    """current(), with the storage read in a worker thread (no cookie: no read)."""
    if connection is None or not connection.cookies.get(COOKIE_NAME):
        return None
    return await asyncio.to_thread(current, connection)


def current(connection: Optional[HTTPConnection]) -> Optional[dict]:
    """The signed-in citizen's account ({id, name, phone}), or None."""
    if connection is None:
        return None
    parsed = _parse(connection.cookies.get(COOKIE_NAME, ""))
    if parsed is None or security.is_revoked(parsed[0]):
        return None
    account = _account(parsed[1])
    if account is None:
        return None
    return {"id": parsed[1], "name": account["name"], "phone": account["phone"]}


def _set_cookie(request: Request, response: Response, cid: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE_NAME, _cookie(cid), max_age=SESSION_SECONDS, httponly=True,
                        secure=secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/citizens", tags=["Citizen accounts"])

# Registrations per client: every admitted attempt counts, successful or not.
REGISTER_LIMIT = 5
REGISTER_WINDOW_S = 600
_registrations: dict[str, list[float]] = {}
_number_locks: dict[str, asyncio.Lock] = {}


def _admit_registration(client: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _registrations.get(client, []) if now - t < REGISTER_WINDOW_S]
    if len(recent) >= REGISTER_LIMIT:
        _registrations[client] = recent
        return False
    recent.append(now)
    _registrations[client] = recent
    if len(_registrations) > 2000:
        for key in [k for k, v in _registrations.items() if not v or now - v[-1] >= REGISTER_WINDOW_S]:
            del _registrations[key]
    return True


def _number_lock(cid: str) -> asyncio.Lock:
    lock = _number_locks.get(cid)
    if lock is None:
        if len(_number_locks) > 5000:
            for key in [k for k, v in _number_locks.items() if not v.locked()]:
                del _number_locks[key]
        lock = _number_locks[cid] = asyncio.Lock()
    return lock


def _same_origin_or_refuse(request: Request) -> None:
    # Account changes come only from the app's own pages (CSRF).
    if not request.headers.get("origin") or not security.same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin or origin-less request refused")


@router.post("/register")
async def register(payload: Registration, request: Request, response: Response):
    _same_origin_or_refuse(request)
    try:
        phone = normalise_phone(payload.phone)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    client = security.client_key(request)
    if not _admit_registration(client):
        raise HTTPException(status_code=429, detail="Too many registrations from here. Try again later.",
                            headers={"Retry-After": str(REGISTER_WINDOW_S)})
    cid = citizen_id(phone)
    salt = secrets.token_bytes(16)
    record = {"name": payload.name, "phone": phone, "salt": salt.hex(),
              "pin_hash": await _hash(payload.pin, salt), "created_at": time.time()}
    created = await asyncio.to_thread(storage.insert_citizen, cid, json.dumps(record),
                                       database=storage.data_path())
    if not created:
        # Saying so reveals that the number has an account; the alternative
        # (a silent success) would leave a caller unable to tell why sign-in
        # fails. Registrations are rate limited per client instead.
        raise HTTPException(status_code=409, detail="This number already has an account. Sign in instead.")
    _set_cookie(request, response, cid)
    logger.info("Citizen account created")
    return {"name": payload.name, "phone": phone}


@router.post("/session")
async def sign_in(payload: Login, request: Request, response: Response):
    _same_origin_or_refuse(request)
    try:
        phone = normalise_phone(payload.phone)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    cid = citizen_id(phone)
    account_key = "citizen:" + cid
    client = security.client_key(request)
    # One attempt per number at a time: checking the backoff, hashing and
    # recording the outcome happen together, so parallel guesses cannot all
    # slip in before the first failure is counted.
    async with _number_lock(cid):
        return await _check_pin(payload, request, response, cid, account_key, client)


async def _check_pin(payload, request, response, cid, account_key, client):
    wait = staff._wait(account_key, client)
    if wait:
        raise HTTPException(status_code=429, detail="Too many wrong PINs. Try again shortly.",
                            headers={"Retry-After": str(wait)})
    account = await asyncio.to_thread(_account, cid)
    # Unknown numbers cost the same as a wrong PIN and get the same answer.
    salt = bytes.fromhex(account["salt"]) if account else secrets.token_bytes(16)
    attempt = await _hash(payload.pin, salt) if re.fullmatch(r"[0-9]{6}", payload.pin) else ""
    ok = bool(account) and bool(attempt) and hmac.compare_digest(attempt, account["pin_hash"])
    if not ok:
        staff._fail(account_key, client)
        raise HTTPException(status_code=401, detail="Wrong phone number or PIN")
    staff._succeed(account_key)
    _set_cookie(request, response, cid)
    return {"name": account["name"], "phone": account["phone"]}


@router.get("/session")
async def session(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    me = current(request)
    return {"signed_in": bool(me), "name": me["name"] if me else None, "phone": me["phone"] if me else None}


@router.delete("/session")
async def sign_out(request: Request, response: Response):
    _same_origin_or_refuse(request)
    parsed = _parse(request.cookies.get(COOKIE_NAME, ""))
    if parsed is not None:
        try:
            security.revoke_session(parsed[0], parsed[2])
        except security.RevocationUnavailable:
            return Response(status_code=503, content='{"detail":"Sign-out could not be recorded. Try again."}',
                            media_type="application/json", headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"signed_in": False}


@router.get("/me/incidents")
async def my_incidents(request: Request):
    """The signed-in citizen's own reports, newest last, without other callers' details."""
    me = await current_async(request)
    if me is None:
        raise HTTPException(status_code=401, detail="Sign in to see your reports")
    from routers import incident as incident_router

    async with incident_router._INCIDENTS_LOCK:
        mine = [incident_router.public_incident(i) for i in incident_router.ACTIVE_INCIDENTS
                if i.get("citizen_id") == me["id"]]
    return {"incidents": mine}
