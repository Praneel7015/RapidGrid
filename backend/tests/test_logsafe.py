"""Unit tests for the single-line log sanitiser."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logsafe import log_safe


def test_control_characters_are_replaced():
    out = log_safe("a\r\nb\tc\x00d\x1be f\x85g")
    assert out == "a  b c d e f g"
    assert not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in out)


def test_long_values_are_truncated():
    out = log_safe("x" * 5000)
    assert len(out) == 200 and out.endswith("…")
    assert log_safe("y" * 10, limit=5) == "yyyy…"


def test_non_strings_and_normal_text_pass_through():
    assert log_safe(3.5) == "3.5"
    assert log_safe(None) == "None"
    assert log_safe("INC-abc123 Aster CMI (Hebbal)") == "INC-abc123 Aster CMI (Hebbal)"
