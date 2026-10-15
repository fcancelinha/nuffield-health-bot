from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nuffield_bot.config import Settings
from nuffield_bot.domain.models import ClassStatus
from nuffield_bot.services.booking_service import BookingService
from tests.fakes import FakeClock, FakeNotifier, FakeRepo, make_class

# 2026-10-13 is BST, so 06:00 UTC == 07:00 Europe/London (the default release time).
BURST_NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)
OFF_PEAK_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def build(repo, notifier, clock, dry_run: bool = False) -> BookingService:
    return BookingService(repo, notifier, clock, Settings(dry_run=dry_run))


@pytest.mark.asyncio
async def test_books_available_slot_and_notifies():
    repo = FakeRepo([make_class()])
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    await svc._tick(is_burst=True)

    assert repo.book_calls == ["c1"]
    assert len(notifier.messages) == 1
    assert notifier.messages[0].startswith("✅")


@pytest.mark.asyncio
async def test_dedup_does_not_rebook():
    repo = FakeRepo([make_class()])
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    await svc._tick(is_burst=True)
    await svc._tick(is_burst=True)

    assert repo.book_calls == ["c1"]


@pytest.mark.asyncio
async def test_failed_booking_notifies_and_is_retryable():
    repo = FakeRepo([make_class()], succeed=False)
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    await svc._tick(is_burst=True)
    await svc._tick(is_burst=True)

    assert repo.book_calls == ["c1", "c1"]  # not deduped, booking never succeeded
    assert all(m.startswith("❌") for m in notifier.messages)


@pytest.mark.asyncio
async def test_full_class_is_skipped():
    repo = FakeRepo([make_class(status=ClassStatus.FULL, booked=10)])
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    await svc._tick(is_burst=True)

    assert repo.book_calls == []
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_class_outside_time_window_is_skipped():
    # 07:00 UTC == 08:00 Europe/London, outside the 11:00-14:00 lunch window.
    morning = make_class(start=datetime(2026, 10, 21, 7, 0, tzinfo=UTC))
    lunch = make_class(class_id="c2")  # defaults to 13:00 local
    repo = FakeRepo([morning, lunch])
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    await svc._tick(is_burst=True)

    assert repo.book_calls == ["c2"]


@pytest.mark.asyncio
async def test_dry_run_notifies_without_booking():
    repo = FakeRepo([make_class()])
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW), dry_run=True)

    await svc._tick(is_burst=True)

    assert repo.book_calls == []
    assert len(notifier.messages) == 1
    assert "dry run" in notifier.messages[0]


@pytest.mark.asyncio
async def test_joins_waitlist_when_enabled():
    repo = FakeRepo([make_class(status=ClassStatus.WAITLIST)])
    notifier = FakeNotifier()
    svc = BookingService(
        repo,
        notifier,
        FakeClock(BURST_NOW),
        Settings(dry_run=False, join_waitlist=True),
    )

    await svc._tick(is_burst=True)

    assert repo.book_calls == ["c1"]
    assert "waitlist" in notifier.messages[0].casefold()


@pytest.mark.asyncio
async def test_skips_waitlist_when_disabled():
    repo = FakeRepo([make_class(status=ClassStatus.WAITLIST)])
    notifier = FakeNotifier()
    svc = BookingService(
        repo,
        notifier,
        FakeClock(BURST_NOW),
        Settings(dry_run=False, join_waitlist=False),
    )

    await svc._tick(is_burst=True)

    assert repo.book_calls == []


@pytest.mark.asyncio
async def test_preflight_reports_auth_ok():
    repo = FakeRepo([make_class()])
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    await svc._preflight()

    assert repo.find_calls == 1
    assert repo.book_calls == []  # preflight never books
    assert "Auth OK" in notifier.messages[0]


@pytest.mark.asyncio
async def test_preflight_reports_failure_without_raising():
    class BrokenRepo(FakeRepo):
        async def find_upcoming(self, location_id, name_filters, days_ahead):
            raise RuntimeError("401 Unauthorized")

    notifier = FakeNotifier()
    svc = build(BrokenRepo(), notifier, FakeClock(BURST_NOW))

    await svc._preflight()

    assert "Startup check failed" in notifier.messages[0]


@pytest.mark.asyncio
async def test_tick_passes_both_class_filters_to_repo():
    class CapturingRepo(FakeRepo):
        async def find_upcoming(self, location_id, name_filters, days_ahead):
            self.seen_filters = name_filters
            return []

    repo = CapturingRepo()
    svc = build(repo, FakeNotifier(), FakeClock(BURST_NOW))

    await svc._tick(is_burst=True)

    assert repo.seen_filters == [
        "Reformer Pilates - Signature",
        "Reformer Pilates - Foundations",
    ]


@pytest.mark.asyncio
async def test_holds_off_booking_before_release():
    # 20:00 UTC the night before == 21:00 London, well before the 07:00 drop.
    night_before = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
    repo = FakeRepo([make_class(status=ClassStatus.WAITLIST)])
    notifier = FakeNotifier()
    svc = BookingService(
        repo, notifier, FakeClock(night_before), Settings(dry_run=False)
    )

    await svc._tick(is_burst=False)

    assert repo.book_calls == []
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_books_once_release_time_passes():
    repo = FakeRepo([make_class()])
    notifier = FakeNotifier()
    clock = FakeClock(datetime(2026, 10, 12, 20, 0, tzinfo=UTC))
    svc = BookingService(repo, notifier, clock, Settings(dry_run=False))

    await svc._tick(is_burst=False)
    assert repo.book_calls == []

    clock.set_now(BURST_NOW)  # 07:00 London
    await svc._tick(is_burst=True)
    assert repo.book_calls == ["c1"]


@pytest.mark.asyncio
async def test_gate_can_be_disabled():
    night_before = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
    repo = FakeRepo([make_class()])
    svc = BookingService(
        repo,
        FakeNotifier(),
        FakeClock(night_before),
        Settings(dry_run=False, book_only_after_release=False),
    )

    await svc._tick(is_burst=False)

    assert repo.book_calls == ["c1"]


def test_burst_window_detection():
    svc = build(FakeRepo(), FakeNotifier(), FakeClock(BURST_NOW))
    assert svc._is_burst_window(BURST_NOW) is True
    assert svc._is_burst_window(OFF_PEAK_NOW) is False


def test_next_interval_switches_with_burst():
    settings = Settings()
    svc = build(FakeRepo(), FakeNotifier(), FakeClock(BURST_NOW))

    assert svc._next_interval(is_burst=False) == settings.poll_interval_seconds

    burst = svc._next_interval(is_burst=True)
    assert settings.burst_interval_seconds <= burst <= (
        settings.burst_interval_seconds + settings.burst_jitter_seconds
    )
