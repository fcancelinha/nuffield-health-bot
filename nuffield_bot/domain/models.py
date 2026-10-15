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


class BookingResult(BaseModel):
    success: bool
    class_id: str
    message: str
    booked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
