"""
Preflight check — run this before a demo, and the moment new API keys land.

    python scripts/preflight.py

Verifies every external dependency and every routing tier, and tells you
exactly what to fix when something is wrong. Exit code 0 means the system is
demo-ready; 1 means a REQUIRED check failed.

Nothing here prints secret values — only key length and a 4-character suffix,
so you can confirm which key is loaded without leaking it into a screen share.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

OK, WARN, FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    marker = {OK: "[ PASS ]", WARN: "[ WARN ]", FAIL: "[ FAIL ]"}[status]
    print(f"{marker}  {name}")
    if detail:
        for line in detail.splitlines():
            print(f"          {line}")


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 58 - len(title)))


# ---------------------------------------------------------------------------
# 1. Environment
# ---------------------------------------------------------------------------


def check_env() -> dict[str, str | None]:
    section("Environment")
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        record(OK, ".env present", f"{env_path}")
    else:
        record(WARN, ".env missing", "Copy .env.example to .env and add your keys.")
    load_dotenv(env_path if env_path.exists() else None)

    keys = {}
    for name, required in [
        ("GOOGLE_ROUTES_API_KEY", False),
        ("OPENWEATHER_API_KEY", False),
        ("GOOGLE_MAPS_JS_API_KEY", False),
    ]:
        value = os.getenv(name)
        keys[name] = value
        if not value:
            record(WARN, f"{name} not set", "Optional - system degrades gracefully.")
        elif value.startswith("your_") or len(value) < 20:
            record(FAIL if required else WARN, f"{name} looks like a placeholder")
            keys[name] = None
        else:
            record(OK, f"{name} loaded", f"length={len(value)}, ends ...{value[-4:]}")
    return keys


# ---------------------------------------------------------------------------
# 2. External APIs
# ---------------------------------------------------------------------------


def check_google_routes(key: str | None) -> None:
    section("Google Routes API")
    if not key:
        record(WARN, "Skipped - no key", "The offline router will serve all requests.")
        return

    try:
        resp = httpx.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
            },
            json={
                "origin": {"location": {"latLng": {"latitude": 13.0350, "longitude": 77.5970}}},
                "destination": {
                    "location": {"latLng": {"latitude": 12.9290, "longitude": 77.5878}}
                },
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
            },
            timeout=20.0,
        )
    except Exception as exc:
        record(WARN, "Network error reaching Google", f"{type(exc).__name__}: {exc}")
        return

    if resp.status_code == 200:
        route = (resp.json().get("routes") or [{}])[0]
        secs = float(str(route.get("duration", "0s")).rstrip("s") or 0)
        km = float(route.get("distanceMeters", 0)) / 1000
        record(
            OK,
            "Live traffic routing active",
            f"Hebbal -> Jayadeva: {km:.1f} km, {secs / 60:.1f} min",
        )
        return

    body = resp.text[:200]
    if resp.status_code in (401, 403):
        record(
            WARN,
            f"HTTP {resp.status_code} - key rejected",
            (
                "FIX (Google Cloud Console, same project as the key):\n"
                "  1. APIs & Services > Library > search 'Routes API' > ENABLE\n"
                "  2. Billing > link a billing account (required even on free tier)\n"
                "  3. APIs & Services > Credentials > your key > API restrictions:\n"
                "     either 'Don't restrict key' or tick 'Routes API'\n"
                "  4. Application restrictions: set to 'None' for a server-side key\n"
                "     (an HTTP-referrer restriction will always reject backend calls)\n"
                "  5. Changes can take 1-2 minutes to propagate; re-run this script\n"
                f"  Server said: {body}"
            ),
        )
    elif resp.status_code == 429:
        record(
            WARN,
            "HTTP 429 - quota exceeded",
            "The 90s route cache reduces this. Offline tier will cover the demo.",
        )
    else:
        record(WARN, f"HTTP {resp.status_code}", body)


def check_openweather(key: str | None) -> None:
    section("OpenWeather API")
    if not key:
        record(WARN, "Skipped - no key", "ETAs will not be weather-adjusted.")
        return
    try:
        resp = httpx.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": 12.9716, "lon": 77.5946, "appid": key},
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            cond = (data.get("weather") or [{}])[0].get("description", "?")
            record(OK, "Weather feed active", f"Bengaluru now: {cond}")
        elif resp.status_code == 401:
            record(
                WARN,
                "HTTP 401 - key rejected",
                "New OpenWeather keys take up to 2 hours to activate.",
            )
        else:
            record(WARN, f"HTTP {resp.status_code}", resp.text[:160])
    except Exception as exc:
        record(WARN, "Network error", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 3. Offline routing tier
# ---------------------------------------------------------------------------


def check_road_graph() -> bool:
    section("Offline routing tier (the demo safety net)")
    from agents.local_router import local_router

    if not local_router.load():
        record(
            FAIL,
            "Road graph missing",
            (
                "FIX: python scripts/build_road_graph.py\n"
                "Without this, a Google outage leaves you with no routes at all."
            ),
        )
        return False

    meta = local_router.meta
    record(
        OK,
        "Road graph loaded",
        f"{meta.get('node_count'):,} nodes / {meta.get('edge_count'):,} edges, "
        f"built {meta.get('built_at', '?')}",
    )

    started = time.time()
    path = local_router.find_path((13.0350, 77.5970), (12.9290, 77.5878))
    if not path:
        record(FAIL, "Pathfinding failed on a known-good route")
        return False

    record(
        OK,
        "A* pathfinding works",
        f"Hebbal -> Jayadeva: {path['distance_m'] / 1000:.1f} km, "
        f"{path['travel_time_s'] / 60:.1f} min, "
        f"{(time.time() - started) * 1000:.0f} ms",
    )

    # Prove that closures actually change the chosen path.
    victims = [
        e
        for e in path["edge_ids"]
        if "Bellary" in local_router.road_names[local_router.edge_name[e]]
    ]
    if victims:
        local_router.block_edges(victims)
        rerouted = local_router.find_path((13.0350, 77.5970), (12.9290, 77.5878))
        local_router.unblock_all()
        if rerouted and set(rerouted["edge_ids"]) != set(path["edge_ids"]):
            delta = (rerouted["travel_time_s"] - path["travel_time_s"]) / 60
            record(
                OK,
                "Disruption rerouting works",
                f"Blocking {len(victims)} Bellary Road segments diverts the "
                f"route ({delta:+.1f} min)",
            )
        else:
            record(WARN, "Closure did not change the route")
    return True


def check_hospitals() -> None:
    section("Hospital intelligence")
    import json

    path = Path(__file__).resolve().parent.parent / "data" / "hospital_capabilities.json"
    if not path.exists():
        record(
            FAIL,
            "hospital_capabilities.json missing",
            "Cardiac/trauma/stroke routing falls back to modelled estimates.",
        )
        return
    profiles = json.loads(path.read_text(encoding="utf-8")).get("hospitals", {})
    receiving = sum(1 for p in profiles.values() if p.get("emergency_receiving"))
    record(
        OK,
        "Curated capability profiles loaded",
        f"{len(profiles)} hospitals, {receiving} emergency-receiving, "
        f"{len(profiles) - receiving} excluded (oncology / maternity / day-surgery)",
    )


# ---------------------------------------------------------------------------
# 4. Application wiring
# ---------------------------------------------------------------------------


def check_app() -> None:
    section("Application")
    try:
        import main  # noqa: F401

        record(OK, "FastAPI app imports cleanly")
    except Exception as exc:
        record(FAIL, "App failed to import", f"{type(exc).__name__}: {exc}")
        return

    try:
        from fastapi.openapi.utils import get_openapi

        get_openapi(title="t", version="1", routes=main.app.routes)
        record(OK, "OpenAPI schema builds", "/docs will render for judges")
    except Exception as exc:
        record(FAIL, "OpenAPI generation broken", f"/docs will 500. {type(exc).__name__}: {exc}")

    frontend = Path(__file__).resolve().parent.parent.parent / "frontend"
    if (frontend / "node_modules").exists():
        record(OK, "Frontend dependencies installed")
    else:
        record(WARN, "Frontend node_modules missing", "FIX: cd frontend && npm install")


def main_() -> int:
    print("=" * 68)
    print("  RapidGrid / GeoAgentic - preflight check")
    print("=" * 68)

    keys = check_env()
    check_google_routes(keys.get("GOOGLE_ROUTES_API_KEY"))
    check_openweather(keys.get("OPENWEATHER_API_KEY"))
    check_road_graph()
    check_hospitals()
    check_app()

    passed = sum(1 for s, _, _ in _results if s == OK)
    warned = sum(1 for s, _, _ in _results if s == WARN)
    failed = sum(1 for s, _, _ in _results if s == FAIL)

    print("\n" + "=" * 68)
    print(f"  {passed} passed, {warned} warnings, {failed} failures")
    if failed:
        print("  NOT demo-ready - resolve the failures above.")
    elif warned:
        print("  Demo-ready. Warnings degrade gracefully (offline tiers cover them).")
    else:
        print("  All systems live.")
    print("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
