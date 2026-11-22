"""Contacts directory: units, crews, hospitals, dispatch and the 112 line.

Real phone numbers and staff PINs never live in the repository. The directory
is read, in order, from:

1. ``RAPIDGRID_CONTACTS``: the JSON itself (a Render secret).
2. ``RAPIDGRID_CONTACTS_FILE``: a path to a JSON file.
3. ``backend/contacts.json``: a git-ignored local file.
4. ``backend/contacts.example.json``: committed placeholders, with no PINs.

Without any staff PIN (the example file), staff sign-in is off and the app
behaves as an open local demo. ``public_*`` helpers return what any screen
may show; phone numbers and PINs are only returned by the staff-scoped
helpers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger("geoagentic.contacts")

_HERE = Path(__file__).resolve().parent
_ID = r"^[A-Za-z0-9_-]{1,40}$"
UnitType = Literal["Ambulance", "Fire Engine", "Police Cruiser", "Disaster Rescue"]


def _phone(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not re.fullmatch(r"\+?[0-9 ()-]{6,20}", value):
        raise ValueError("phone numbers may contain digits, spaces, brackets, '-' and a leading '+'")
    return value


def _pin(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("staff PINs must be exactly 6 digits")
    return value


class Person(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: Optional[str] = None
    _check_phone = field_validator("phone")(_phone)


class Dispatch(Person):
    staff_pin: Optional[str] = None
    _check_pin = field_validator("staff_pin")(_pin)


class Unit(BaseModel):
    unit_id: str = Field(pattern=_ID)
    type: UnitType
    station: str = Field(min_length=1, max_length=120)
    crew: str = Field(default="", max_length=80)
    phone: Optional[str] = None
    staff_pin: Optional[str] = None
    _check_phone = field_validator("phone")(_phone)
    _check_pin = field_validator("staff_pin")(_pin)


class Hospital(BaseModel):
    hospital_id: str = Field(pattern=_ID)
    name: str = Field(min_length=1, max_length=120)
    emergency_phone: Optional[str] = None
    icu_phone: Optional[str] = None
    staff_pin: Optional[str] = None
    _check_phones = field_validator("emergency_phone", "icu_phone")(_phone)
    _check_pin = field_validator("staff_pin")(_pin)


class Directory(BaseModel):
    hotline_112: Optional[Person] = None
    dispatch: Optional[Dispatch] = None
    units: list[Unit] = Field(default_factory=list, max_length=200)
    hospitals: list[Hospital] = Field(default_factory=list, max_length=200)

    @field_validator("units")
    @classmethod
    def _unique_units(cls, units):
        ids = [u.unit_id for u in units]
        if len(ids) != len(set(ids)):
            raise ValueError("unit ids must be unique")
        return units


def _raw() -> tuple[str, str]:
    inline = os.getenv("RAPIDGRID_CONTACTS", "").strip()
    if inline:
        return inline, "RAPIDGRID_CONTACTS"
    for candidate in (os.getenv("RAPIDGRID_CONTACTS_FILE", ""), str(_HERE / "contacts.json"),
                      str(_HERE / "contacts.example.json")):
        if candidate and Path(candidate).is_file():
            return Path(candidate).read_text(encoding="utf-8"), Path(candidate).name
    return "{}", "empty"


@lru_cache(maxsize=1)
def directory() -> Directory:
    text, source = _raw()
    try:
        loaded = Directory.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as error:
        # Never echo the content: it holds phone numbers and PINs.
        logger.error("Contacts directory from %s is invalid (%s error(s)); using none",
                     source, getattr(error, "error_count", lambda: 1)())
        return Directory()
    logger.info("Contacts directory loaded from %s: %d units, %d hospitals",
                source, len(loaded.units), len(loaded.hospitals))
    return loaded


def reload() -> Directory:
    directory.cache_clear()
    return directory()


def staff_pins_configured() -> bool:
    d = directory()
    return bool((d.dispatch and d.dispatch.staff_pin)
                or any(u.staff_pin for u in d.units)
                or any(h.staff_pin for h in d.hospitals))


def staff_pin(role: str, subject: str) -> Optional[str]:
    d = directory()
    if role == "dispatcher":
        return d.dispatch.staff_pin if d.dispatch else None
    if role == "unit":
        return next((u.staff_pin for u in d.units if u.unit_id == subject), None)
    if role == "hospital":
        return next((h.staff_pin for h in d.hospitals if h.hospital_id == subject), None)
    return None


def unit(unit_id: str) -> Optional[Unit]:
    return next((u for u in directory().units if u.unit_id == unit_id), None)


def hospital(hospital_id: str) -> Optional[Hospital]:
    return next((h for h in directory().hospitals if h.hospital_id == hospital_id), None)


def public_units() -> list[dict]:
    """What any screen may show about the fleet: no crew phones, no PINs."""
    return [{"unit_id": u.unit_id, "type": u.type, "station": u.station} for u in directory().units]


def public_hospitals() -> list[dict]:
    return [{"hospital_id": h.hospital_id, "name": h.name} for h in directory().hospitals]


def hotline() -> Optional[dict]:
    h = directory().hotline_112
    return {"name": h.name, "phone": h.phone} if h else None
