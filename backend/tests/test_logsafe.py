"""Unit tests for the single-line log sanitiser."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logsafe import log_safe


def test_control_characters_are_replaced():
    out = log_safe("a\r\nb\tc\x00d\x1be f\x85g")
    assert out == "a b c d e f g"
    assert not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in out)


def test_long_values_are_truncated():
    out = log_safe("x" * 5000)
    assert len(out) == 200 and out.endswith("…")
    assert log_safe("y" * 10, limit=5) == "yyyy…"


def test_non_strings_and_normal_text_pass_through():
    assert log_safe(3.5) == "3.5"
    assert log_safe(None) == "None"
    assert log_safe("INC-abc123 Aster CMI (Hebbal)") == "INC-abc123 Aster CMI (Hebbal)"


def test_log_safe_handles_non_string_values():
    assert log_safe(True) == "True"
    assert log_safe(0.5) == "0.5"
    out = log_safe("a\r\nb\rc\nd")
    assert "\r" not in out and "\n" not in out
    assert out == "a b c d"


FAKE_OWM_KEY = "fake-owm-key-123"


def _prediction_request():
    from models.schemas import Coordinate, PredictionRequest, RouteResponse

    route = RouteResponse(
        route_id="route-1",
        coordinates=[Coordinate(lat=12.97, lng=77.59), Coordinate(lat=12.95, lng=77.61)],
        distance=1000.0,
        eta=600.0,
        delay_probability=0.1,
        selection_reason="test",
        confidence=0.9,
    )
    return PredictionRequest(route_details=route)


def test_weather_call_uses_constant_url_and_params_and_never_logs_key(monkeypatch, caplog):
    import asyncio
    import logging

    import httpx

    from agents.prediction import PredictionAgent

    calls = []

    async def fake_get(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"weather": [{"main": "Rain"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    agent = PredictionAgent()
    agent.api_key = FAKE_OWM_KEY

    response = asyncio.run(agent.predict_on_demand(_prediction_request()))
    assert response.eta_estimate == 750.0  # rain adds 25%

    url, kwargs = calls[0]
    assert url == "https://api.openweathermap.org/data/2.5/weather"
    assert FAKE_OWM_KEY not in url
    params = kwargs["params"]
    assert set(params) == {"lat", "lon", "appid"}
    assert params["lat"] == 12.95 and params["lon"] == 77.61
    assert params["appid"] == FAKE_OWM_KEY

    # Failure path: exception text carries the key; it must not reach any log record.
    async def failing_get(self, url, **kwargs):
        raise RuntimeError(f"boom while calling {url}?appid={FAKE_OWM_KEY}")

    monkeypatch.setattr(httpx.AsyncClient, "get", failing_get)
    caplog.set_level(logging.DEBUG)
    response = asyncio.run(agent.predict_on_demand(_prediction_request()))
    assert response.eta_estimate == 600.0
    messages = [record.getMessage() for record in caplog.records]
    assert any("Weather API failed: RuntimeError" in m for m in messages)
    assert not any(FAKE_OWM_KEY in m for m in messages)
