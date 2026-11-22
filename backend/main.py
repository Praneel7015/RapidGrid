"""
RapidGrid — FastAPI application entry point.

Serves the dispatch API (/api/incidents, /api/driver, /api/hospital,
/api/chat, /api/city), decision-inspection endpoints (/api/route/compute,
/api/fusion/combine, /api/hospital/rank), health checks, the chat WebSocket
and, when built, the React frontend from the same origin.

Run with:
  uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from security import (
    SecurityHeadersMiddleware,
    ShowcaseAccessMiddleware,
    allowed_origins,
    authenticated,
    mode as access_mode,
    register_socket,
    router as session_router,
    session_id_of,
    unregister_socket,
)
from logsafe import log_safe
from staff import router as staff_router

from agents.city_state import city_state
from agents.local_router import local_router
from agents.prediction import prediction_agent
from agents.route_optimization import route_agent
from agents.traffic_intelligence import traffic_agent
from routers import chat, city, fusion, hospital, routing, incident, driver, units
import fleet
from agents import simulator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("geoagentic")
# httpx logs every request URL at INFO; the weather URL carries the API key.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup:
      1. Traffic Intelligence loads the road network.
      2. Route Optimization builds its graph from the same data.
      3. Route Optimization subscribes to ROUTE_REQUEST on the bus.
      4. Traffic Intelligence publishes an initial snapshot.
    """
    logger.info("GeoAgentic backend starting up …")

    # Capacity overrides live on the same durable store as incident snapshots.
    hospital.restore_capacity()

    # 1. Load road network into Traffic Intelligence
    traffic_agent.load_network()

    # 1b. Load the offline routing graph so the Route Optimization Agent can
    #     fall back to real pathfinding when the live API is unavailable.
    if local_router.load():
        logger.info(
            "Offline routing tier ready (%s nodes) - live API failures will "
            "degrade to A* instead of straight lines.",
            local_router.meta.get("node_count", "?"),
        )
        # The causal city model runs on the same graph: time of day and
        # weather set the baseline, and closures displace traffic onto
        # neighbouring roads rather than making it disappear.
        if city_state.attach(local_router):
            try:
                import httpx

                if prediction_agent.api_key:
                    async with httpx.AsyncClient(timeout=6.0) as client:
                        resp = await client.get(
                            "https://api.openweathermap.org/data/2.5/weather",
                            params={"lat": 12.9716, "lon": 77.5946,
                                    "appid": prediction_agent.api_key},
                        )
                    if resp.status_code == 200:
                        condition = (resp.json().get("weather") or [{}])[0].get("main")
                        if condition:
                            city_state.set_weather(condition)
                            logger.info("City model seeded with live weather: %s", condition)
            except Exception:
                logger.warning("Could not seed live weather; city model starts clear")
    else:
        logger.warning(
            "Offline routing tier UNAVAILABLE - run scripts/build_road_graph.py"
        )

    # 2. Share the loaded data with Route Optimization (single source of truth)
    route_agent.load_network(
        nodes=traffic_agent.nodes,
        edges=traffic_agent.edges,
    )

    # 3. Register agents on the event bus
    route_agent.register_on_bus()
    prediction_agent.register_on_bus()

    # 4. Publish initial traffic snapshot
    await traffic_agent.publish_snapshot()

    # Units from the contacts directory, with their stored assignments, and
    # the simulator that moves units without a phone sending GPS.
    fleet.load()
    sim_task = asyncio.create_task(simulator.run())

    logger.info("All agents initialised and registered ✓")
    yield  # ← app runs
    sim_task.cancel()
    logger.info("GeoAgentic backend shutting down …")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GeoAgentic API",
    description=(
        "Multi-agent AI platform for emergency vehicle routing. "
        "Coordinates Traffic Intelligence, Route Optimization, and more "
        "through a Decision Fusion Engine."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# A same-origin deployment needs no cross-origin grant. Development origins
# must be explicitly listed; never combine wildcard origins with cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ShowcaseAccessMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # The default handler echoes the rejected input, which cannot be encoded
    # when it is NaN or Infinity and turned a 422 into a 500.
    errors = [
        {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "Invalid value")), "type": str(e.get("type", "value_error"))}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


# Mount routers
app.include_router(session_router)
app.include_router(staff_router)
app.include_router(routing.router)
app.include_router(hospital.router)
app.include_router(fusion.router)
app.include_router(incident.router)
app.include_router(driver.router)
app.include_router(chat.router)
app.include_router(city.router)
app.include_router(units.router)


