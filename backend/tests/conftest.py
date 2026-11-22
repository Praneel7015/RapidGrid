"""Shared test settings.

Starlette's TestClient sends ``Host: testserver``. The open demo API answers
only to known host names (DNS-rebinding protection), so every test names it,
whatever the shell exports. A test can override it with monkeypatch.

The contacts directory is cached per process; each test starts from the
committed example (no staff PINs) unless it sets its own.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts  # noqa: E402


@pytest.fixture(autouse=True)
def _testserver_is_an_allowed_host(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_ALLOWED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def _example_contacts(monkeypatch):
    # Always the committed example, never a developer's private contacts.json.
    monkeypatch.delenv("RAPIDGRID_CONTACTS", raising=False)
    monkeypatch.setenv("RAPIDGRID_CONTACTS_FILE",
                       str(Path(contacts.__file__).with_name("contacts.example.json")))
    contacts.directory.cache_clear()
    yield
    contacts.directory.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_fleet(monkeypatch, tmp_path):
    """Each test gets its own data directory and a fleet rebuilt from it."""
    import fleet
    from agents import simulator

    monkeypatch.setenv("RAPIDGRID_DATA_DIR", str(tmp_path))
    fleet._units.clear()
    fleet._trails.clear()
    monkeypatch.setattr(fleet, "_loaded", False)
    simulator.reset()
    yield
    fleet._units.clear()
    fleet._trails.clear()
    fleet._loaded = False
