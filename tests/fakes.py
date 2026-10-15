from __future__ import annotations

from datetime import UTC, datetime

from nuffield_bot.domain.models import BookingResult, ClassStatus, GymClass


class FakeRepo:
    def __init__(self, classes: list[GymClass] | None = None, succeed: bool = True):
        self._classes = classes or []
        self._succeed = succeed
        self.book_calls: list[str] = []
        self.find_calls = 0

    async def find_upcoming(self, location_id, name_filters, days_ahead):
        self.find_calls += 1
        return list(self._classes)

    async def book(self, class_id: str) -> BookingResult:
        self.book_calls.append(class_id)
        return BookingResult(
            success=self._succeed,
            class_id=class_id,
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
