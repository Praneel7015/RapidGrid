"""Shared showcase access gate, deliberately not production identity or RBAC.

An explicit demo remains available for synthetic data. Hosted showcases can use
operator mode, which protects every API and chat socket with an expiring,
HttpOnly session. The secret is configured on the server, never baked into JS.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

logger = logging.getLogger("geoagentic.security")

COOKIE_NAME = "rapidgrid_operator"
SESSION_SECONDS = 8 * 60 * 60
_REVOKED_COLLECTION = "security:revoked_sessions"

# Wrong-token throttle. Counted per client and across all clients, for the
# login form and for Bearer headers alike, so neither path is a faster way to
# guess. A throttled attempt is refused without comparing the token.
FAILURE_WINDOW_SECONDS = 10 * 60
MAX_FAILURES_PER_CLIENT = 10
MAX_FAILURES_TOTAL = 200
# Clearing incidents destroys case records, so it needs operator access even in
# the synthetic demo. Clearing the city model only undoes closures that any
# demo visitor can already draw, so the demo keeps it open.
_BULK_ACTIONS = {"/api/incidents/clear"}
# The interactive API docs list every endpoint and let visitors try them. They
# are for local development, so operator mode serves them only after unlock.
_DOC_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


def mode() -> str:
    # Unknown modes fail closed rather than accidentally enabling the demo.
    # A missing hosted configuration must not silently expose the showcase.
    # Local quickstarts retain the synthetic demo default.
    default = "operator" if os.getenv("RENDER", "").lower() == "true" else "demo"
    return "demo" if os.getenv("RAPIDGRID_MODE", default).lower() == "demo" else "operator"


def operator_token() -> str:
    return os.getenv("RAPIDGRID_OPERATOR_TOKEN", "")


def allowed_origins() -> list[str]:
    return [value.strip().rstrip("/") for value in os.getenv("RAPIDGRID_ALLOWED_ORIGINS", "").split(",") if value.strip()]


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def allowed_hosts() -> set[str]:
    """Host names the open demo API answers to.

    Local names, plus RAPIDGRID_ALLOWED_HOSTS (comma-separated), the hosts of
    RAPIDGRID_ALLOWED_ORIGINS, and Render's own hostname when present.
    """
    hosts = set(_LOCAL_HOSTS)
    hosts.update(h.strip().lower() for h in os.getenv("RAPIDGRID_ALLOWED_HOSTS", "").split(",") if h.strip())
    hosts.update((urlsplit(origin).hostname or "").lower() for origin in allowed_origins())
    if os.getenv("RENDER_EXTERNAL_HOSTNAME"):
        hosts.add(os.getenv("RENDER_EXTERNAL_HOSTNAME", "").lower())
    hosts.discard("")
    return hosts


def host_allowed(connection: HTTPConnection) -> bool:
    host = connection.headers.get("host", "")
    if not host:
        return False
    try:
        hostname = (urlsplit("//" + host).hostname or "").lower()
    except ValueError:  # e.g. an unbalanced "[" in the Host header
        return False
    return hostname in allowed_hosts()


def same_origin(connection: HTTPConnection) -> bool:
    return _same_origin(connection)


def _same_origin(connection: HTTPConnection) -> bool:
    origin = connection.headers.get("origin")
    if not origin:
        return True  # CLI clients may authenticate with an Authorization header.
    if origin.rstrip("/") in allowed_origins():
        return True
    parsed = urlsplit(origin)
    scheme = connection.headers.get("x-forwarded-proto", connection.url.scheme).split(",")[0].strip()
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    return parsed.scheme == scheme and parsed.netloc.lower() == connection.headers.get("host", "").lower()


# --- Wrong-token throttle -----------------------------------------------------

_failures: dict[str, deque] = {}
_all_failures: deque = deque()
_failures_lock = threading.Lock()


def _hops() -> int:
    try:
        return max(0, int(os.getenv("RAPIDGRID_CLIENT_IP_HOPS", "0") or 0))
    except ValueError:
        return 0


def _pick(parts: list[str]) -> str:
    if not parts:
        return ""
    hops = _hops()
    if hops:
        # Fewer entries than trusted proxies: nothing here can be trusted.
        return parts[-hops] if len(parts) >= hops else ""
    return parts[0]


def client_key(connection: HTTPConnection) -> str:
    """The client a failure is charged to.

    Behind a proxy the socket peer is the proxy itself, and charging every
    visitor to it would let one person's typos lock everyone out. A deployment
    names the header carrying the client address; on Render the default is the
    first X-Forwarded-For entry. A client can forge that entry, which only buys
    it a fresh per-client allowance: the global ceiling still caps the total
    guessing rate.
    """
    default = "x-forwarded-for" if os.getenv("RENDER", "").lower() == "true" else ""
    # An empty value (as a copied .env template leaves it) means "not set".
    header = (os.getenv("RAPIDGRID_CLIENT_IP_HEADER", "").strip() or default).lower()
    if header:
        # Which X-Forwarded-For entry to trust depends on the platform:
        # RAPIDGRID_CLIENT_IP_HOPS=N takes the N-th entry from the right (the
        # address the N-th trusted proxy saw), which a client cannot forge.
        # Unset, the first entry is used (forgeable; see the operator check
        # at /api/session/forwarding to choose N on a deployment).
        parts = [p.strip() for p in connection.headers.get(header, "").split(",") if p.strip()]
        value = _pick(parts)
        if value:
            return value[:64]
    return connection.client.host if connection.client else "unknown"


def _prune(entries: deque, now: float) -> None:
    while entries and now - entries[0] >= FAILURE_WINDOW_SECONDS:
        entries.popleft()


def retry_after(key: str) -> int:
    """Seconds until ``key`` may try again, or 0 when it is not throttled."""
    now = time.monotonic()
    with _failures_lock:
        _prune(_all_failures, now)
        mine = _failures.get(key)
        if mine is not None:
            _prune(mine, now)
            if not mine:
                _failures.pop(key, None)
        waits = []
        if mine and len(mine) >= MAX_FAILURES_PER_CLIENT:
            waits.append(FAILURE_WINDOW_SECONDS - (now - mine[0]))
        if len(_all_failures) >= MAX_FAILURES_TOTAL:
            waits.append(FAILURE_WINDOW_SECONDS - (now - _all_failures[0]))
        return max(1, int(max(waits)) + 1) if waits else 0


def record_failure(key: str) -> None:
    now = time.monotonic()
    with _failures_lock:
        if len(_failures) > 1000:
            for stale in [k for k, v in _failures.items() if not v or now - v[-1] >= FAILURE_WINDOW_SECONDS]:
                del _failures[stale]
        _failures.setdefault(key, deque()).append(now)
        _all_failures.append(now)
    logger.warning("Wrong showcase access token")


def reset_throttle() -> None:
    with _failures_lock:
        _failures.clear()
        _all_failures.clear()


# --- Sessions ---------------------------------------------------------------
# A cookie is "<issued_at>.<session id>.<signature>". The signature is keyed on
# the operator token, so changing the token ends every session. Logging out
# records the session id as revoked until it would have expired anyway, so a
# copy of the cookie stops working too.

_revoked: dict[str, float] | None = None
_revoked_lock = threading.Lock()


class RevocationUnavailable(RuntimeError):
    """The revoked-session list could not be read or saved."""


def _revoked_sessions() -> dict[str, float]:
    """The revoked sessions, loaded once. Raises RevocationUnavailable when the
    store cannot be read; nothing is cached then, so the next call retries."""
    global _revoked
    if _revoked is None:
        try:
            import storage

            stored = storage.load_collection(_REVOKED_COLLECTION, {}) or {}
            loaded = {str(k): float(v) for k, v in stored.items()}
        except Exception as error:
            logger.exception("Could not load revoked sessions")
            raise RevocationUnavailable("revoked sessions unavailable") from error
        _revoked = loaded
    return _revoked


def _revoke(session_id: str, expires_at: float) -> None:
    """Revoke a session in this process, then store it.

    The in-process revocation always takes effect. RevocationUnavailable is
    raised if the list could not be loaded or the result could not be saved,
    so logout can report that it may not survive a restart.
    """
    with _revoked_lock:
        try:
            revoked = _revoked_sessions()
        except RevocationUnavailable:
            # Never overwrite the stored list with a partial one; remember the
            # revocation for this process only, then report the failure.
            _pending_revocations[session_id] = expires_at
            raise
        now = time.time()
        for key in [k for k, until in revoked.items() if until <= now]:
            del revoked[key]
        revoked.update(_pending_revocations)
        _pending_revocations.clear()
        revoked[session_id] = expires_at
        snapshot = dict(revoked)
    try:
        import storage

        storage.save_collection(_REVOKED_COLLECTION, snapshot)
    except Exception as error:
        logger.exception("Could not store a revoked session")
        raise RevocationUnavailable("revoked session not stored") from error


# Revocations made while the stored list could not be loaded. They apply in
# this process and are merged into the list once it loads.
_pending_revocations: dict[str, float] = {}


def revoke_session(session_id: str, expires_at: float) -> None:
    """Revoke any signed session id (operator or staff) until it expires."""
    _revoke(session_id, expires_at)


def is_revoked(session_id: str) -> bool:
    """True when revoked, or when the revoked list cannot be read (fail closed)."""
    with _revoked_lock:
        if session_id in _pending_revocations:
            return True
        try:
            return session_id in _revoked_sessions()
        except RevocationUnavailable:
            return True


# --- Chat sockets per session -------------------------------------------------
# A socket opened with a session cookie is closed when that session is revoked,
# so logging out also ends any chat connection it opened.

_socket_sessions: dict[str, set] = {}


def session_id_of(connection: HTTPConnection) -> str | None:
    secret = operator_token()
    session = _valid_session(connection.cookies.get(COOKIE_NAME, ""), secret) if secret else None
    return session[0] if session else None


def register_socket(session_id: str | None, websocket) -> None:
    if session_id:
        _socket_sessions.setdefault(session_id, set()).add(websocket)


def unregister_socket(session_id: str | None, websocket) -> None:
    if session_id and session_id in _socket_sessions:
        _socket_sessions[session_id].discard(websocket)
        if not _socket_sessions[session_id]:
            del _socket_sessions[session_id]


async def close_session_sockets(session_id: str) -> None:
    for websocket in list(_socket_sessions.pop(session_id, ())):
        try:
            await websocket.close(code=1008)
        except Exception:
            pass  # already closed


def _signature(issued_at: str, session_id: str, secret: str) -> str:
    message = "rapidgrid-session:" + issued_at + ":" + session_id
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _session_value() -> str:
    issued_at = str(int(time.time()))
    session_id = secrets.token_urlsafe(18)
    return issued_at + "." + session_id + "." + _signature(issued_at, session_id, operator_token())


def _valid_session(cookie: str, secret: str) -> tuple[str, float] | None:
    """(session id, expiry) for a well-formed, signed, unexpired cookie."""
    parts = cookie.split(".")
    if len(parts) != 3:
        return None
    issued_at, session_id, signature = parts
    if len(issued_at) > 12 or not issued_at.isascii() or not issued_at.isdigit():
        return None
    if not 16 <= len(session_id) <= 64 or not session_id.isascii():
        return None
    age = time.time() - int(issued_at)
    if not 0 <= age < SESSION_SECONDS:
        return None
    if not hmac.compare_digest(signature.encode(), _signature(issued_at, session_id, secret).encode()):
        return None
    return session_id, int(issued_at) + SESSION_SECONDS


def _session_active(cookie: str, secret: str) -> bool:
    session = _valid_session(cookie, secret)
    if session is None:
        return False
    with _revoked_lock:
        if session[0] in _pending_revocations:
            return False
        try:
            return session[0] not in _revoked_sessions()
        except RevocationUnavailable:
            # Without the revoked list a logged-out cookie cannot be told
            # apart, so cookie sign-in fails closed until it loads.
            return False


def authenticated(connection: HTTPConnection) -> bool:
    secret = operator_token()
    if not secret:
        return False
    auth = connection.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        # The raw token is the same secret the unlock form takes, for scripts
        # such as the reset command; it is throttled the same way.
        key = client_key(connection)
        if retry_after(key):
            return False
        if hmac.compare_digest(auth[7:].encode(), secret.encode()):
            return True
        record_failure(key)
        return False
    return _session_active(connection.cookies.get(COOKIE_NAME, ""), secret)


class ShowcaseAccessMiddleware:
    """Protect HTTP and WebSocket paths with identical session rules."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        path = scope.get("path", "").rstrip("/")
        api_path = path == "/api" or path.startswith("/api/")
        socket_path = path.startswith("/ws/")
        # How to refuse depends on the connection, not the path: a plain HTTP
        # request to /ws/... gets an HTTP response.
        is_socket = scope["type"] == "websocket"
        connection = HTTPConnection(scope)
        if path in _DOC_PATHS:
            if mode() == "operator" and not authenticated(connection):
                return await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return await self.app(scope, receive, send)
        if not api_path and not socket_path:
            return await self.app(scope, receive, send)
        if mode() == "demo" and not host_allowed(connection):
            # DNS rebinding: a hostile page can point its own domain at a
            # locally running demo; Host then names that domain and Origin
            # matches it, so the origin check alone passes.
            if is_socket:
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "Unknown host"}, status_code=421)(scope, receive, send)
            return
        method = scope.get("method", "GET")
        if (socket_path or method not in {"GET", "HEAD", "OPTIONS"}) and not _same_origin(connection):
            if is_socket:
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "Cross-origin action refused"}, status_code=403)(scope, receive, send)
            return
        # The access bootstrap stays available so the UI can offer sign-in.
        # A vehicle's tracker app has no browser session: it authenticates
        # with its own secret key (routers/tracker.py).
        if path in ("/api/session", "/api/track") or method == "OPTIONS":
            return await self.app(scope, receive, send)
        protected = mode() == "operator" or (method not in {"GET", "HEAD"} and path in _BULK_ACTIONS)
        if protected and not authenticated(connection):
            if is_socket:
                await send({"type": "websocket.close", "code": 1008})
            else:
                configured = bool(operator_token())
                detail = "Unlock this showcase to continue" if configured else "Operator access is not configured"
                await JSONResponse({"detail": detail}, status_code=401 if configured else 503)(scope, receive, send)
            return
        return await self.app(scope, receive, send)


