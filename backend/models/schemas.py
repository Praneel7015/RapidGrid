"""
Pydantic schemas for GeoAgentic agent I/O contracts.

Matches the contracts defined in:
  .skills/geoagentic-architecture/references/agent-contracts.md

Conventions (from technology-stack.md):
  - All timestamps UTC, ISO 8601.
  - All coordinates [lat, lng] (NOT GeoJSON [lng, lat]).
  - Every agent output includes `confidence` and `data_freshness`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared enums & primitives
# ---------------------------------------------------------------------------


class DataFreshness(str, Enum):
    """Indicates how current the underlying data is."""

    LIVE = "live"
    CACHED = "cached"
    STALE = "stale"


class VehicleType(str, Enum):
    AMBULANCE = "ambulance"
    FIRE_TRUCK = "fire_truck"
    POLICE = "police"


class EmergencyType(str, Enum):
    """Emergency classification — drives hospital scoring weight selection."""

    GENERAL = "general"
    CARDIAC = "cardiac"
    TRAUMA = "trauma"
    STROKE = "stroke"
    ROAD_ACCIDENT = "road_accident"


class AccessibilityInputModality(str, Enum):
    """Supported input modalities for the Accessibility Agent."""

    TEXT = "text"
    VOICE = "voice"
    SIGN_LANGUAGE = "sign_language"
    SYMBOL = "symbol"
    SOS_BUTTON = "sos_button"


class CommunicationChannel(str, Enum):
    """Notification channels for the Communication Agent."""

    SMS = "sms"
    EMAIL = "email"
    HOSPITAL_API = "hospital_api"
    TRAFFIC_CONTROL = "traffic_control"
    APP_PUSH = "app_push"


class AgentName(str, Enum):
    """Names of the agents in the system."""

    TRAFFIC = "traffic_intelligence"
    ROUTE = "route_optimization"
    HOSPITAL = "hospital_intelligence"
    PREDICTION = "prediction_agent"
    ACCESSIBILITY = "accessibility_agent"
    COMMUNICATION = "communication_agent"
    FUSION = "decision_fusion_engine"
    COORDINATOR = "emergency_coordinator"


class Coordinate(BaseModel):
    """A geographic point — [lat, lng], NOT GeoJSON [lng, lat]."""

    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


# ---------------------------------------------------------------------------
# Traffic Intelligence Agent — contract
# ---------------------------------------------------------------------------


class TrafficRequest(BaseModel):
    """Input to the Traffic Intelligence Agent."""

    bounding_box: dict | None = Field(
        None,
        description=(
            "Geographic bounding box: {north, south, east, west}. "
            "Omit to analyse all loaded segments."
        ),
    )
    time_window: str | None = Field(
        None,
        description="ISO 8601 duration or interval for the analysis window.",
    )


class TrafficIncident(BaseModel):
    """An incident affecting a road segment."""

    incident_id: str
    description: str
    severity: float = Field(..., ge=0, le=1)


class TrafficSegment(BaseModel):
    """
    Output of the Traffic Intelligence Agent — one per road segment.

    From agent-contracts.md:
      {segment_id, congestion_score, incidents[], closures[],
       weather_risk, data_freshness, confidence}
    """

    segment_id: str
    congestion_score: float = Field(
        ...,
        ge=0,
        le=1,
        description="0 = free-flow, 1 = gridlock. Normalised from raw multiplier.",
    )
    incidents: list[TrafficIncident] = Field(default_factory=list)
    closures: list[str] = Field(
        default_factory=list,
        description="IDs of closed road segments.",
    )
    weather_risk: float = Field(
        0.0,
        ge=0,
        le=1,
        description="0 = clear, 1 = severe weather impact.",
    )
    data_freshness: DataFreshness = DataFreshness.LIVE
    confidence: float = Field(..., ge=0, le=1)


class TrafficSnapshot(BaseModel):
    """A full traffic snapshot — collection of segments at a point in time."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    segments: list[TrafficSegment]
    data_freshness: DataFreshness = DataFreshness.LIVE


