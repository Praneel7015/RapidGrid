"""
Decision Fusion Engine for GeoAgentic.

Contract (from agent-contracts.md):
  Input:  all active agents' latest outputs for one incident.
  Output: {recommended_route, recommended_hospital, factor_breakdown:
           {response_time_weight, traffic_weight, hospital_weight,
            severity_weight, accessibility_weight},
           explanation, overall_confidence}

  factor_breakdown and explanation are NOT optional — this is what makes
  the system explainable rather than a black box.

The five fusion dimensions (from geoagentic-architecture SKILL.md):
  1. Response time   — from Route Optimization Agent (ETA, delay probability)
  2. Traffic          — from Traffic Intelligence Agent (congestion, incidents)
  3. Hospital         — from Hospital Intelligence Agent (resource score)
  4. Severity         — from the incident's classified severity
  5. Accessibility    — from the Accessibility Agent (placeholder for now)

Weights are tunable per emergency type.  Never collapse fusion into a
single opaque score with no per-agent breakdown in the output.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from agents.clinical_outcome import compare as compare_outcomes
from agents.hospital_intelligence import hospital_agent
from agents.prediction import prediction_agent
from agents.route_optimization import route_agent
from agents.traffic_intelligence import traffic_agent
from bus.event_bus import FUSION_COMPLETE, bus
from models.schemas import (
    Coordinate,
    DataFreshness,
    EmergencyType,
    FactorWeight,
    FusionRequest,
    FusionResponse,
    HospitalRequest,
    RouteRequest,
)

logger = logging.getLogger("geoagentic.fusion")

AGENT_NAME = "decision_fusion_engine"


# ---------------------------------------------------------------------------
# Fusion weight tables — tunable per emergency type
# ---------------------------------------------------------------------------
# These are the *fusion-level* weights (combining agents' outputs), distinct
# from the hospital-scoring weights (which are internal to Hospital
# Intelligence).  Higher severity → more weight on response time.

FUSION_WEIGHTS: dict[str, dict[str, float]] = {
    "general": {
        "response_time": 0.30,
        "traffic":       0.20,
        "hospital":      0.25,
        "severity":      0.15,
        "accessibility": 0.10,
    },
    "cardiac": {
        "response_time": 0.25,
        "traffic":       0.15,
        "hospital":      0.35,  # hospital capability critical for cardiac
        "severity":      0.15,
        "accessibility": 0.10,
    },
    "trauma": {
        "response_time": 0.35,  # time-critical
        "traffic":       0.20,
        "hospital":      0.25,
        "severity":      0.10,
        "accessibility": 0.10,
    },
    "stroke": {
        "response_time": 0.30,  # very time-sensitive
        "traffic":       0.15,
        "hospital":      0.30,  # neurology/imaging availability
        "severity":      0.15,
        "accessibility": 0.10,
    },
}


# ---------------------------------------------------------------------------
# DecisionFusionEngine
# ---------------------------------------------------------------------------

class DecisionFusionEngine:
    """
    Combines each agent's independent recommendation into one explainable
    action plan along five weighted dimensions.
    """

    def async_fuse(self):
        """Placeholder for bus-driven fusion — not used in REST flow."""
        pass

    async def fuse(self, request: FusionRequest) -> FusionResponse:
        """
        Orchestrate all three agents and fuse their outputs.

        Steps:
          1. Run Traffic Intelligence for the incident area.
          2. Run Hospital Intelligence to rank hospitals.
          3. Pick the top hospital's node as the routing destination.
          4. Run Route Optimization from vehicle to hospital.
          5. Score each of the five fusion dimensions.
          6. Build factor_breakdown and explanation.
          7. Publish FUSION_COMPLETE to the bus.
        """
        incident_id = request.incident_id or f"INC-{uuid.uuid4().hex[:4]}"
        etype = request.emergency_type.value
        weights = FUSION_WEIGHTS.get(etype, FUSION_WEIGHTS["general"])

        logger.info(
            "Fusion started for %s — %s emergency, severity=%.2f",
            incident_id, etype, request.severity,
        )

        # -- 1. Traffic Intelligence ------------------------------------------
        traffic_snapshot = traffic_agent.get_snapshot()
        traffic_segments = traffic_snapshot.segments

        # Aggregate traffic score: average congestion across all segments
        # 0 = all clear, 1 = all gridlocked
        avg_congestion = (
            sum(s.congestion_score for s in traffic_segments) / len(traffic_segments)
            if traffic_segments else 0.0
        )
        # Invert: high congestion → low traffic score (bad conditions)
        traffic_score = round(1.0 - avg_congestion, 3)
        traffic_confidence = (
            sum(s.confidence for s in traffic_segments) / len(traffic_segments)
            if traffic_segments else 0.5
        )

        # Count active incidents and closures for the explanation
        total_incidents = sum(len(s.incidents) for s in traffic_segments)
        total_closures = sum(len(s.closures) for s in traffic_segments)

        logger.info(
            "Traffic: avg_congestion=%.3f, score=%.3f, incidents=%d, closures=%d",
            avg_congestion, traffic_score, total_incidents, total_closures,
        )

        # -- 2. Hospital Intelligence ----------------------------------------
        hospital_request = HospitalRequest(
            incident_location=request.incident_location,
            emergency_type=request.emergency_type,
            patient_info=request.patient_info,
        )
        hospital_result = await hospital_agent.rank_hospitals(hospital_request)
        top_hospital = hospital_result.ranked_hospitals[0]

        # Hospital score for fusion = the top hospital's weighted score
        hospital_score = round(top_hospital.score, 3)
        hospital_confidence = top_hospital.confidence

        logger.info(
            "Hospital: top=%s (score=%.3f), %d candidates ranked",
            top_hospital.hospital_id, hospital_score,
            len(hospital_result.ranked_hospitals),
        )

        # -- 3. Determine routing destination ---------------------------------
        # We now use the exact hospital coordinates directly
        destination_node = top_hospital.location

        # -- 4. Route Optimization --------------------------------------------
        route_request = RouteRequest(
            origin=request.vehicle_location,
            destination=destination_node,
            vehicle_type=request.vehicle_type,
        )
        route_result = route_agent.compute_route(route_request)

        # Track this route in the Prediction Agent to receive live ETA updates
        prediction_agent.track_route(route_result)
        
        # Pull live weather impact on the ETA
        from models.schemas import PredictionRequest
        pred_req = PredictionRequest(route_id=route_result.route_id, route_details=route_result)
        pred_res = await prediction_agent.predict_on_demand(pred_req)
        
        # Override ETA with the weather-adjusted ETA
        route_result.eta = pred_res.eta_estimate

        # Response time score: inverse of ETA, normalised
        # Lower ETA → higher score.  Max reasonable = 1800s (30 min).
        #
        # A failed route returns eta=0.0, which would otherwise score a PERFECT
        # 1.0 — rewarding the engine for having no route at all.  Detect the
        # degraded case explicitly and score it as the failure it is, so the
        # dispatcher sees low confidence instead of false certainty.
        max_eta = 1800.0
        route_failed = (
            route_result.confidence <= 0.0
            or route_result.eta <= 0.0
            or len(route_result.coordinates) < 2
        )
        if route_failed:
            response_time_score = 0.0
            logger.warning(
                "Route computation DEGRADED for %s — scoring response_time=0.0 "
                "(eta=%.1f, confidence=%.2f, points=%d)",
                incident_id, route_result.eta, route_result.confidence,
                len(route_result.coordinates),
            )
        else:
            response_time_score = round(
                max(0.0, min(1.0, 1.0 - (route_result.eta / max_eta))), 3
            )
        route_confidence = route_result.confidence

        logger.info(
            "Route: %s, ETA=%.0fs, response_time_score=%.3f",
            route_result.route_id, route_result.eta, response_time_score,
        )

        # -- 5. Severity score ------------------------------------------------
        # Directly from the classified severity — higher severity means the
        # situation demands faster action (already 0–1).
        severity_score = round(request.severity, 3)

        # -- 6. Accessibility score -------------------------------------------
        # Placeholder until the Accessibility Agent is implemented.
        # Default to 0.8 (assume standard accessibility met).
        if request.accessibility_needs:
            # If needs are specified, score lower to flag that extra
            # accommodation is required in the action plan
            accessibility_score = 0.6
            accessibility_confidence = 0.7
        else:
            accessibility_score = 0.8
            accessibility_confidence = 0.85

        # -- 7. Build factor breakdown ----------------------------------------
        factor_data = [
            {
                "factor": "response_time",
                "label": "Response Time",
                "weight": weights["response_time"],
                "score": response_time_score,
                "agent": "route_optimization",
                "confidence": route_confidence,
            },
            {
                "factor": "traffic",
                "label": "Traffic Conditions",
                "weight": weights["traffic"],
                "score": traffic_score,
                "agent": "traffic_intelligence",
                "confidence": traffic_confidence,
            },
            {
                "factor": "hospital",
                "label": "Hospital Availability",
                "weight": weights["hospital"],
                "score": hospital_score,
                "agent": "hospital_intelligence",
                "confidence": hospital_confidence,
            },
            {
                "factor": "severity",
                "label": "Emergency Severity",
                "weight": weights["severity"],
                "score": severity_score,
                "agent": "emergency_coordinator",
                "confidence": 0.90,  # severity classification confidence
            },
            {
                "factor": "accessibility",
                "label": "Accessibility",
                "weight": weights["accessibility"],
                "score": accessibility_score,
                "agent": "accessibility",
                "confidence": accessibility_confidence,
            },
        ]

        factor_breakdown: list[FactorWeight] = []
        weighted_total = 0.0
        confidence_weighted_sum = 0.0

        for fd in factor_data:
            weighted = round(fd["weight"] * fd["score"], 4)
            weighted_total += weighted
            confidence_weighted_sum += fd["weight"] * fd["confidence"]

            factor_breakdown.append(FactorWeight(
                factor=fd["label"],
                weight=fd["weight"],
                score=fd["score"],
                weighted_score=weighted,
                contributing_agent=fd["agent"],
                agent_confidence=fd["confidence"],
            ))

        overall_confidence = round(confidence_weighted_sum, 3)

        # -- 8. Build explanation ---------------------------------------------
        explanation = self._build_explanation(
            incident_id=incident_id,
            etype=etype,
            severity=request.severity,
            top_hospital=top_hospital,
            route=route_result,
            factor_breakdown=factor_breakdown,
            weighted_total=weighted_total,
            traffic_score=traffic_score,
            total_incidents=total_incidents,
            total_closures=total_closures,
            ranked_hospitals=hospital_result.ranked_hospitals,
            route_failed=route_failed,
        )

        # -- 8b. Clinical outcome ---------------------------------------------
        # Minutes are a proxy; time-to-definitive-treatment is the outcome the
        # decision actually turns on. This is what makes the specialty bypass
        # legible: driving further can still reach the cath lab sooner, because
        # the nearer hospital would have to transfer the patient onward.
        outcome = None
        ranked = hospital_result.ranked_hospitals
        if len(ranked) >= 2:
            try:
                outcome = compare_outcomes(
                    emergency_type=etype,
                    chosen=ranked[0],
                    alternative=ranked[1],
                    to_patient_seconds=None,
                    chosen_to_hospital_seconds=ranked[0].road_eta_seconds or route_result.eta,
                    alternative_to_hospital_seconds=(
                        ranked[1].road_eta_seconds or route_result.eta
                    ),
                )
                if outcome:
                    logger.info(
                        "Outcome: %s %.0f min vs target %.0f (%s)",
                        outcome["chosen"]["metric"],
                        outcome["chosen"]["total_minutes"],
                        outcome["chosen"]["target_minutes"],
                        outcome["verdict"],
                    )
            except Exception:
                logger.exception("Outcome estimation failed for %s", incident_id)

        # -- 9. Assemble response ---------------------------------------------
        response = FusionResponse(
            incident_id=incident_id,
            recommended_route=route_result,
            recommended_hospital=top_hospital,
            factor_breakdown=factor_breakdown,
            explanation=explanation,
            overall_confidence=min(overall_confidence, 1.0),
            emergency_type=request.emergency_type,
            severity=request.severity,
            data_freshness=(
                DataFreshness.CACHED if route_failed else DataFreshness.LIVE
            ),
            all_hospitals=hospital_result.ranked_hospitals,
            alternate_routes=route_result.alternate_routes,
            clinical_outcome=outcome,
        )

        # -- 10. Publish to bus -----------------------------------------------
        await bus.publish(
            topic=FUSION_COMPLETE,
            payload=response.model_dump(mode="json"),
            source_agent=AGENT_NAME,
        )

        logger.info(
            "Fusion complete for %s — overall_confidence=%.3f, "
            "route=%s (ETA %.0fs), hospital=%s (%s)",
            incident_id, overall_confidence,
            route_result.route_id, route_result.eta,
            top_hospital.hospital_id, top_hospital.name,
        )

        return response

    # -- helpers --------------------------------------------------------------


    def _build_explanation(
        self,
        incident_id: str,
        etype: str,
        severity: float,
        top_hospital: Any,
        route: Any,
        factor_breakdown: list[FactorWeight],
        weighted_total: float,
        traffic_score: float,
        total_incidents: int,
        total_closures: int,
        ranked_hospitals: list[Any] | None = None,
        route_failed: bool = False,
    ) -> str:
        """
        Build a plain-language explanation of the fused decision.

        From SKILL.md: "A dispatcher must be able to see WHY a route or
        hospital was chosen, not just WHAT was chosen."
        """
        severity_label = (
            "critical" if severity >= 0.8
            else "high" if severity >= 0.6
            else "moderate" if severity >= 0.4
            else "low"
        )

        # Sort factors by weighted contribution
        sorted_factors = sorted(
            factor_breakdown, key=lambda f: f.weighted_score, reverse=True,
        )
        top_two = sorted_factors[:2]

        parts = [
            f"Action plan for incident {incident_id} "
            f"({etype} emergency, {severity_label} severity).",
        ]

        # Route reasoning
        parts.append(
            f"ROUTE: {route.selection_reason} "
            f"ETA: {route.eta:.0f}s, distance: {route.distance:.0f}m."
        )

        # Hospital reasoning
        parts.append(
            f"HOSPITAL: {top_hospital.name} selected "
            f"(score {top_hospital.score:.3f}). {top_hospital.reasoning}"
        )

        # Traffic conditions
        if total_incidents > 0 or total_closures > 0:
            parts.append(
                f"TRAFFIC: {total_incidents} active incident(s), "
                f"{total_closures} road closure(s). "
                f"Overall traffic score: {traffic_score:.2f}/1.00."
            )
        else:
            parts.append(
                f"TRAFFIC: No active incidents or closures. "
                f"Traffic score: {traffic_score:.2f}/1.00."
            )

        # Counterfactual: why the runner-up lost.
        #
        # A dispatcher overriding an AI recommendation needs to know what the
        # trade-off was, not just that a number was higher. This states the
        # decision as the choice it actually is: capability against minutes.
        counterfactual = self._build_counterfactual(ranked_hospitals)
        if counterfactual:
            parts.append(counterfactual)

        # Fusion rationale
        factor_desc = " and ".join(
            f"{f.factor} ({f.weighted_score:.3f})" for f in top_two
        )
        parts.append(
            f"FUSION: Decision driven primarily by {factor_desc}. "
            f"Combined weighted score: {weighted_total:.3f}."
        )

        if route_failed:
            parts.append(
                "WARNING: Live routing was unavailable for this plan. ETA and "
                "response-time scoring are degraded - verify before dispatch."
            )

        return " ".join(parts)

    def _build_counterfactual(self, ranked: list[Any] | None) -> str:
        """
        Explain the winner against the runner-up on their largest divergence.

        Returns an empty string when there is no meaningful alternative to
        compare against.
        """
        if not ranked or len(ranked) < 2:
            return ""

        winner, runner_up = ranked[0], ranked[1]

        win_factors = {b.factor: b for b in (winner.score_breakdown or [])}
        run_factors = {b.factor: b for b in (runner_up.score_breakdown or [])}
        shared = set(win_factors) & set(run_factors)
        if not shared:
            return ""

        # The factor where the runner-up most outperforms the winner is what
        # the recommendation is trading away.
        conceded = max(
            shared,
            key=lambda f: run_factors[f].weighted_score - win_factors[f].weighted_score,
        )
        gained = max(
            shared,
            key=lambda f: win_factors[f].weighted_score - run_factors[f].weighted_score,
        )

        concede_gap = (
            run_factors[conceded].weighted_score - win_factors[conceded].weighted_score
        )
        margin = winner.score - runner_up.score

        detail = ""
        if winner.road_eta_seconds and runner_up.road_eta_seconds:
            delta_min = (runner_up.road_eta_seconds - winner.road_eta_seconds) / 60
            if abs(delta_min) >= 0.5:
                direction = "closer" if delta_min > 0 else "further"
                detail = (
                    f" By road, {winner.name} is {abs(delta_min):.1f} min {direction} "
                    f"({winner.road_eta_seconds / 60:.1f} vs "
                    f"{runner_up.road_eta_seconds / 60:.1f} min)."
                )

        if concede_gap <= 0.001:
            return (
                f"ALTERNATIVE CONSIDERED: {runner_up.name} "
                f"(score {runner_up.score:.3f}) was not selected - "
                f"{winner.name} scored higher on every factor.{detail}"
            )

        return (
            f"ALTERNATIVE CONSIDERED: {runner_up.name} scores better on "
            f"{run_factors[conceded].factor} "
            f"({run_factors[conceded].raw_score:.2f} vs "
            f"{win_factors[conceded].raw_score:.2f}), but {winner.name} wins on "
            f"{win_factors[gained].factor} "
            f"({win_factors[gained].raw_score:.2f} vs "
            f"{run_factors[gained].raw_score:.2f}) and takes it by "
            f"{margin:.3f}.{detail}"
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

fusion_engine = DecisionFusionEngine()