router = APIRouter(prefix="/api/session", tags=["Showcase access"])


class SessionLogin(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.get("")
async def session_status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    operator = authenticated(request)
    return {"mode": mode(), "authenticated": mode() == "demo" or operator, "operator_authenticated": operator}


@router.post("")
async def login_session(payload: SessionLogin, request: Request, response: Response):
    secret = operator_token()
    if not secret:
        raise HTTPException(status_code=503, detail="Operator access is not configured")
    key = client_key(request)
    wait = retry_after(key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="Too many wrong tokens. Try again later.",
            headers={"Retry-After": str(wait)},
        )
    if not hmac.compare_digest(payload.token.encode(), secret.encode()):
        record_failure(key)
        raise HTTPException(status_code=401, detail="Invalid showcase access token")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE_NAME, _session_value(), max_age=SESSION_SECONDS, httponly=True, secure=secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"mode": mode(), "authenticated": True, "operator_authenticated": True}


@router.delete("")
async def logout_session(request: Request, response: Response):
    try:
        return await _logout(request, response)
    except RevocationUnavailable:
        # The session is revoked in this process, but the record may not
        # survive a restart. Say so, and keep the cookie so the user can retry.
        return JSONResponse(
            {"detail": "Signed out on this server, but it could not be recorded. Try again."},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )


