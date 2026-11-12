"""Explicit calendar dates outrank relative-weekday and model guesses."""

from datetime import datetime

import pytest

from booking import normalize_appointment_fields
from storage import DUBAI


NOW = datetime(2026, 11, 12, 16, 30, tzinfo=DUBAI)


@pytest.mark.parametrize("message, expected", [
    ("This Saturday 10AM", {"day": "2026-11-14", "time": "10:00"}),
    ("Saturday November 21 at 10am", {"day": "2026-11-21", "time": "10:00"}),
    ("Saturday 2026-11-21 at 10am", {"day": "2026-11-21", "time": "10:00"}),
    ("Saturday, 21st November 2026 at 2:30pm", {"day": "2026-11-21", "time": "14:30"}),
    ("Friday 13 Nov at noon", {"day": "2026-11-13", "time": "12:00"}),
    ("tomorrow at 19:30", {"day": "2026-11-13", "time": "19:30"}),
    ("today at 12am", {"day": "2026-11-12", "time": "00:00"}),
    ("2026-11-21T10:00:00+00:00", {"day": "2026-11-21", "time": "14:00"}),
    ("2026-11-21T10:00:00+04:00", {"day": "2026-11-21", "time": "10:00"}),
])
def test_normalized_fields(message, expected):
    assert normalize_appointment_fields(message, NOW) == expected


@pytest.mark.parametrize("message", [
    "Friday November 21 at 10am",  # Date and claimed weekday conflict.
    "this Saturday November 21 at 10am",  # Relative date conflicts too.
    "November 31 at 10am",
    "10:99am",
    "tomorrow at 10am or 2pm",
    "next Saturday at 10am",
])
def test_ambiguous_or_invalid_fields_require_clarification(message):
    assert normalize_appointment_fields(message, NOW).get("error")