# ---------------------------------------------------------------------------
# Route Optimization Agent — contract
# ---------------------------------------------------------------------------


class RouteRequest(BaseModel):
    """
    Input to the Route Optimization Agent.

    From agent-contracts.md:
      {origin, destination, vehicle_type, blocked_segments[], traffic_snapshot}
    """

    origin: Coordinate = Field(
        ...,
        description="Starting location [lat, lng].",
    )
    destination: Coordinate = Field(
        ...,
        description="Target location [lat, lng].",
    )
    vehicle_type: VehicleType = VehicleType.AMBULANCE
    blocked_segments: list[str] = Field(
        default_factory=list,
        description="Edge IDs to treat as blocked (in addition to any already blocked in the network).",
    )
    traffic_snapshot: TrafficSnapshot | None = Field(
        None,
        description="Live traffic data. If omitted, uses the latest snapshot from the bus.",
    )


class RouteResponse(BaseModel):
    """
    Output of the Route Optimization Agent — the primary recommended route.

    From agent-contracts.md:
      {route_id, coordinates[], distance, eta, delay_probability,
       alternate_routes[], selection_reason, confidence}

    `alternate_routes` contains full RouteResponse objects (minus their own
    alternates) so the Dispatcher Dashboard can render them on a map with
    full ETA/coordinate/reason detail.
    """

    route_id: str
    coordinates: list[Coordinate] = Field(
        ...,
        min_length=2,
        description="Ordered [lat, lng] waypoints from origin to destination.",
    )
    distance: float = Field(
        ...,
        ge=0,
        description="Total route distance in metres.",
    )
    eta: float = Field(
        ...,
        ge=0,
        description="Estimated travel time in seconds (weighted by multipliers).",
    )
    delay_probability: float = Field(
        ...,
        ge=0,
        le=1,
        description="Probability of delay along this route (0–1).",
    )
    alternate_routes: list[RouteResponse] = Field(
        default_factory=list,
        description=(
            "Full RouteResponse objects for each distinct alternate route. "
            "Each alternate's own alternate_routes is always empty to avoid "
            "infinite recursion. Empty if no distinct alternate exists."
        ),
    )
    selection_reason: str = Field(
        ...,
        description="Plain-language explanation of why this route was selected.",
    )
    data_freshness: DataFreshness = DataFreshness.LIVE
    confidence: float = Field(..., ge=0, le=1)
    node_ids: list[str] = Field(
        default_factory=list,
        description="Ordered node IDs along the route (for debugging / alternate-distinctness checks).",
    )
    edge_ids: list[str] = Field(
        default_factory=list,
        description="Ordered edge IDs along the route (for distinctness verification).",
    )


# Pydantic v2: rebuild model to resolve the self-reference in alternate_routes
RouteResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Prediction Agent — contract
# ---------------------------------------------------------------------------


class PredictionRequest(BaseModel):
    """
    Input to the Prediction Agent.

    From agent-contracts.md:
      {route_id or segment_id, historical_data, current_traffic}
    """

    route_id: str | None = Field(None, description="The route ID to predict ETA for.")
    segment_id: str | None = Field(
        None, description="The segment ID to predict ETA for (if not tracking a full route)."
    )
    historical_data: dict | None = Field(None, description="Historical traffic/delay data.")
    current_traffic: TrafficSnapshot | None = Field(
        None, description="Live traffic snapshot. If omitted, uses the latest bus snapshot."
    )
    route_details: RouteResponse | None = Field(
        None, description="The full route details for tracking ETA updates."
    )