async def _logout(request: Request, response: Response):
    secret = operator_token()
    session = _valid_session(request.cookies.get(COOKIE_NAME, ""), secret) if secret else None
    if session is not None:
        try:
            _revoke(*session)
        finally:
            await close_session_sockets(session[0])
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": mode() == "demo", "operator_authenticated": False}


# --- Response headers ---------------------------------------------------------

# Only directives that cannot break the app: no framing (clickjacking), no
# plugins, no <base> or form redirection. Script and connect sources are left
# open because the voice model and map tiles load from third-party CDNs.
# Framing only by this site itself (the guided demo shows the four role
# screens side by side); no other site can frame it (clickjacking).
CONTENT_SECURITY_POLICY = "frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
_SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), geolocation=(self), microphone=(self)"),
]
HSTS = b"max-age=15552000"


class SecurityHeadersMiddleware:
    """Add browser hardening headers to every HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        https = connection.url.scheme == "https" or connection.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                if https and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", HSTS))
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_with_headers)


@router.get("/forwarding")
async def forwarding_check(request: Request, response: Response):
    """Operator only: the shape of the client-address header on this
    deployment, to choose RAPIDGRID_CLIENT_IP_HOPS. Send a request with a
    made-up X-Forwarded-For; the entries the platform added are those after
    it. Returns counts and whether the made-up value came first, never an
    address."""
    response.headers["Cache-Control"] = "no-store"
    if not authenticated(request):
        raise HTTPException(status_code=401, detail="Operator sign-in required")
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return {
        "entries": len(parts),
        "first_is_documentation_address": bool(parts) and parts[0].startswith("203.0.113."),
        "hops_setting": _hops(),
        "chosen_entry_is_documentation_address": _pick(parts).startswith("203.0.113."),
    }


@router.post("/purge")
async def purge_personal_data(request: Request, response: Response):
    """Operator only, after the event: delete every call, message, position
    trail and citizen account (personal data under the DPDP Act). The
    operator token is the only way in; staff PINs are not enough."""
    response.headers["Cache-Control"] = "no-store"
    if not authenticated(request):
        raise HTTPException(status_code=401, detail="Operator sign-in required")
    # A JSON body cannot be sent cross-site without a CORS preflight (CSRF).
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(status_code=415, detail="Send a JSON request")
    import storage
    from routers.monitoring import reset_event_state

    from routers.chat import manager

    from routers import incident as incident_router

    result = await reset_event_state()
    database = storage.data_path()
    # Under the incident lock: a report that was in flight either committed
    # before this (and is deleted here) or commits after it (and finds its
    # caller's account gone, so it keeps no account details).
    async with incident_router._INCIDENTS_LOCK:
        accounts = storage.clear_citizens(database=database)
        incident_router.ACTIVE_INCIDENTS.clear()
        import fleet
        fleet.reset()  # a unit reserved by a report that came in meanwhile is freed
        incident_router.save_db()
        # Older snapshot layouts kept for rollback also hold callers' details.
        storage.delete_collection("incidents", database=database)
    legacy = database.parent / "incidents_db.json"
    if legacy.exists():
        legacy.unlink()
    for sockets in list(manager.active_connections.values()):
        for ws in list(sockets):
            try:
                await ws.close(code=1001)
            except Exception:
                pass
    manager.active_connections.clear()
    await manager.clear_all()  # waits for any message write in flight
    try:
        await asyncio.to_thread(storage.compact, database=database)
    except storage.CompactIncomplete:
        raise HTTPException(status_code=503, detail="Purge incomplete: the database is busy. Run it again.") from None
    logging.getLogger("geoagentic.security").warning("Personal data purged: %d accounts", accounts)
    return {"status": "purged", "accounts_deleted": accounts, **result}
