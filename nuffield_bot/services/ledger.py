from __future__ import annotations

from collections.abc import Iterable

from nuffield_bot.domain.models import HeldBooking


class BookingLedger:
    """Remembers what the bot has already done, so a loop that revisits the
    same class every second does not re-book it or re-notify about it.

    Kept apart from BookingService so the polling logic is not tangled up with
    the bookkeeping that stops it repeating itself.
    """

    def __init__(self) -> None:
        self._secured: set[str] = set()
        self._failed: set[str] = set()
        self._announced_hold: set[str] = set()
        self._status: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._secured)

    def is_secured(self, class_id: str) -> bool:
        return class_id in self._secured

    def record_secured(self, class_id: str) -> None:
        self._secured.add(class_id)
        self._failed.discard(class_id)

    def should_report_failure(self, class_id: str) -> bool:
        """True only the first time a class fails, so a class that keeps
        failing is retried quietly rather than messaging on every poll."""
        if class_id in self._failed:
            return False
        self._failed.add(class_id)
        return True

    def should_report_hold(self, class_id: str) -> bool:
        """True once per class while waiting for the release, instead of once
        per poll all night."""
        if class_id in self._announced_hold:
            return False
        self._announced_hold.add(class_id)
        return True

    def adopt(self, bookings: Iterable[HeldBooking]) -> int:
        """Treat places we already hold as secured. Lets the dedup survive a
        restart rather than re-booking classes we are already on."""
        held = {b.class_id for b in bookings if b.held}
        self._secured |= held
        self.remember_statuses(bookings)
        return len(held)

    def status_of(self, class_id: str) -> str | None:
        return self._status.get(class_id)

    def remember_statuses(self, bookings: Iterable[HeldBooking]) -> None:
        self._status = {b.class_id: b.status for b in bookings if b.held}
