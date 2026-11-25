"""A stale GPS fix is not used; its exception text is logged, not returned to the client."""

import logging

import fleet
import routers.incident
import routers.units as units


def test_stale_fix_response_hides_exception_text(monkeypatch, caplog):
    monkeypatch.setattr(routers.incident, "in_service_area", lambda lat, lng: True)

    def stale(*args, **kwargs):
        raise fleet.StaleFix("internal detail about unit u1 timing")

    monkeypatch.setattr(fleet, "record_position", stale)
    with caplog.at_level(logging.INFO, logger=units.__name__):
        body = units.store_fix("u1", 12.9, 77.6)

    assert body == {"unit_id": "u1", "ignored": "not used"}
    assert "internal detail about unit u1 timing" in caplog.text


def test_known_stale_reasons_keep_their_notes(monkeypatch):
    monkeypatch.setattr(routers.incident, "in_service_area", lambda lat, lng: True)
    monkeypatch.setattr(fleet, "record_position",
                        lambda *a, **k: (_ for _ in ()).throw(fleet.StaleFix("older than the last fix")))
    assert units.store_fix("u1", 12.9, 77.6)["ignored"] == "older than the last fix"
