"""
Prediction Agent for GeoAgentic (Live Version).

Contract (from agent-contracts.md):
  Input:  {route_id or segment_id, historical_data, current_traffic}
  Output: {eta_estimate, delay_probability, confidence_interval, model_version}
"""

from __future__ import annotations

import logging
import os

import httpx

from bus.event_bus import TRAFFIC_UPDATE, bus
from models.schemas import (
    DataFreshness,
    Event,
    PredictionRequest,
    PredictionResponse,
    RouteResponse,
    TrafficSnapshot,
)

logger = logging.getLogger("geoagentic.agent.prediction")

AGENT_NAME = "prediction_agent"
MODEL_VERSION = "v2.0.live-weather"


class PredictionAgent:
    def __init__(self) -> None:
        self._tracked_routes: dict[str, RouteResponse] = {}
        self._latest_traffic: TrafficSnapshot | None = None
        from dotenv import load_dotenv

        load_dotenv()
        self.api_key = os.getenv("OPENWEATHER_API_KEY")

    def register_on_bus(self) -> None:
        bus.subscribe(TRAFFIC_UPDATE, self.handle_traffic_update)

    def track_route(self, route: RouteResponse) -> None:
        self._tracked_routes[route.route_id] = route

    async def handle_traffic_update(self, event: Event) -> None:
        pass

    async def predict_on_demand(self, request: PredictionRequest) -> PredictionResponse:
        route = request.route_details
        if not route or not route.coordinates:
            return PredictionResponse(
                target_id="unknown",
                eta_estimate=0.0,
                delay_probability=0.0,
                confidence_interval="+/- 0s",
                model_version=MODEL_VERSION,
                data_freshness=DataFreshness.LIVE,
            )

        # Get the destination coordinate to check weather
        dest = route.coordinates[-1]

        weather_delay_s = 0.0
        weather_condition = "Clear"

        if self.api_key:
            url = f"https://api.openweathermap.org/data/2.5/weather?lat={dest.lat}&lon={dest.lng}&appid={self.api_key}"
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=5.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        weather_array = data.get("weather", [])
                        if weather_array:
                            weather_condition = weather_array[0].get("main", "Clear")

                            # Apply delay multiplier if it's raining (common in Bengaluru)
                            if weather_condition in ["Rain", "Thunderstorm", "Drizzle"]:
                                weather_delay_s = route.eta * 0.25  # 25% delay
                                logger.info(
                                    f"Detected Rain in Bengaluru! Adding {weather_delay_s:.0f}s to ETA."
                                )
            except Exception as e:
                logger.error(f"Weather API failed: {e}")

        new_eta = route.eta + weather_delay_s
        margin = int(new_eta * 0.1)
        interval = f"+/- {margin}s"

        return PredictionResponse(
            target_id=route.route_id,
            eta_estimate=round(new_eta, 1),
            delay_probability=0.2 if weather_delay_s > 0 else 0.05,
            confidence_interval=interval,
            model_version=MODEL_VERSION,
            data_freshness=DataFreshness.LIVE,
        )


prediction_agent = PredictionAgent()