# ---------------------------------------------------------------------------
# Health endpoint — required by technology-stack.md
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Platform"])
async def health(request: Request):
    """
    Health check endpoint.

    Honest status: degraded when offline graph is missing; unhealthy when
    neither live routing nor offline graph can serve paths. In operator mode a
    visitor who has not unlocked sees only the status line; the routing and
    city-model detail is for operators.
    """
    telemetry = route_agent.stats()
    configured = bool(route_agent.api_key)
    live = telemetry["live_api_status"] == "available"
    offline = bool(local_router.available)
    if not live and not offline:
        status_value = "unhealthy"
    elif not offline or not live:
        status_value = "degraded"
    else:
        status_value = "healthy"

    if access_mode() == "operator" and not authenticated(request):
        return {"status": status_value, "service": "geoagentic-backend"}

    return {
        "status": status_value,
        "service": "geoagentic-backend",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city_model": (
            # Health is public: intervention labels/targets belong behind the
            # operator gate on /api/city/state, not in monitoring responses.
            {key: value for key, value in city_state.snapshot().items() if key != "interventions"}
            if city_state.ready else {"status": "unattached"}
        ),
        "routing": {
            "live_api_key_configured": configured,
            "offline_graph_available": offline,
            **telemetry,
        },
        # The pipeline: triage -> hospital ranking -> routing -> plan.
        "pipeline": ["triage", "hospital_ranking", "routing", "plan"],
    }


@app.get("/health/live", tags=["Platform"])
async def liveness():
    """Process liveness, independent of paid providers and readiness checks."""
    return {"status": "alive"}


from fastapi import WebSocket, WebSocketDisconnect
@app.websocket("/ws/chat/{incident_id}")
async def websocket_chat_endpoint(websocket: WebSocket, incident_id: str):
    if not chat.incident_exists(incident_id):
        await websocket.close(code=1008)
        return
    await chat.manager.connect(websocket, incident_id)
    session_id = session_id_of(websocket)
    register_socket(session_id, websocket)
    try:
        while True:
            try:
                data = await websocket.receive_text()
            except KeyError:
                continue  # a binary frame; this channel carries text only
            if not chat.incident_exists(incident_id):
                # The incident was cleared while this socket was open; a late
                # message must not recreate stored history for it.
                chat.manager.disconnect(websocket, incident_id)
                await websocket.close(code=1008)
                return
            try:
                payload = json.loads(data)
                body = str(payload.get("message") or "").strip()
                role = payload.get("sender_role", "citizen")
                if not body or len(body) > chat.MAX_MESSAGE_CHARS or role not in chat.CHAT_ROLES:
                    continue
                msg_data = chat._build_message(
                    incident_id,
                    role,
                    str(payload.get("sender_name") or "Anonymous")[: chat.MAX_NAME_CHARS],
                    body,
                )
                await chat.manager.broadcast(incident_id, msg_data)
            except Exception:
                logger.exception("WebSocket chat message failed for %s", log_safe(incident_id))
    except WebSocketDisconnect:
        pass
    finally:
        chat.manager.disconnect(websocket, incident_id)
        unregister_socket(session_id, websocket)


# ---------------------------------------------------------------------------
# Frontend — same origin as /api and /ws when the Vite build is present
# ---------------------------------------------------------------------------

_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _safe_frontend_file(full_path: str, dist: Path | None = None) -> str | None:
    # realpath + startswith(root + sep) is the containment check CodeQL
    # recognises as a path-injection sanitiser; it also rejects symlink escapes.
    root = os.path.realpath(dist or _FRONTEND_DIST)
    try:
        candidate = os.path.realpath(os.path.join(root, full_path))
    except ValueError:  # e.g. an embedded NUL byte
        return None
    if not candidate.startswith(root + os.sep):
        return None
    if os.path.isfile(candidate):
        return candidate
    return None


if _FRONTEND_DIST.is_dir():
    _assets = _FRONTEND_DIST / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    async def frontend_index():
        return FileResponse(_FRONTEND_DIST / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend_spa(full_path: str):
        if full_path == "api" or full_path.startswith(("api/", "ws/")):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        found = _safe_frontend_file(full_path)
        if found is not None:
            return FileResponse(found)
        return FileResponse(_FRONTEND_DIST / "index.html")
