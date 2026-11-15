"""Shared showcase access gate, deliberately not production identity or RBAC.

An explicit demo remains available for synthetic data. Hosted showcases can use
operator mode, which protects every API and chat socket with an expiring,
HttpOnly session. The secret is configured on the server, never baked into JS.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

COOKIE_NAME = "rapidgrid_operator"
SESSION_SECONDS = 8 * 60 * 60
_BULK_ACTIONS = {"/api/incidents/clear", "/api/city/clear"}


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


def _signature(issued_at: str, secret: str) -> str:
    return hmac.new(secret.encode(), ("rapidgrid-session:" + issued_at).encode(), hashlib.sha256).hexdigest()


def _session_value() -> str:
    issued_at = str(int(time.time()))
    return issued_at + "." + _signature(issued_at, operator_token())


def authenticated(connection: HTTPConnection) -> bool:
    secret = operator_token()
    if not secret:
        return False
    auth = connection.headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), secret.encode()):
        return True
    cookie = connection.cookies.get(COOKIE_NAME, "")
    issued_at, separator, signature = cookie.partition(".")
    if not separator or len(issued_at) > 12 or not issued_at.isascii() or not issued_at.isdigit():
        return False
    age = time.time() - int(issued_at)
    return 0 <= age < SESSION_SECONDS and hmac.compare_digest(signature.encode(), _signature(issued_at, secret).encode())


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
        if not api_path and not socket_path:
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        method = scope.get("method", "GET")
        if (socket_path or method not in {"GET", "HEAD", "OPTIONS"}) and not _same_origin(connection):
            if socket_path:
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "Cross-origin action refused"}, status_code=403)(scope, receive, send)
            return
        # The access bootstrap stays available so the UI can offer sign-in.
        if path == "/api/session" or method == "OPTIONS":
            return await self.app(scope, receive, send)
        protected = mode() == "operator" or (method not in {"GET", "HEAD"} and path in _BULK_ACTIONS)
        if protected and not authenticated(connection):
            if socket_path:
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
    if not hmac.compare_digest(payload.token.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Invalid showcase access token")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE_NAME, _session_value(), max_age=SESSION_SECONDS, httponly=True, secure=secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"mode": mode(), "authenticated": True, "operator_authenticated": True}


@router.delete("")
async def logout_session(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": mode() == "demo", "operator_authenticated": False}
