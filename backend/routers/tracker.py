"""Background GPS from a tracker app on the vehicle's phone.

A browser stops sending GPS when its screen locks or the tab goes to the
background, so a vehicle tracked only by the crew screen goes quiet at
exactly the wrong moments. A tracker app keeps sending with the screen off.
This endpoint speaks the protocol of the free Traccar Client app (Android
and iOS) and of OsmAnd's live tracking:

- query or form fields: ``id``, ``lat``, ``lon``, ``timestamp`` (Unix
  seconds or milliseconds, or ISO 8601), ``accuracy`` (m), ``speed``
  (knots, as that protocol sends it), ``bearing`` or ``heading``;
- or Traccar Client's JSON: ``{"device_id": ..., "location": {"timestamp":
  ..., "coords": {"latitude", "longitude", "accuracy", "speed" (m/s),
  "heading"}}}``.

``id`` (the app's "device identifier") is the vehicle's secret tracker key
from the contacts directory, never its unit id: it is the only credential,
so it is compared in constant time and wrong keys are throttled per client
like a wrong PIN (OWASP A07). Fixes go through the same checks as the crew
screen's (service area, rate, order, age).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response

import contacts
import security

logger = logging.getLogger("geoagentic.tracker")

router = APIRouter(tags=["Fleet"])

_MAX_BODY = 8192
_KNOT_MPS = 0.514444

# Wrong keys are throttled per client in a bucket of their own: a tracker
# with an old key, or many phones behind one carrier address, must not lock
# out operator or staff sign-in (whose global ceiling this does not touch).
_WRONG_KEYS_PER_CLIENT = 20
_WINDOW_S = 600.0
_wrong: dict[str, deque] = {}
_wrong_lock = threading.Lock()


def _wait(client: str) -> int:
    """Seconds this client must wait: it has tried too many DIFFERENT wrong
    keys. One tracker resending an old key counts once, so it cannot lock
    out the other phones behind the same carrier address."""
    now = time.monotonic()
    with _wrong_lock:
        entries = _wrong.get(client)
        while entries and now - entries[0][0] >= _WINDOW_S:
            entries.popleft()
        if entries is not None and not entries:
            _wrong.pop(client, None)
        if entries and len({digest for _at, digest in entries}) >= _WRONG_KEYS_PER_CLIENT:
            return int(_WINDOW_S - (now - entries[0][0])) + 1
    return 0


def _wrong_key(client: str, key: str) -> None:
    now = time.monotonic()
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    with _wrong_lock:
        if len(_wrong) > 5000:  # bounded memory under a spray of addresses
            for stale in [k for k, v in _wrong.items() if not v or now - v[-1][0] >= _WINDOW_S]:
                del _wrong[stale]
        entries = _wrong.setdefault(client, deque(maxlen=1000))
        if not any(d == digest for _at, d in entries):
            entries.append((now, digest))
    logger.warning("Wrong tracker key")


def reset_throttle() -> None:
    with _wrong_lock:
        _wrong.clear()


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> Optional[float]:
    """Epoch seconds from seconds, milliseconds or ISO 8601; None if unreadable."""
    if value is None or value == "":
        return None
    number = _number(value)
    if number is not None:
        return number / 1000.0 if number > 1e11 else number
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fields(query: dict, body: bytes, content_type: str) -> dict:
    """One flat set of fields from whichever form the app sent."""
    fields = dict(query)
    if not body:
        return fields
    if "json" in content_type or body[:1] in (b"{", b"["):
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):  # RecursionError: "[[[[..." nested past the limit
            raise HTTPException(status_code=400, detail="Unreadable body") from None
        if isinstance(data, list):  # a batch: the newest fix is the one that matters
            data = data[-1] if data else {}
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Unreadable body")
        location = data.get("location") or {}
        if isinstance(location, list):
            location = location[-1] if location else {}
        if not isinstance(location, dict):
            location = {}
        coords = location.get("coords") if isinstance(location.get("coords"), dict) else {}
        fields.setdefault("id", data.get("device_id") or data.get("id"))
        fields.update({k: v for k, v in {
            "lat": coords.get("latitude"), "lon": coords.get("longitude"),
            "accuracy": coords.get("accuracy"), "heading": coords.get("heading"),
            "timestamp": location.get("timestamp"),
        }.items() if v is not None})
        if coords.get("speed") is not None:
            fields["speed_mps"] = coords.get("speed")
        return fields
    from urllib.parse import parse_qsl

    fields.update(dict(parse_qsl(body.decode("utf-8", "replace"))))
    return fields


@router.api_route("/api/track", methods=["GET", "POST"])
async def tracker_fix(request: Request):
    if not contacts.trackers_configured():
        raise HTTPException(status_code=404, detail="Not Found")
    client = security.client_key(request)
    wait = _wait(client)
    if wait:
        raise HTTPException(status_code=429, detail="Too many wrong keys", headers={"Retry-After": str(wait)})
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > _MAX_BODY):
        raise HTTPException(status_code=413, detail="Body too large")
    body = b""
    async for chunk in request.stream():  # capped as it arrives, not after
        body += chunk
        if len(body) > _MAX_BODY:
            raise HTTPException(status_code=413, detail="Body too large")
    fields = _fields(dict(request.query_params), body, request.headers.get("content-type", ""))

    key = str(fields.get("id") or "")
    unit_id = contacts.unit_for_tracker(key) if 0 < len(key) <= 128 else None
    if unit_id is None:
        _wrong_key(client, key)
        raise HTTPException(status_code=401, detail="Unknown tracker")

    lat, lng = _number(fields.get("lat")), _number(fields.get("lon", fields.get("lng")))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=422, detail="lat and lon are required")
    accuracy = _number(fields.get("accuracy"))
    accuracy = min(accuracy, 5000.0) if accuracy is not None and accuracy >= 0 else None
    heading = _number(fields.get("heading", fields.get("bearing")))
    heading = heading % 360 if heading is not None and heading >= 0 else None
    speed = _number(fields.get("speed_mps"))
    if speed is None and _number(fields.get("speed")) is not None:
        speed = _number(fields.get("speed")) * _KNOT_MPS
    speed = min(speed, 120.0) if speed is not None and speed >= 0 else None

    from routers.units import store_fix

    try:
        result = store_fix(unit_id, lat, lng, accuracy_m=accuracy, heading=heading,
                           captured_at=_timestamp(fields.get("timestamp")), speed_mps=speed)
    except HTTPException as refused:
        # A fix that cannot be used (outside the area, too soon after the
        # last) is still answered 200: tracker apps resend a refused fix
        # before newer ones, so an error would stall the whole queue.
        logger.info("Tracker fix for %s not used: %s", unit_id, refused.detail)
        return Response(status_code=200)
    if "ignored" in result:
        logger.info("Tracker fix for %s not used: %s", unit_id, result["ignored"])
    # The app only needs a 200; nothing about the unit is echoed to a key holder.
    return Response(status_code=200)
