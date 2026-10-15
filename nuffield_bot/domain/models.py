from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ClassStatus(StrEnum):
    AVAILABLE = "available"
    FULL = "full"
    WAITLIST = "waitlist"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GymClass:
    id: str
    name: str
    start: datetime
    duration_minutes: int
    capacity: int
    booked: int
    status: ClassStatus

    @property
    def spots_left(self) -> int:
        return max(0, self.capacity - self.booked)


@dataclass(frozen=True, slots=True)
class HeldBooking:
    """A place on our account: confirmed, waitlisted, or recently cancelled."""

    class_id: str
    name: str
    start: datetime
    # Raw API wording ("Booked", "Waitlist", "User Cancelled") for messages.
    status: str
    # Whether we currently occupy a place. Cancelled bookings stay listed.
    held: bool
    # Only meaningful when status is Waitlist: the API returns 1 for confirmed
    # and cancelled bookings too.
    waitlist_position: int = 0


class BookingResult(BaseModel):
    success: bool
    class_id: str
    message: str
    # The API can answer WAITLIST for a class the listing showed as available,
    # so only the response tells us which we actually got.
    waitlisted: bool = False
    booked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