class PredictionResponse(BaseModel):
    """
    Output of the Prediction Agent.

    From agent-contracts.md:
      {eta_estimate, delay_probability, confidence_interval, model_version}
    """

    target_id: str = Field(
        ..., description="The route_id or segment_id this prediction applies to."
    )
    eta_estimate: float = Field(..., ge=0, description="Estimated travel time in seconds.")
    delay_probability: float = Field(..., ge=0, le=1, description="Probability of delay (0-1).")
    confidence_interval: str = Field(..., description="E.g., '+/- 45s'.")
    model_version: str = Field(
        ..., description="Version of the prediction model (e.g., 'v1.0.local')."
    )
    data_freshness: DataFreshness = DataFreshness.LIVE
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Hospital Intelligence Agent — contract
# ---------------------------------------------------------------------------


class HospitalRequest(BaseModel):
    """
    Input to the Hospital Intelligence Agent.

    From agent-contracts.md:
      {incident_location, emergency_type, patient_info?}
    """

    incident_location: Coordinate = Field(
        ...,
        description="Location of the incident [lat, lng].",
    )
    emergency_type: EmergencyType = Field(
        EmergencyType.GENERAL,
        description="Type of emergency — drives which weight table is used.",
    )
    patient_info: dict | None = Field(
        None,
        description=(
            "Optional patient details (age, known conditions). Handled gracefully if absent."
        ),
    )


class ScoreBreakdown(BaseModel):
    """Per-factor scores that produced the hospital's overall score."""

    factor: str = Field(..., description="Name of the scoring factor.")
    weight: float = Field(..., ge=0, le=1, description="Weight applied to this factor.")
    raw_score: float = Field(..., ge=0, le=1, description="Score before weighting (0–1).")
    weighted_score: float = Field(..., ge=0, description="weight × raw_score.")


class HospitalEntry(BaseModel):
    """
    One hospital in the ranked output.

    From agent-contracts.md:
      {hospital_id, score, reasoning, icu_available, specialists[],
       blood_availability}
    """

    hospital_id: str
    name: str
    location: Coordinate
    score: float = Field(
        ...,
        ge=0,
        le=1,
        description="Overall weighted score (higher = better match).",
    )
    score_breakdown: list[ScoreBreakdown] = Field(
        default_factory=list,
        description="Per-factor breakdown — makes scoring explainable.",
    )
    road_eta_seconds: float | None = Field(
        default=None,
        description=(
            "Travel time along the actual road network, from the stage-2 "
            "re-ranking pass. None when the road graph was unavailable and "
            "only the straight-line estimate was used."
        ),
    )
    road_distance_m: float | None = Field(
        default=None,
        description="Distance along the routed road path, in metres.",
    )
    capability_provenance: str = Field(
        default="simulated",
        description=(
            "'curated' when this facility's capability profile comes from the "
            "reviewed table; 'simulated' when it is a modelled estimate. "
            "The UI must label these differently."
        ),
    )
    emergency_receiving: bool = Field(
        default=True,
        description="Whether the facility accepts emergency ambulance arrivals.",
    )
    reasoning: str = Field(
        ...,
        description=(
            "Human-readable explanation of why this hospital scored as it did. "
            "Must reference actual sub-scores, never a generic placeholder."
        ),
    )
    icu_available: int = Field(..., ge=0, description="Number of ICU beds available.")
    specialists: list[str] = Field(
        default_factory=list,
        description="Specialist departments/staff currently on call.",
    )
    blood_availability: float = Field(
        ...,
        ge=0,
        le=1,
        description="Blood supply readiness (0 = none, 1 = fully stocked).",
    )
    data_freshness: DataFreshness = DataFreshness.LIVE
    confidence: float = Field(..., ge=0, le=1)


class HospitalRankingResponse(BaseModel):
    """
    Output of the Hospital Intelligence Agent.

    Returns the full ranked list (not just the top choice) so the
    Decision Fusion Engine and dispatcher can see runner-ups.
    """

    ranked_hospitals: list[HospitalEntry] = Field(
        ...,
        min_length=1,
        description="Hospitals ranked by overall weighted score, descending.",
    )
    emergency_type: EmergencyType
    weights_used: str = Field(
        ...,
        description="Which weight table was applied (e.g. 'general', 'cardiac').",
    )
    data_freshness: DataFreshness = DataFreshness.LIVE


