"""The container start check (preflight --startup) warns about an open deployment but never blocks boot."""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND / "scripts" / "preflight.py"
EXAMPLE = BACKEND / "contacts.example.json"


def run(**env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("RAPIDGRID_") and k != "RENDER"}
    # The committed example (no PINs) unless a test names another source, never a local contacts.json.
    base["RAPIDGRID_CONTACTS_FILE"] = str(EXAMPLE)
    base.update(env)
    return subprocess.run([sys.executable, str(SCRIPT), "--startup"], env=base,
                          capture_output=True, text=True, timeout=60)


def with_pin():
    return json.dumps({"dispatch": {"name": "Test desk", "staff_pin": str(secrets.randbelow(9 * 10**7) + 10**7)}})


def test_operator_without_pins_warns_and_starts():
    result = run(RAPIDGRID_MODE="operator")
    assert result.returncode == 0
    assert "OPEN DEPLOYMENT: no staff PINs" in result.stderr


def test_render_default_without_pins_warns():
    result = run(RENDER="true")  # mode defaults to operator on Render
    assert result.returncode == 0
    assert "OPEN DEPLOYMENT" in result.stderr


def test_operator_with_pin_is_quiet():
    result = run(RAPIDGRID_MODE="operator", RAPIDGRID_CONTACTS=with_pin())
    assert result.returncode == 0
    assert "WARNING" not in result.stderr


def test_demo_default_is_quiet():
    result = run()
    assert result.returncode == 0
    assert "WARNING" not in result.stderr


def test_demo_with_public_hosts_warns():
    result = run(RAPIDGRID_ALLOWED_HOSTS="demo.example.test")
    assert result.returncode == 0
    assert "demo mode with RAPIDGRID_ALLOWED_HOSTS" in result.stderr


def test_invalid_contacts_fail_closed_so_no_open_warning(monkeypatch):
    # An invalid configured directory locks staff sign-in (no PIN can match):
    # nothing is open, so the open-deployment warning must not fire.
    for bad in ("{not json", json.dumps({"dispatch": {"name": "x", "staff_pin": "12"}})):
        result = run(RAPIDGRID_MODE="operator", RAPIDGRID_CONTACTS=bad)
        assert result.returncode == 0
        assert "OPEN DEPLOYMENT" not in result.stderr
    import contacts
    monkeypatch.setenv("RAPIDGRID_CONTACTS", "{not json")
    assert contacts.reload().invalid and contacts.staff_pins_configured()
    monkeypatch.delenv("RAPIDGRID_CONTACTS")
    contacts.reload()
