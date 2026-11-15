"""Make request-controlled values safe to interpolate into a single log line."""
from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def log_safe(value: object, limit: int = 200) -> str:
    """Return ``str(value)`` with control characters (CR, LF, ...) replaced and length capped."""
    # Explicit replace calls are the line-break sanitiser form static analysis (CodeQL) recognises.
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = _CONTROL.sub(" ", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