# ---------------------------------------------------------------------------
# Decision Fusion Engine — contract
# ---------------------------------------------------------------------------


class FusionRequest(BaseModel):
    incident_id: str | None = None
    """
    Input to the Decision Fusion Engine.

    Provides enough information for the engine to orchestrate all three
    active agents (Traffic Intelligence, Route Optimization, Hospital
    Intelligence) and fuse their outputs into one action plan.
    """
    incident_location: Coordinate = Field(
        ...,
        description="Location of the incident [lat, lng].",
    )
    emergency_type: EmergencyType = Field(
        EmergencyType.GENERAL,
        description="Type of emergency — affects hospital scoring weights.",
    )
    severity: float = Field(
        0.5,
        ge=0,
        le=1,
        description="Emergency severity (0 = minor, 1 = critical). Affects fusion weights.",
    )
    vehicle_location: Coordinate = Field(
        ...,
        description="Location of the ambulance/vehicle [lat, lng].",
    )
    vehicle_type: VehicleType = VehicleType.AMBULANCE
    patient_info: dict | None = Field(
        None,
        description="Optional patient details (age, known conditions).",
    )
    accessibility_needs: dict | None = Field(
        None,
        description=(
            "Citizen's accessibility requirements (from Accessibility Agent). "
            "Placeholder until the Accessibility Agent is implemented."
        ),
    )


class FactorWeight(BaseModel):
    """
    One dimension of the Decision Fusion Engine's factor breakdown.

    From agent-contracts.md:
      factor_breakdown: {response_time_weight, traffic_weight,
      hospital_weight, severity_weight, accessibility_weight}
    """

    factor: str = Field(..., description="Name of the fusion dimension.")
    weight: float = Field(..., ge=0, le=1, description="Weight applied (sums to 1.0).")
    score: float = Field(..., ge=0, le=1, description="Normalised score for this dimension.")
    weighted_score: float = Field(..., ge=0, description="weight × score.")
    contributing_agent: str = Field(
        ...,
        description="Which agent primarily contributed this score.",
    )
    agent_confidence: float = Field(
        ...,
        ge=0,
        le=1,
        description="The contributing agent's own confidence in its output.",
    )


class FusionResponse(BaseModel):
    """
    Output of the Decision Fusion Engine — one explainable action plan.

    From agent-contracts.md:
      {recommended_route, recommended_hospital, factor_breakdown:
       {response_time_weight, traffic_weight, hospital_weight,
        severity_weight, accessibility_weight},
       explanation, overall_confidence}

    factor_breakdown and explanation are NOT optional — this is what makes
    the system explainable rather than a black box.
    """

    incident_id: str = Field(
        ...,
        description="Unique identifier for this incident.",
    )
    recommended_route: RouteResponse = Field(
        ...,
        description="The fused recommended route.",
    )
    recommended_hospital: HospitalEntry = Field(
        ...,
        description="The fused recommended hospital.",
    )
    factor_breakdown: list[FactorWeight] = Field(
        ...,
        min_length=5,
        description=(
            "Per-dimension breakdown: response_time, traffic, hospital, "
            "severity, accessibility. Makes the decision explainable."
        ),
    )
    explanation: str = Field(
        ...,
        description=(
            "Plain-language explanation of the fused decision. "
            "A dispatcher must be able to see WHY a route/hospital "
            "was chosen, not just WHAT was chosen."
        ),
    )
    overall_confidence: float = Field(
        ...,
        ge=0,
        le=1,
        description="Confidence-weighted aggregate across all agents.",
    )
    emergency_type: EmergencyType
    severity: float = Field(..., ge=0, le=1)
    data_freshness: DataFreshness = DataFreshness.LIVE
    all_hospitals: list[HospitalEntry] = Field(
        default_factory=list,
        description="Full ranked hospital list for dispatcher review.",
    )
    alternate_routes: list[RouteResponse] = Field(
        default_factory=list,
        description="Alternate routes for dispatcher review.",
    )
    clinical_outcome: dict | None = Field(
        default=None,
        description=(
            "Modelled time-to-definitive-treatment for the chosen hospital and "
            "the runner-up (door-to-balloon, door-to-needle, golden hour). "
            "Estimated from guideline targets, not measured - always labelled "
            "as simulated in the UI."
        ),
    )


