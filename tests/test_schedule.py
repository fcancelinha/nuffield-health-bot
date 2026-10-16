from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nuffield_bot.config import Settings, parse_weekdays
from nuffield_bot.services.booking_service import BookingService
from tests.fakes import FakeClock, FakeNotifier, FakeRepo, make_class

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
BURST_NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("mon,wed,fri", {MON, WED, FRI}),
        ("Mon-Fri", {MON, TUE, WED, THU, FRI}),
        ("weekends", {SAT, SUN}),
        # Ranges may wrap around the end of the week.
        ("Fri-Mon", {FRI, SAT, SUN, MON}),
    ],
)
def test_parse_weekdays(spec, expected):
    assert parse_weekdays(spec) == expected


def test_bad_day_spec_raises_rather_than_matching_nothing():
    """A typo must fail loudly: silently selecting no days would leave the bot
    running happily and never booking."""
    with pytest.raises(ValueError):
        parse_weekdays("Munday")
    with pytest.raises(ValueError):
        parse_weekdays("")
    with pytest.raises(ValidationError):
        Settings(class_days="Munday")


@pytest.mark.asyncio
async def test_classes_on_unwanted_days_are_skipped():
    """Baltimore Wharf runs no evening Reformer at weekends, so a Mon-Fri
    schedule should ignore a Saturday class even inside the time window."""
    # 2026-10-19 is a Monday, 2026-10-24 a Saturday; both 12:00 UTC.
    monday = make_class(class_id="mon", start=datetime(2026, 10, 19, 12, 0, tzinfo=UTC))
    saturday = make_class(
        class_id="sat", start=datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    )
    repo = FakeRepo([monday, saturday])
    svc = BookingService(
        repo,
        FakeNotifier(),
        FakeClock(BURST_NOW),
        Settings(dry_run=False, class_days="Mon-Fri"),
    )

    await svc._tick(is_burst=True)

    assert repo.book_calls == ["mon"]


def test_day_is_judged_in_gym_timezone_not_utc():
    """A Monday 00:30 London class is Sunday 23:30 UTC; the gym's day wins."""
    svc = BookingService(
        FakeRepo(),
        FakeNotifier(),
        FakeClock(BURST_NOW),
        Settings(class_days="Mon", class_time_from="00:00", class_time_to="23:59"),
    )
    # 2026-10-18 23:30 UTC == Monday 2026-10-19 00:30 BST
    late = make_class(start=datetime(2026, 10, 18, 23, 30, tzinfo=UTC))

    assert svc._matches_schedule(late) is True
