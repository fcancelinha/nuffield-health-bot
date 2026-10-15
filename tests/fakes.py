from __future__ import annotations

from datetime import UTC, datetime

from nuffield_bot.domain.models import (
    BookingResult,
    ClassStatus,
    GymClass,
    HeldBooking,
)


class FakeRepo:
    def __init__(
        self,
        classes: list[GymClass] | None = None,
        succeed: bool = True,
        held: set[str] | None = None,
        bookings: list[HeldBooking] | None = None,
    ):
        self._classes = classes or []
        self._succeed = succeed
        self.bookings = bookings if bookings is not None else [
            make_booking(class_id) for class_id in sorted(held or set())
        ]
        self.book_calls: list[str] = []
        self.find_calls = 0
        self.booking_lookups: list[int] = []

    async def find_my_bookings(self) -> list[HeldBooking]:
        self.booking_lookups.append(len(self.bookings))
        return list(self.bookings)

    async def find_upcoming(self, location_id, name_filters, days_ahead):
        self.find_calls += 1
        return list(self._classes)

    async def book(self, class_id: str) -> BookingResult:
        self.book_calls.append(class_id)
        # Mirror the real API: a full-but-waitlistable class answers WAITLIST.
        waitlisted = any(
            c.id == class_id and c.status == ClassStatus.WAITLIST for c in self._classes
        )
        return BookingResult(
            success=self._succeed,
            class_id=class_id,
            waitlisted=waitlisted and self._succeed,
            message="ok" if self._succeed else "full",
        )


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    async def notify(self, message: str) -> None:
        self.messages.append(message)


class FakeClock:
    def __init__(self, now: datetime):
        self._now = now
        self.sleeps: list[float] = []

    def set_now(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def make_booking(
    class_id: str = "c1",
    status: str = "Booked",
    held: bool = True,
    start: datetime | None = None,
    waitlist_position: int = 0,
) -> HeldBooking:
    return HeldBooking(
        class_id=class_id,
        name="Reformer Pilates",
        start=start or datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        status=status,
        held=held,
        waitlist_position=waitlist_position,
    )


def make_class(
    class_id: str = "c1",
    status: ClassStatus = ClassStatus.AVAILABLE,
    capacity: int = 10,
    booked: int = 9,
    start: datetime | None = None,
) -> GymClass:
    return GymClass(
        id=class_id,
        name="Reformer Pilates",
        start=start or datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        duration_minutes=45,
        capacity=capacity,
        booked=booked,
        status=status,
    )