# ---------------------------------------------------------------------------
# Accessibility Agent — contract
# ---------------------------------------------------------------------------


class AccessibilityProfile(BaseModel):
    """Profile of the citizen reporting or involved in the incident."""

    primary_language: str = "English"
    preferred_communication: AccessibilityInputModality = AccessibilityInputModality.TEXT
    needs_sign_interpreter: bool = False
    needs_wheelchair_access: bool = False
    other_accommodations: list[str] = Field(default_factory=list)


class IncidentReport(BaseModel):
    """
    The normalized output representing an incident, regardless of input modality.
    This is what the rest of the system (Fusion Engine, etc.) consumes.
    """

    incident_type: EmergencyType = EmergencyType.GENERAL
    location: Coordinate
    severity_estimate: float = Field(0.5, ge=0, le=1)
    patient_count: int = 1
    extracted_details: str = Field(
        ..., description="Normalized text describing the incident details."
    )


class AccessibilityRequest(BaseModel):
    """
    Input to the Accessibility Agent.
    Accepts raw citizen input via any modality.
    """

    raw_input: Any = Field(
        ..., description="The raw input data (text string, mock video descriptor, etc.)."
    )
    modality: AccessibilityInputModality
    location: Coordinate = Field(..., description="Device location (auto-captured).")
    known_citizen_profile: AccessibilityProfile | None = None


class AccessibilityResponse(BaseModel):
    """
    Output of the Accessibility Agent.

    From agent-contracts.md:
      {incident_report, citizen_accessibility_profile, input_modality, confidence}
    """

    incident_report: IncidentReport
    citizen_accessibility_profile: AccessibilityProfile
    input_modality: AccessibilityInputModality
    confidence: float = Field(
        ..., ge=0, le=1, description="Confidence in the translation/normalization."
    )
    data_freshness: DataFreshness = DataFreshness.LIVE


# ---------------------------------------------------------------------------
# Communication Agent — contract
# ---------------------------------------------------------------------------


class DeliveryReceipt(BaseModel):
    """Receipt for a single notification dispatch."""

    recipient: str = Field(..., description="E.g., 'Hospital ER', 'Driver', 'Family'")
    channel: CommunicationChannel
    status: str = Field(..., description="'delivered', 'failed', 'pending'")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    error_message: str | None = None


class CommunicationRequest(BaseModel):
    """Input to the Communication Agent."""

    action_plan: dict = Field(..., description="The approved action plan to dispatch.")


class CommunicationResponse(BaseModel):
    """Output of the Communication Agent."""

    receipts: list[DeliveryReceipt]
    overall_status: str = Field(..., description="E.g., 'completed_with_errors', 'success'")
    data_freshness: DataFreshness = DataFreshness.LIVE


# ---------------------------------------------------------------------------
# Emergency Coordinator — contract
# ---------------------------------------------------------------------------


class CoordinatorRequest(BaseModel):
    """Input to the Emergency Coordinator."""

    incident_report: IncidentReport
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class CoordinatorResponse(BaseModel):
    """Output of the Emergency Coordinator."""

    incident_id: str
    severity: float = Field(..., ge=0, le=1)
    activated_agents: list[AgentName]
    status: str = "active"


# ---------------------------------------------------------------------------
# Event bus envelope
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """Envelope for every message on the event bus."""

    event_id: str
    topic: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    payload: dict
    source_agent: str
