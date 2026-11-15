"""Run the actual app with isolated storage and deterministic external providers.

Only used by browser tests. No live provider keys, public incidents, or production
service mutations are needed to exercise the four-portal workflow.
"""
from pathlib import Path
import os
import sys

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

os.environ["GOOGLE_ROUTES_API_KEY"] = ""
os.environ["OPENWEATHER_API_KEY"] = ""

from main import app  # noqa: E402


async def catalogue_response(self, url, *args, **kwargs):
    if "overpass" not in str(url):
        raise AssertionError(f"Unexpected external request in browser test: {url}")
    # Exercise the real enrichment, curated capabilities, ranking and offline
    # routing code, with an empty OSM response causing the local seed catalogue.
    return httpx.Response(200, json={"elements": []}, request=httpx.Request("GET", url))


httpx.AsyncClient.get = catalogue_response

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
