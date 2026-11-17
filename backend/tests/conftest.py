"""Shared test settings.

Starlette's TestClient sends ``Host: testserver``. The open demo API answers
only to known host names (DNS-rebinding protection), so the tests name it.
"""

import os

os.environ.setdefault("RAPIDGRID_ALLOWED_HOSTS", "testserver")
