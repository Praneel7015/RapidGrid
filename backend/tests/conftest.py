"""Shared test settings.

Starlette's TestClient sends ``Host: testserver``. The open demo API answers
only to known host names (DNS-rebinding protection), so every test names it,
whatever the shell exports. A test can override it with monkeypatch.
"""

import pytest


@pytest.fixture(autouse=True)
def _testserver_is_an_allowed_host(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_ALLOWED_HOSTS", "testserver")
