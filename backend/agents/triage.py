"""
Emergency triage classifier.

Turns free-text citizen reports into an emergency category and a severity
estimate. This is a transparent weighted-keyword model, not a learned one -
and it is deliberately described that way everywhere it surfaces. Claiming an
ML classifier we do not have would be the same mistake as the fabricated
confidence figures this codebase has been carrying.

Replaces a four-branch keyword check that missed most real phrasings: it
classified "two-wheeler hit by a truck, rider has heavy bleeding from the leg"
as a general, moderate-severity call, because "bleeding" does not contain the
substring "blood" and no collision term was listed. That routed a haemorrhaging
trauma patient to a general hospital instead of a trauma centre.

Design notes
------------
* Every category is scored, then the strongest wins - not first-match-wins,
  which made ordering silently significant.
* Terms are matched on word boundaries, so "arm" no longer fires inside
  "alarm" and "ct" no longer fires inside "accident".
* Severity is a category floor raised by explicit red-flag modifiers
  (unresponsive, not breathing, heavy bleeding) and lowered by de-escalators
  (minor, mild, stable).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from models.schemas import EmergencyType

logger = logging.getLogger("geoagentic.triage")

# Weighted term -> category. Higher weight = more diagnostic of that category.
_CATEGORY_TERMS: dict[EmergencyType, dict[str, float]] = {
    EmergencyType.CARDIAC: {
        "cardiac arrest": 5.0,
        "heart attack": 5.0,
        "cardiac": 4.0,
        "chest pain": 4.0,
        "chest": 2.0,
        "heart": 2.5,
        "crushing": 2.0,
        "palpitation": 2.0,
        "palpitations": 2.0,
        "angina": 3.5,
        "no pulse": 5.0,
        "pulseless": 5.0,
        "cpr": 4.0,
        "defibrillator": 4.0,
        "clutching": 1.5,
        "left arm pain": 2.5,
        "breathless": 1.5,
        "sweating profusely": 1.5,
        "collapsed": 1.5,
    },
    EmergencyType.STROKE: {
        "stroke": 5.0,
        "slurred": 4.0,
        "slurring": 4.0,
        "droop": 4.0,
        "drooping": 4.0,
        "facial droop": 5.0,
        "one side": 3.0,
        "left side weakness": 4.0,
        "right side weakness": 4.0,
        "weakness": 2.0,
        "numbness": 2.5,
        "cannot speak": 3.5,
        "can't speak": 3.5,
        "confused speech": 3.0,
        "vision loss": 2.5,
        "blurred vision": 2.0,
        "paralysis": 3.5,
        "paralysed": 3.5,
        "seizure": 2.5,
        "fits": 2.0,
        "fainted": 1.5,
    },
    EmergencyType.TRAUMA: {
        "accident": 4.0,
        "crash": 4.5,
        "collision": 4.5,
        "hit by": 4.0,
        "knocked down": 4.0,
        "run over": 4.5,
        "rammed": 3.5,
        "bleeding": 4.0,
        "blood": 3.0,
        "bleed": 3.5,
        "haemorrhage": 4.5,
        "hemorrhage": 4.5,
        "fracture": 3.5,
        "broken": 3.0,
        "wound": 3.0,
        "stabbed": 4.5,
        "stab": 4.0,
        "gunshot": 5.0,
        "shot": 3.0,
        "crushed": 3.5,
        "trapped": 3.0,
        "amputat": 5.0,
        "severed": 4.5,
        "head injury": 4.5,
        "spinal": 4.0,
        "fell from": 3.5,
        "fall from": 3.5,
        "two-wheeler": 2.5,
        "bike": 1.5,
        "truck": 2.0,
        "lorry": 2.0,
        "car": 1.0,
        "burn": 3.0,
        "burns": 3.5,
        "scald": 3.0,
    },
}

# Red flags escalate severity regardless of category.
_RED_FLAGS: dict[str, float] = {
    "not breathing": 0.30,
    "unresponsive": 0.28,
    "unconscious": 0.25,
    "no pulse": 0.30,
    "pulseless": 0.30,
    "not responding": 0.22,
    "barely conscious": 0.20,
    "cardiac arrest": 0.30,
    "choking": 0.25,
    "heavy bleeding": 0.18,
    "severe": 0.12,
    "critical": 0.15,
    "profuse": 0.15,
    "gushing": 0.18,
    "turning blue": 0.25,
    "convulsing": 0.18,
    "child": 0.10,
    "infant": 0.15,
    "baby": 0.15,
    "pregnant": 0.12,
    "elderly": 0.06,
    "multiple people": 0.15,
    "trapped": 0.12,
    "gunshot": 0.20,
    "stabbed": 0.18,
}

# De-escalators pull severity back down.
_DE_ESCALATORS: dict[str, float] = {
    "minor": -0.18,
    "mild": -0.15,
    "small": -0.10,
    "stable": -0.15,
    "conscious and talking": -0.15,
    "walking": -0.10,
    "not serious": -0.20,
    "slight": -0.12,
}

_CATEGORY_FLOOR: dict[EmergencyType, float] = {
    EmergencyType.CARDIAC: 0.78,
    EmergencyType.STROKE: 0.80,
    EmergencyType.TRAUMA: 0.68,
    EmergencyType.GENERAL: 0.45,
}

_CATEGORY_DETAIL: dict[EmergencyType, str] = {
    EmergencyType.CARDIAC: "Suspected cardiac event - chest pain or arrest presentation.",
    EmergencyType.STROKE: "Suspected stroke - focal neurological deficit reported.",
    EmergencyType.TRAUMA: "Physical trauma - injury, collision or haemorrhage reported.",
    EmergencyType.GENERAL: "Undifferentiated medical emergency.",
}


@dataclass
class TriageResult:
    emergency_type: EmergencyType
    severity: float
    details: str
    confidence: float
    matched_terms: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    method: str = "weighted-keyword-v1"

    def as_note(self) -> str:
        """One-line provenance string for the incident record."""
        flags = f" Red flags: {', '.join(self.red_flags)}." if self.red_flags else ""
        return (
            f"{self.details} Classified by {self.method} "
            f"(matched: {', '.join(self.matched_terms) or 'no specific terms'})."
            f"{flags}"
        )


def _find(term: str, text: str) -> bool:
    """Word-boundary match, so 'arm' does not fire inside 'alarm'."""
    if " " in term or term.endswith("-"):
        return term in text
    return re.search(rf"\b{re.escape(term)}", text) is not None


def classify(raw_text: str) -> TriageResult:
    """Score every category and return the strongest interpretation."""
    text = (raw_text or "").lower().strip()
    if not text:
        return TriageResult(
            emergency_type=EmergencyType.GENERAL,
            severity=_CATEGORY_FLOOR[EmergencyType.GENERAL],
            details=_CATEGORY_DETAIL[EmergencyType.GENERAL],
            confidence=0.30,
        )

    scores: dict[EmergencyType, float] = {}
    matches: dict[EmergencyType, list[str]] = {}

    for category, terms in _CATEGORY_TERMS.items():
        total = 0.0
        hit: list[str] = []
        for term, weight in terms.items():
            if _find(term, text):
                total += weight
                hit.append(term)
        if total:
            scores[category] = total
            matches[category] = hit

    if not scores:
        category = EmergencyType.GENERAL
        matched: list[str] = []
        # No diagnostic terms at all - low confidence, and say so.
        confidence = 0.35
    else:
        category = max(scores, key=scores.get)
        matched = matches[category]
        top = scores[category]
        runner_up = max((v for k, v in scores.items() if k != category), default=0.0)

        # Confidence needs BOTH strong evidence and clear separation. Scoring
        # separation alone made a single weak term look certain simply because
        # nothing competed with it - "fainted at the bus stop" came back as
        # stroke at 0.88. Evidence strength gates the result instead.
        evidence = min(1.0, top / 8.0)
        separation = (top - runner_up) / top if top else 0.0
        confidence = round(
            max(0.25, min(0.95, 0.25 + 0.5 * evidence + 0.2 * separation * evidence)), 3
        )

    severity = _CATEGORY_FLOOR[category]
    flags: list[str] = []
    for flag, bump in _RED_FLAGS.items():
        if _find(flag, text):
            severity += bump
            flags.append(flag)
    for term, drop in _DE_ESCALATORS.items():
        if _find(term, text):
            severity += drop

    severity = round(max(0.15, min(0.99, severity)), 2)

    result = TriageResult(
        emergency_type=category,
        severity=severity,
        details=_CATEGORY_DETAIL[category],
        confidence=confidence,
        matched_terms=sorted(matched, key=len, reverse=True)[:5],
        red_flags=flags[:4],
    )
    logger.info(
        "Triage: %s severity=%.2f confidence=%.2f (matched %s)",
        category.value,
        severity,
        confidence,
        matched[:4],
    )
    return result
