from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from nuffield_bot.config import Settings
from nuffield_bot.domain.models import ClassStatus, GymClass
from nuffield_bot.domain.ports import ClassRepository, Clock, Notifier

HEARTBEAT_INTERVAL = timedelta(minutes=15)


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
        self._booked: set[str] = set()
        self._held: set[str] = set()
        self._log = logger or structlog.get_logger()
        self._arm_at = self._next_release_after(clock.now())
        self._last_beat = clock.now() - HEARTBEAT_INTERVAL

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
            booked=len(self._booked),
        )

    async def _preflight(self) -> None:
        """Exercise auth and the listing call once at boot so a broken token is
        reported immediately rather than discovered at release time."""
        try:
            classes = await self._repo.find_upcoming(
                location_id=self._settings.location_id,
                name_filters=self._settings.class_name_filters,
                days_ahead=self._settings.target_days_ahead,
            )
        except Exception as exc:
            self._log.exception("preflight.failed")
            await self._notifier.notify(f"⚠️ Startup check failed: {exc!r}")
            return
        armed = self._is_armed(self._clock.now())
        self._log.info("preflight.ok", matches=len(classes), armed=armed)
        state = "ARMED" if armed else "holding until release"
        await self._notifier.notify(
            f"🤖 Bot up ({state}). Auth OK, {len(classes)} matching class(es) "
            f"{self._settings.target_days_ahead} days out. "
            f"Release {self._settings.release_hour:02d}:"
            f"{self._settings.release_minute:02d} {self._settings.timezone}, "
            f"dry_run={self._settings.dry_run}."
        )

    def _next_interval(self, is_burst: bool) -> float:
        if not is_burst:
            return self._settings.poll_interval_seconds
        jitter = random.uniform(0, self._settings.burst_jitter_seconds)
        return self._settings.burst_interval_seconds + jitter

    async def _tick(self, is_burst: bool) -> None:
        classes = await self._repo.find_upcoming(
            location_id=self._settings.location_id,
            name_filters=self._settings.class_name_filters,
            days_ahead=self._settings.target_days_ahead,
        )
        armed = self._is_armed(self._clock.now())

        for cls in classes:
            if cls.id in self._booked:
                continue
            if not self._in_time_window(cls):
                continue
            open_now = cls.status == ClassStatus.AVAILABLE and cls.spots_left > 0
            joinable = (
                self._settings.join_waitlist and cls.status == ClassStatus.WAITLIST
            )
            wants = open_now or joinable
            if wants and not armed:
                # Once per class, not once per 5s poll all night.
                if cls.id not in self._held:
                    self._held.add(cls.id)
                    self._log.info(
                        "slot.held_until_release",
                        class_id=cls.id,
                        name=cls.name,
                        status=cls.status.value,
                        arms_at=self._arm_at.isoformat(),
                    )
            elif wants:
                await self._try_book(cls)
            elif is_burst:
                self._log.debug(
                    "burst.poll",
                    class_id=cls.id,
                    status=cls.status.value,
                )

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

    def _in_time_window(self, cls: GymClass) -> bool:
        local_time = cls.start.astimezone(self._tz).time()
        return (
            self._settings.class_time_from
            <= local_time
            <= self._settings.class_time_to
        )

    async def _try_book(self, cls: GymClass) -> None:
        is_waitlist = cls.status == ClassStatus.WAITLIST
        verb = "join waitlist for" if is_waitlist else "book"
        when = cls.start.astimezone(self._tz)
        self._log.info(
            "slot.found",
            class_id=cls.id,
            name=cls.name,
            spots=cls.spots_left,
            waitlist=is_waitlist,
        )
        if self._settings.dry_run:
            self._booked.add(cls.id)
            await self._notifier.notify(
                f"🟡 (dry run) Would {verb} {cls.name} at {when:%a %d %b %H:%M %Z}"
            )
            self._log.info("booking.dry_run", class_id=cls.id, waitlist=is_waitlist)
            return
        result = await self._repo.book(cls.id)
        if result.success:
            self._booked.add(cls.id)
            prefix = "📋 Joined waitlist for" if is_waitlist else "✅ Booked"
            await self._notifier.notify(
                f"{prefix} {cls.name} at {when:%a %d %b %H:%M %Z} ({result.message})"
            )
            self._log.info("booking.success", **result.model_dump())
        else:
            await self._notifier.notify(
                f"❌ Failed to {verb} {cls.name}: {result.message}"
            )
            self._log.warning(
                "booking.failed", class_id=cls.id, message=result.message
            )

    def _is_burst_window(self, now: datetime) -> bool:
        local = now.astimezone(self._tz)
        delta = abs((local - self._release_on(local)).total_seconds())
        return delta <= self._settings.burst_window_seconds