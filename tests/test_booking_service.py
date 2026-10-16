from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nuffield_bot.config import Settings
from nuffield_bot.domain.models import BookingResult, ClassStatus
from nuffield_bot.services.booking_service import BookingService
from tests.fakes import (
    FakeClock,
    FakeNotifier,
    FakeRepo,
    make_booking,
    make_class,
)

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


@pytest.mark.asyncio
async def test_available_class_that_comes_back_waitlisted_is_reported_honestly():
    """2026-10-15: the listing said available, the POST answered WAITLIST. We
    must not claim '✅ Booked' for a waitlist place."""

    class WaitlistingRepo(FakeRepo):
        async def book(self, class_id):
            self.book_calls.append(class_id)
            return BookingResult(
                success=True,
                class_id=class_id,
                waitlisted=True,
                message="WAITLIST #1 (ref r)",
            )

    notifier = FakeNotifier()
    svc = BookingService(
        WaitlistingRepo([make_class(status=ClassStatus.AVAILABLE)]),
        notifier,
        FakeClock(BURST_NOW),
        Settings(dry_run=False),
    )

    await svc._tick(is_burst=True)

    assert "waitlist" in notifier.messages[0].casefold()
    assert "✅" not in notifier.messages[0]


@pytest.mark.asyncio
async def test_repeated_booking_failure_notifies_once_but_keeps_trying():
    repo = FakeRepo([make_class()], succeed=False)
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    for _ in range(5):
        await svc._tick(is_burst=True)

    # Still retrying the booking...
    assert len(repo.book_calls) == 5
    # ...but the user is not messaged five times.
    assert len(notifier.messages) == 1
    assert notifier.messages[0].startswith("❌")


@pytest.mark.asyncio
async def test_preflight_seeds_dedup_from_places_already_held():
    """A restart must not re-book a class we are already on."""
    repo = FakeRepo([make_class()], held={"c1"})
    notifier = FakeNotifier()
    svc = build(repo, notifier, FakeClock(BURST_NOW))

    await svc._preflight()
    await svc._tick(is_burst=True)

    assert repo.book_calls == []
    assert "1 already held" in notifier.messages[0]


@pytest.mark.asyncio
async def test_preflight_survives_a_failed_held_lookup():
    class BrokenHeld(FakeRepo):
        async def find_held_class_ids(self):
            raise RuntimeError("500")

    repo = BrokenHeld([make_class()])
    svc = build(repo, FakeNotifier(), FakeClock(BURST_NOW))

    await svc._preflight()
    await svc._tick(is_burst=True)

    # Degrades to the old behaviour rather than refusing to run.
    assert repo.book_calls == ["c1"]


def _promo_svc(repo, notifier, clock):
    return BookingService(repo, notifier, clock, Settings(dry_run=False))


@pytest.mark.asyncio
async def test_waitlist_promotion_is_announced():
    """Nuffield sends no email when a waitlist place becomes a booking, so an
    unnoticed promotion can turn into a no-show fee."""
    repo = FakeRepo(bookings=[make_booking("c1", status="Waitlist")])
    notifier = FakeNotifier()
    clock = FakeClock(OFF_PEAK_NOW)
    svc = _promo_svc(repo, notifier, clock)

    await svc._preflight()
    notifier.messages.clear()

    repo.bookings = [make_booking("c1", status="Booked")]
    clock.set_now(OFF_PEAK_NOW + timedelta(hours=1))
    await svc._check_promotions(clock.now())

    assert len(notifier.messages) == 1
    assert "Off the waitlist" in notifier.messages[0]


@pytest.mark.asyncio
async def test_promotion_is_announced_once_not_every_poll():
    repo = FakeRepo(bookings=[make_booking("c1", status="Waitlist")])
    notifier = FakeNotifier()
    clock = FakeClock(OFF_PEAK_NOW)
    svc = _promo_svc(repo, notifier, clock)
    await svc._preflight()
    notifier.messages.clear()

    repo.bookings = [make_booking("c1", status="Booked")]
    for hour in range(1, 4):
        clock.set_now(OFF_PEAK_NOW + timedelta(hours=hour))
        await svc._check_promotions(clock.now())

    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_losing_a_place_is_announced():
    repo = FakeRepo(bookings=[make_booking("c1", status="Booked")])
    notifier = FakeNotifier()
    clock = FakeClock(OFF_PEAK_NOW)
    svc = _promo_svc(repo, notifier, clock)
    await svc._preflight()
    notifier.messages.clear()

    repo.bookings = [make_booking("c1", status="Class Cancelled", held=False)]
    clock.set_now(OFF_PEAK_NOW + timedelta(hours=1))
    await svc._check_promotions(clock.now())

    assert "no longer held" in notifier.messages[0]


@pytest.mark.asyncio
async def test_promotion_check_is_rate_limited_and_skips_the_burst():
    repo = FakeRepo(bookings=[make_booking("c1", status="Waitlist")])
    clock = FakeClock(OFF_PEAK_NOW)
    svc = _promo_svc(repo, FakeNotifier(), clock)
    await svc._preflight()
    before = len(repo.booking_lookups)

    # Too soon after the last check.
    await svc._check_promotions(clock.now())
    assert len(repo.booking_lookups) == before

    # Due, but inside the release burst: the booking race takes priority.
    clock.set_now(BURST_NOW)
    await svc._check_promotions(clock.now())
    assert len(repo.booking_lookups) == before


def test_burst_does_not_start_long_before_the_drop():
    """2026-10-16: bursting 45s early spent the rate-limit quota on a day the
    API had not published yet, and the 429s began 26s before the release."""
    svc = build(FakeRepo(), FakeNotifier(), FakeClock(BURST_NOW))
    release = BURST_NOW  # 07:00 London

    assert svc._is_burst_window(release - timedelta(seconds=30)) is False
    assert svc._is_burst_window(release - timedelta(seconds=3)) is True
    assert svc._is_burst_window(release) is True
    assert svc._is_burst_window(release + timedelta(seconds=40)) is True
    assert svc._is_burst_window(release + timedelta(seconds=60)) is False


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
