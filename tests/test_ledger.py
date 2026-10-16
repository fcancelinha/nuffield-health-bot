from __future__ import annotations

from nuffield_bot.services.ledger import BookingLedger
from tests.fakes import make_booking


def test_failure_is_reported_once_until_the_class_succeeds():
    """The loop revisits a failing class every poll; without this it would
    message once a second during a burst."""
    ledger = BookingLedger()

    assert ledger.should_report_failure("c1") is True
    assert ledger.should_report_failure("c1") is False

    ledger.record_secured("c1")

    assert ledger.is_secured("c1") is True
    assert ledger.should_report_failure("c1") is True


def test_adopt_ignores_cancelled_bookings():
    """Cancelled bookings stay in the upcoming list. Adopting them would
    permanently stop the bot rebooking a class you cancelled."""
    ledger = BookingLedger()

    adopted = ledger.adopt(
        [
            make_booking("booked", status="Booked"),
            make_booking("waiting", status="Waitlist"),
            make_booking("gone", status="User Cancelled", held=False),
        ]
    )

    assert adopted == 2
    assert ledger.is_secured("gone") is False
    assert ledger.status_of("waiting") == "Waitlist"
