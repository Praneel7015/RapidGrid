"""
Clinical outcome estimation.

Converts a routing decision from "how many minutes" into "will this patient
make the treatment window", which is the question the decision actually turns
on. Minutes are a proxy; time-to-definitive-treatment is the outcome.

The important mechanism here is the secondary transfer. A hospital that cannot
deliver the definitive intervention does not simply cost a few extra minutes -
the patient is stabilised, re-packaged and moved again, which in Indian metros
routinely adds 45-60 minutes. That is why routing 6 minutes further to a
facility with a 24x7 cath lab can be dramatically faster to treatment than the
nearest emergency room, and it is what makes the specialty-bypass rule in
hospital_intelligence.py clinically correct rather than merely defensible.

Every figure below is a MODELLED estimate from published guideline targets and
typical Indian metro process times. None of it is measured from live hospital
systems, and it is labelled `simulated` wherever it surfaces.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger("geoagentic.outcome")


@dataclass(frozen=True)
class Pathway:
    """A time-critical treatment pathway."""

    key: str
    label: str
    intervention: str
    metric: str
    target_minutes: float
    capability: str
    capability_floor: float
    in_hospital_minutes: float
    transfer_penalty_minutes: float
    guideline: str


PATHWAYS: dict[str, Pathway] = {
    "cardiac": Pathway(
        key="cardiac",
        label="STEMI / cardiac arrest",
        intervention="primary angioplasty",
        metric="Door-to-balloon",
        target_minutes=90.0,
        capability="cardiology",
        capability_floor=0.70,
        in_hospital_minutes=32.0,
        transfer_penalty_minutes=52.0,
        guideline="First medical contact to balloon under 90 min",
    ),
    "stroke": Pathway(
        key="stroke",
        label="Acute ischaemic stroke",
        intervention="thrombolysis",
        metric="Door-to-needle",
        target_minutes=60.0,
        capability="neurology",
        capability_floor=0.70,
        in_hospital_minutes=38.0,
        transfer_penalty_minutes=55.0,
        guideline="Door-to-needle under 60 min; window 4.5 h from onset",
    ),
    "trauma": Pathway(
        key="trauma",
        label="Major trauma",
        intervention="damage-control surgery",
        metric="Time to definitive care",
        target_minutes=60.0,
        capability="trauma_centre",
        capability_floor=0.70,
        in_hospital_minutes=28.0,
        transfer_penalty_minutes=48.0,
        guideline="Definitive care within the first hour",
    ),
}

# Time the crew spends on scene: assess, stabilise, package, load.
ON_SCENE_MINUTES = 7.0
# Dispatch decision + crew turnout before the wheels move.
ACTIVATION_MINUTES = 2.5


@dataclass
class OutcomeEstimate:
    pathway: str
    label: str
    metric: str
    intervention: str
    target_minutes: float
    guideline: str

    activation_minutes: float
    to_patient_minutes: float
    on_scene_minutes: float
    to_hospital_minutes: float
    in_hospital_minutes: float
    transfer_minutes: float
    total_minutes: float

    within_target: bool
    margin_minutes: float
    requires_transfer: bool
    capability_score: float
    hospital_name: str
    provenance: str = "simulated"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _capability_of(hospital: Any, key: str) -> float:
    """Read a capability score off either a dict or a HospitalEntry."""
    if isinstance(hospital, dict):
        caps = hospital.get("capabilities") or {}
        if key in caps:
            return float(caps[key])
        for bd in hospital.get("score_breakdown") or []:
            factor = (bd.get("factor") if isinstance(bd, dict) else bd.factor) or ""
            if key.split("_")[0].lower() in factor.lower():
                return float(bd.get("raw_score") if isinstance(bd, dict) else bd.raw_score)
        return 0.0
    caps = getattr(hospital, "capabilities", None)
    if isinstance(caps, dict) and key in caps:
        return float(caps[key])
    for bd in getattr(hospital, "score_breakdown", []) or []:
        if key.split("_")[0].lower() in (bd.factor or "").lower():
            return float(bd.raw_score)
    return 0.0


def _name_of(hospital: Any) -> str:
    if isinstance(hospital, dict):
        return hospital.get("name", "Unknown")
    return getattr(hospital, "name", "Unknown")


def estimate(
    emergency_type: str,
    hospital: Any,
    to_patient_seconds: float | None,
    to_hospital_seconds: float | None,
    capability_override: float | None = None,
) -> OutcomeEstimate | None:
    """
    Estimate time from call to definitive treatment.

    Returns None for emergency types with no time-critical pathway defined -
    better to say nothing than to invent a target.
    """
    pathway = PATHWAYS.get((emergency_type or "").lower())
    if pathway is None:
        return None

    to_patient = (to_patient_seconds or 0) / 60.0
    to_hospital = (to_hospital_seconds or 0) / 60.0

    capability = (
        capability_override
        if capability_override is not None
        else _capability_of(hospital, pathway.capability)
    )
    requires_transfer = capability < pathway.capability_floor

    # A stronger unit turns the patient around faster once through the door;
    # scale between 1.25x and 0.85x of the baseline process time.
    scale = 1.25 - (0.4 * min(1.0, max(0.0, capability)))
    in_hospital = pathway.in_hospital_minutes * scale
    transfer = pathway.transfer_penalty_minutes if requires_transfer else 0.0

    total = (
        ACTIVATION_MINUTES + to_patient + ON_SCENE_MINUTES + to_hospital + in_hospital + transfer
    )

    notes: list[str] = []
    if requires_transfer:
        notes.append(
            f"No {pathway.capability.replace('_', ' ')} capability on site - "
            f"assumes secondary transfer for {pathway.intervention} "
            f"(+{pathway.transfer_penalty_minutes:.0f} min)."
        )
    else:
        notes.append(
            f"{pathway.intervention.capitalize()} available on site — no secondary transfer."
        )
    if total > pathway.target_minutes:
        notes.append(
            f"Exceeds the {pathway.target_minutes:.0f} min target by "
            f"{total - pathway.target_minutes:.0f} min."
        )

    return OutcomeEstimate(
        pathway=pathway.key,
        label=pathway.label,
        metric=pathway.metric,
        intervention=pathway.intervention,
        target_minutes=pathway.target_minutes,
        guideline=pathway.guideline,
        activation_minutes=round(ACTIVATION_MINUTES, 1),
        to_patient_minutes=round(to_patient, 1),
        on_scene_minutes=round(ON_SCENE_MINUTES, 1),
        to_hospital_minutes=round(to_hospital, 1),
        in_hospital_minutes=round(in_hospital, 1),
        transfer_minutes=round(transfer, 1),
        total_minutes=round(total, 1),
        within_target=total <= pathway.target_minutes,
        margin_minutes=round(pathway.target_minutes - total, 1),
        requires_transfer=requires_transfer,
        capability_score=round(capability, 3),
        hospital_name=_name_of(hospital),
        notes=notes,
    )


def compare(
    emergency_type: str,
    chosen: Any,
    alternative: Any,
    to_patient_seconds: float | None,
    chosen_to_hospital_seconds: float | None,
    alternative_to_hospital_seconds: float | None,
) -> dict[str, Any] | None:
    """
    Frame the routing choice as an outcome difference rather than a distance one.

    This is what turns "we drove 6 minutes further" into "we reached treatment
    45 minutes sooner" - the sentence that makes the specialty bypass land.
    """
    a = estimate(emergency_type, chosen, to_patient_seconds, chosen_to_hospital_seconds)
    b = estimate(emergency_type, alternative, to_patient_seconds, alternative_to_hospital_seconds)
    if not a or not b:
        return None

    drive_delta = a.to_hospital_minutes - b.to_hospital_minutes
    outcome_delta = b.total_minutes - a.total_minutes

    if outcome_delta > 1 and drive_delta > 0.5:
        verdict = (
            f"Drove {drive_delta:.1f} min further to reach {a.intervention} "
            f"{outcome_delta:.0f} min sooner."
        )
    elif outcome_delta > 1:
        verdict = f"Reaches {a.intervention} {outcome_delta:.0f} min sooner."
    elif outcome_delta < -1:
        verdict = (
            f"Slower to {a.intervention} by {abs(outcome_delta):.0f} min — chosen on other factors."
        )
    else:
        verdict = f"Comparable time to {a.intervention}."

    return {
        "chosen": a.as_dict(),
        "alternative": b.as_dict(),
        "drive_delta_minutes": round(drive_delta, 1),
        "outcome_delta_minutes": round(outcome_delta, 1),
        "verdict": verdict,
        "provenance": "simulated",
    }
