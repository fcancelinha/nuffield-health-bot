from datetime import datetime
from typing import Protocol

from nuffield_bot.domain.models import BookingResult, GymClass, HeldBooking


class ClassRepository(Protocol):
    async def find_upcoming(
        self,
        location_id: str,
        name_filters: list[str],
        days_ahead: int,
    ) -> list[GymClass]: ...

    async def book(self, class_id: str) -> BookingResult: ...

    async def find_my_bookings(self) -> list[HeldBooking]: ...


class Notifier(Protocol):
    async def notify(self, message: str) -> None: ...


class TokenProvider(Protocol):
    async def get_token(self, force_refresh: bool = False) -> str: ...

    async def ensure_valid_for(self, seconds: float) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...