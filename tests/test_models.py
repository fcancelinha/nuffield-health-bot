from __future__ import annotations

from datetime import datetime

from nuffield_bot.adapters.clock import SystemClock
from nuffield_bot.domain.models import BookingResult, ClassStatus
from tests.fakes import make_class


def test_system_clock_now_is_timezone_aware():
    now = SystemClock().now()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None


def test_spots_left_never_negative():
    assert make_class(capacity=10, booked=12).spots_left == 0
    assert make_class(capacity=10, booked=3).spots_left == 7


def test_available_status_value_matches_api_string():
    assert ClassStatus.AVAILABLE.value == "available"


def test_booking_result_booked_at_is_timezone_aware():
    result = BookingResult(success=True, class_id="c1", message="ok")
    assert result.booked_at.tzinfo is not None
