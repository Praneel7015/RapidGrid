"""Slow provider work must not prevent the API loop serving other callers."""
import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.schemas import Coordinate, RouteRequest
from routers import routing


def test_route_provider_does_not_block_event_loop():
    started = threading.Event()
    release = threading.Event()
    response = SimpleNamespace(route_id="async-regression", alternate_routes=[])

    def provider(_request):
        started.set()
        # The event loop below must run to release the provider. Calling this
        # directly from async code times out and fails instead of deadlocking.
        assert release.wait(timeout=2), "Provider blocked the event loop"
        return response

    async def exercise():
        request = RouteRequest(origin=Coordinate(lat=13.035, lng=77.597),
                               destination=Coordinate(lat=12.929, lng=77.5878))
        task = asyncio.create_task(routing.compute_route(request))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        release.set()
        assert await task is response

    try:
        with patch.object(routing.route_agent, "compute_route", side_effect=provider):
            asyncio.run(exercise())
    finally:
        release.set()
        routing._route_cache.pop(response.route_id, None)
