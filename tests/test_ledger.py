from __future__ import annotations

from nuffield_bot.services.ledger import BookingLedger
from tests.fakes import make_booking


def test_securing_a_class_stops_it_being_retried():
    ledger = BookingLedger()
    assert ledger.is_secured("c1") is False

    ledger.record_secured("c1")

    assert ledger.is_secured("c1") is True
    assert len(ledger) == 1


def test_failure_is_reported_once_then_stays_quiet():
    ledger = BookingLedger()

    assert ledger.should_report_failure("c1") is True
    assert ledger.should_report_failure("c1") is False
    assert ledger.should_report_failure("c1") is False


def test_a_later_success_re_arms_failure_reporting():
    """If a class fails, then succeeds, a fresh failure later is worth hearing
    about again."""
    ledger = BookingLedger()
    ledger.should_report_failure("c1")

    ledger.record_secured("c1")

    assert ledger.should_report_failure("c1") is True


def test_hold_is_announced_once_per_class():
    ledger = BookingLedger()

    assert ledger.should_report_hold("c1") is True
    assert ledger.should_report_hold("c1") is False
    assert ledger.should_report_hold("c2") is True


def test_adopt_counts_only_places_actually_held():
    ledger = BookingLedger()

    adopted = ledger.adopt(
        [
            make_booking("booked", status="Booked"),
            make_booking("waiting", status="Waitlist"),
            make_booking("gone", status="User Cancelled", held=False),
        ]
    )

    assert adopted == 2
    assert ledger.is_secured("booked") and ledger.is_secured("waiting")
    # A cancelled booking must stay bookable.
    assert ledger.is_secured("gone") is False


def test_statuses_are_tracked_for_promotion_detection():
    ledger = BookingLedger()
    ledger.adopt([make_booking("c1", status="Waitlist")])

    assert ledger.status_of("c1") == "Waitlist"
    assert ledger.status_of("unknown") is None

    ledger.remember_statuses([make_booking("c1", status="Booked")])

    assert ledger.status_of("c1") == "Booked"
