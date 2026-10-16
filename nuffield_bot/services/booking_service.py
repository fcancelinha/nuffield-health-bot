from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from nuffield_bot.config import Settings
from nuffield_bot.domain.models import ClassStatus, GymClass, HeldBooking
from nuffield_bot.domain.ports import ClassRepository, Clock, Notifier
from nuffield_bot.services.ledger import BookingLedger

HEARTBEAT_INTERVAL = timedelta(minutes=15)
WHEN_FORMAT = "%a %d %b %H:%M %Z"


class BookingService:
    def __init__(
        self,
        repo: ClassRepository,
        notifier: Notifier,
        clock: Clock,
        settings: Settings,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._repo = repo
        self._notifier = notifier
        self._clock = clock
        self._settings = settings
        self._tz = ZoneInfo(settings.timezone)
        self._ledger = BookingLedger()
        self._log = logger or structlog.get_logger()
        self._arm_at = self._next_release_after(clock.now())
        self._last_beat = clock.now() - HEARTBEAT_INTERVAL
        self._booking_check_interval = timedelta(
            seconds=settings.booking_check_interval_seconds
        )
        self._last_booking_check = clock.now()

    def _when(self, moment: datetime) -> str:
        return format(moment.astimezone(self._tz), WHEN_FORMAT)

    async def _find_candidates(self) -> list[GymClass]:
        return await self._repo.find_upcoming(
            location_id=self._settings.location_id,
            name_filters=self._settings.class_name_filters,
            days_ahead=self._settings.target_days_ahead,
        )

    async def run_forever(self) -> None:
        self._log.info(
            "bot.started",
            location=self._settings.location_id,
            class_filters=self._settings.class_name_filters,
            dry_run=self._settings.dry_run,
        )
        await self._preflight()
        while True:
            now = self._clock.now()
            is_burst = self._is_burst_window(now)
            self._heartbeat(now)
            try:
                await self._tick(is_burst)
                await self._check_promotions(now)
            except Exception:
                self._log.exception("tick.failed")
            await self._clock.sleep(self._next_interval(is_burst))

    def _heartbeat(self, now: datetime) -> None:
        if now - self._last_beat < HEARTBEAT_INTERVAL:
            return
        self._last_beat = now
        local = now.astimezone(self._tz)
        self._log.info(
            "heartbeat",
            now=local.isoformat(timespec="seconds"),
            armed=self._is_armed(now),
            mins_to_arm=max(0, round((self._arm_at - local).total_seconds() / 60)),
            booked=len(self._ledger),
        )

    async def _preflight(self) -> None:
        """Exercise auth and the listing call once at boot so a broken token is
        reported immediately rather than discovered at release time."""
        try:
            classes = await self._find_candidates()
        except Exception as exc:
            self._log.exception("preflight.failed")
            await self._notifier.notify(f"⚠️ Startup check failed: {exc!r}")
            return
        held = await self._seed_already_held()
        armed = self._is_armed(self._clock.now())
        self._log.info(
            "preflight.ok", matches=len(classes), armed=armed, already_held=held
        )
        state = "ARMED" if armed else "holding until release"
        await self._notifier.notify(
            f"🤖 Bot up ({state}). Auth OK, {len(classes)} matching class(es) "
            f"{self._settings.target_days_ahead} days out, {held} already held. "
            f"Release {self._settings.release_hour:02d}:"
            f"{self._settings.release_minute:02d} {self._settings.timezone}, "
            f"dry_run={self._settings.dry_run}."
        )

    async def _seed_already_held(self) -> int:
        """Prime the dedup set from places we already hold. Without this a
        restart forgets everything and re-books classes we are already on."""
        bookings = await self._fetch_bookings()
        if bookings is None:
            return 0
        held = self._ledger.adopt(bookings)
        if held:
            self._log.info("preflight.seeded_booked", count=held)
        return held

    async def _fetch_bookings(self) -> list[HeldBooking] | None:
        try:
            return await self._repo.find_my_bookings()
        except Exception:
            # Not fatal: worst case we re-POST and the API rejects the dupe.
            self._log.exception("bookings.lookup_failed")
            return None

    async def _check_promotions(self, now: datetime) -> None:
        """Nuffield sends no email when a waitlist place turns into a real
        booking, so a silent promotion could end in a no-show fee. Poll for the
        status change ourselves, on a slow cadence and never during the burst.
        """
        due = now - self._last_booking_check >= self._booking_check_interval
        if not due or self._is_burst_window(now):
            return
        self._last_booking_check = now
        bookings = await self._fetch_bookings()
        if bookings is None:
            return

        for booking in bookings:
            previous = self._ledger.status_of(booking.class_id)
            if previous is None or previous == booking.status:
                continue
            when = self._when(booking.start)
            self._log.info(
                "booking.status_changed",
                class_id=booking.class_id,
                was=previous,
                now=booking.status,
            )
            if booking.held and previous.casefold() == "waitlist":
                await self._notifier.notify(
                    f"🎉 Off the waitlist: {booking.name} on {when} is now "
                    f"{booking.status}. Turn up or cancel to avoid a no-show."
                )
            elif not booking.held:
                await self._notifier.notify(
                    f"⚠️ {booking.name} on {when} is no longer held "
                    f"(now {booking.status})."
                )
        self._ledger.remember_statuses(bookings)

    def _next_interval(self, is_burst: bool) -> float:
        if not is_burst:
            return self._settings.poll_interval_seconds
        jitter = random.uniform(0, self._settings.burst_jitter_seconds)
        return self._settings.burst_interval_seconds + jitter

    async def _tick(self, is_burst: bool) -> None:
        classes = await self._find_candidates()
        armed = self._is_armed(self._clock.now())

        for cls in classes:
            if self._ledger.is_secured(cls.id) or not self._matches_schedule(cls):
                continue
            if not self._is_takeable(cls):
                if is_burst:
                    self._log.debug(
                        "burst.poll", class_id=cls.id, status=cls.status.value
                    )
                continue
            if armed:
                await self._try_book(cls)
            elif self._ledger.should_report_hold(cls.id):
                self._log.info(
                    "slot.held_until_release",
                    class_id=cls.id,
                    name=cls.name,
                    status=cls.status.value,
                    arms_at=self._arm_at.isoformat(),
                )

    def _is_takeable(self, cls: GymClass) -> bool:
        if cls.status == ClassStatus.AVAILABLE and cls.spots_left > 0:
            return True
        return self._settings.join_waitlist and cls.status == ClassStatus.WAITLIST

    def _release_on(self, day: datetime) -> datetime:
        return day.replace(
            hour=self._settings.release_hour,
            minute=self._settings.release_minute,
            second=0,
            microsecond=0,
        )

    def _next_release_after(self, now: datetime) -> datetime:
        local = now.astimezone(self._tz)
        release = self._release_on(local)
        # Starting right on the drop still counts as that drop; starting well
        # after it means waiting for tomorrow's.
        grace = timedelta(seconds=self._settings.burst_window_seconds)
        if local <= release + grace:
            return release
        return self._release_on(local + timedelta(days=1))

    def _is_armed(self, now: datetime) -> bool:
        """Whether the bot may act yet. Holds until the first release that
        falls after startup, so the container can be started the night before
        without hoovering up whatever happens to be open already."""
        if not self._settings.book_only_after_release:
            return True
        return now.astimezone(self._tz) >= self._arm_at

    def _matches_schedule(self, cls: GymClass) -> bool:
        """Does the class fall on a wanted day, at a wanted time? Both are
        judged in the gym's timezone, not the container's."""
        local = cls.start.astimezone(self._tz)
        if local.weekday() not in self._settings.class_weekdays:
            return False
        return (
            self._settings.class_time_from
            <= local.time()
            <= self._settings.class_time_to
        )

    async def _try_book(self, cls: GymClass) -> None:
        is_waitlist = cls.status == ClassStatus.WAITLIST
        verb = "join waitlist for" if is_waitlist else "book"
        when = self._when(cls.start)
        self._log.info(
            "slot.found",
            class_id=cls.id,
            name=cls.name,
            spots=cls.spots_left,
            waitlist=is_waitlist,
        )
        if self._settings.dry_run:
            self._ledger.record_secured(cls.id)
            self._log.info("booking.dry_run", class_id=cls.id, waitlist=is_waitlist)
            await self._notifier.notify(
                f"🟡 (dry run) Would {verb} {cls.name} at {when}"
            )
            return

        result = await self._repo.book(cls.id)
        if result.success:
            self._ledger.record_secured(cls.id)
            self._log.info("booking.success", **result.model_dump())
            # Trust the response, not the listing: a class that looked open can
            # still come back WAITLIST if it filled before the POST landed.
            prefix = "📋 Joined waitlist for" if result.waitlisted else "✅ Booked"
            await self._notifier.notify(
                f"{prefix} {cls.name} at {when} ({result.message})"
            )
            return

        self._log.warning("booking.failed", class_id=cls.id, message=result.message)
        # Keep retrying, but report only the first failure: the loop revisits a
        # failing class every poll and would otherwise message every second.
        if self._ledger.should_report_failure(cls.id):
            await self._notifier.notify(
                f"❌ Failed to {verb} {cls.name}: {result.message}"
            )

    def _is_burst_window(self, now: datetime) -> bool:
        """Asymmetric on purpose: a short lead for clock skew, then the full
        window after the drop. Bursting long before the release only burns
        rate-limit quota on a day the API has not published yet."""
        local = now.astimezone(self._tz)
        since_release = (local - self._release_on(local)).total_seconds()
        return (
            -self._settings.burst_lead_seconds
            <= since_release
            <= self._settings.burst_window_seconds
        )