from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
)

from nuffield_bot.domain.models import (
    BookingResult,
    ClassStatus,
    GymClass,
    HeldBooking,
)
from nuffield_bot.domain.ports import TokenProvider

logger = structlog.get_logger()

API_BASE = "https://api.nuffieldhealth.com/booking"
BOOKABLE_ITEMS_URL = f"{API_BASE}/member/1.0/bookable_items/gym/"
BOOK_URL = f"{API_BASE}/member/1.0/bookings/gym/"
MY_BOOKINGS_URL = f"{API_BASE}/member/1.0/bookings/"
# my_booking.status values that mean we still hold a place. Cancelled bookings
# stay in the upcoming list, and must not block re-booking the same class.
HELD_STATUSES = frozenset({"booked", "waitlist"})
# An expired token comes back as 403 "Invalid Auth Token or Missing Claim(s)",
# never 401, so re-authenticating only on 401 would never fire. Verified
# against the live API with expired, truncated and garbage tokens.
AUTH_REJECTED_STATUSES = frozenset({401, 403})
MAX_BOOKING_PAGES = 10
# Keep retrying a throttled booking for this long. Spots went from 7 free to
# full in ~30s at the last release, so a slow win still beats giving up.
BOOK_RETRY_BUDGET_SECONDS = 25.0
MAX_RETRY_WAIT_SECONDS = 5.0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """APIM signals the cool-off in a header, or failing that in the body
    ("Rate limit is exceeded. Try again in 2 seconds.")."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = re.search(r"try again in (\d+)\s*second", response.text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _wait_for_retry_after(retry_state) -> float:
    exc = retry_state.outcome.exception()
    if isinstance(exc, httpx.HTTPStatusError):
        hinted = _retry_after_seconds(exc.response)
        if hinted is not None:
            # Small cushion so we land just after the window reopens.
            return min(hinted + 0.25, MAX_RETRY_WAIT_SECONDS)
    return min(0.5 * 2**retry_state.attempt_number, MAX_RETRY_WAIT_SECONDS)


def _is_transient(exc: BaseException) -> bool:
    """Retry network blips and 5xx/429, but not a deterministic 4xx. Retrying a
    401 would re-trigger a browser login on every attempt."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status == 429
    return isinstance(exc, httpx.TransportError)


class HttpClassRepository:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token_provider: TokenProvider,
        subscription_key: str,
        origin: str,
        timezone: str = "Europe/London",
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._subscription_key = subscription_key
        self._origin = origin
        self._tz = ZoneInfo(timezone)

    def _day_bounds(self, days_ahead: int) -> tuple[datetime, datetime]:
        """Midnight-to-midnight for the target day **in the gym's timezone**,
        expressed in UTC for the wire. Deriving the day in UTC would shift the
        window by an hour during BST and drop classes either side of midnight.
        Dates are stepped by calendar day, not 24h, so DST changeovers are safe.
        """
        target = (datetime.now(self._tz) + timedelta(days=days_ahead)).date()
        start = datetime.combine(target, dtime.min, tzinfo=self._tz)
        end = datetime.combine(target + timedelta(days=1), dtime.min, tzinfo=self._tz)
        return start.astimezone(UTC), (end - timedelta(milliseconds=1)).astimezone(UTC)

    def _base_headers(self) -> dict[str, str]:
        # Prefer a key the provider observed during a real login over the
        # configured bootstrap value, so a rotation heals itself.
        key = getattr(self._tokens, "subscription_key", None) or self._subscription_key
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "ocp-apim-subscription-key": key,
            "origin": self._origin,
            "x-transaction-id": str(uuid.uuid4()),
            "device-time": str(int(time.time() * 1000)),
        }

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send an authenticated request, re-authenticating once if the token
        is rejected."""
        headers = {**self._base_headers(), **kwargs.pop("headers", {})}
        headers["Authorization"] = f"Bearer {await self._tokens.get_token()}"
        response = await self._client.request(method, url, headers=headers, **kwargs)
        if response.status_code in AUTH_REJECTED_STATUSES:
            logger.warning(
                "auth.rejected_reauthenticating",
                status=response.status_code,
                detail=response.text[:120],
            )
            token = await self._tokens.get_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            response = await self._client.request(
                method, url, headers=headers, **kwargs
            )
        response.raise_for_status()
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=_wait_for_retry_after,
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )
    async def find_upcoming(
        self,
        location_id: str,
        name_filters: list[str],
        days_ahead: int,
    ) -> list[GymClass]:
        from_date, to_date = self._day_bounds(days_ahead)
        params = {
            "location": location_id,
            "from_date": from_date.isoformat(timespec="milliseconds"),
            "to_date": to_date.isoformat(timespec="milliseconds"),
        }
        response = await self._request("GET", BOOKABLE_ITEMS_URL, params=params)
        items = response.json().get("items", [])

        # A bare str would iterate per-character and match almost anything.
        if isinstance(name_filters, str):
            name_filters = [name_filters]
        needles = [f.casefold() for f in name_filters]
        return [
            self._to_gym_class(item)
            for item in items
            if any(n in item.get("title", "").casefold() for n in needles)
        ]

    @retry(
        stop=stop_after_attempt(3),
        wait=_wait_for_retry_after,
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )
    async def find_my_bookings(self) -> list[HeldBooking]:
        """Every upcoming place on the account. Seeds the dedup set across
        restarts and lets the service spot a waitlist turning into a booking."""
        bookings: list[HeldBooking] = []
        for page in range(1, MAX_BOOKING_PAGES + 1):
            response = await self._request(
                "GET", MY_BOOKINGS_URL, params={"past": "false", "page": page}
            )
            payload = response.json()
            for item in payload.get("items", []):
                # One malformed record must not break dedup seeding at startup.
                if not item.get("sfid") or not item.get("from_date"):
                    continue
                mine = item.get("my_booking") or {}
                status = mine.get("status") or ""
                bookings.append(
                    HeldBooking(
                        class_id=item["sfid"],
                        name=item.get("title", ""),
                        start=datetime.fromisoformat(item["from_date"]),
                        status=status,
                        held=status.casefold() in HELD_STATUSES,
                        waitlist_position=int(mine.get("waitlist_position") or 0),
                    )
                )
            if not payload.get("next"):
                break
        return bookings

    async def book(self, class_id: str) -> BookingResult:
        """Booking is the one request worth fighting for, so a throttled POST is
        retried harder than a listing poll."""
        try:
            return await self._book_once(class_id)
        except httpx.HTTPStatusError as exc:
            return BookingResult(
                success=False,
                class_id=class_id,
                message=f"HTTP {exc.response.status_code}: {exc.response.text[:160]}",
            )
        except httpx.TransportError as exc:
            return BookingResult(
                success=False,
                class_id=class_id,
                message=f"{type(exc).__name__} after retries",
            )

    @retry(
        stop=stop_after_delay(BOOK_RETRY_BUDGET_SECONDS),
        wait=_wait_for_retry_after,
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )
    async def _book_once(self, class_id: str) -> BookingResult:
        # HTTP errors propagate: tenacity retries the transient ones and
        # reraises the rest for book() to turn into a result.
        response = await self._request("POST", BOOK_URL, json={"reservation": class_id})
        data = response.json()
        status = data.get("status")
        # heroku_id is the booking reference needed to cancel later.
        reference = data.get("heroku_id")
        position = int(data.get("waitlist_position") or 0)
        if status not in ("BOOKED", "WAITLIST"):
            return BookingResult(
                success=False, class_id=class_id, message=f"Unexpected status: {status}"
            )
        # WAITLIST means the class filled between the listing and our POST. It
        # is a successful waitlist join, not a failure. Only `status` is
        # authoritative: the API reports waitlist_position=1 even on confirmed
        # and cancelled bookings, so it says nothing about which we got.
        waitlisted = status == "WAITLIST"
        label = "WAITLIST" if waitlisted else "BOOKED"
        suffix = f" #{position}" if waitlisted and position else ""
        return BookingResult(
            success=True,
            class_id=class_id,
            waitlisted=waitlisted,
            message=f"{label}{suffix} (ref {reference})",
        )

    @staticmethod
    def _to_gym_class(item: dict) -> GymClass:
        start = datetime.fromisoformat(item["from_date"])
        end = datetime.fromisoformat(item["to_date"])
        # The API returns these as floats (8.0).
        capacity = int(item.get("attendance_capacity") or 0)
        booked = int(item.get("attendees") or 0)
        return GymClass(
            id=item["sfid"],
            name=item.get("title", ""),
            start=start,
            duration_minutes=int((end - start).total_seconds() // 60),
            capacity=capacity,
            booked=booked,
            status=_map_status(item),
        )


def _map_status(item: dict) -> ClassStatus:
    raw = item.get("status", "")
    if raw == "Online" and not item.get("is_full"):
        return ClassStatus.AVAILABLE
    if item.get("has_waitlist"):
        return ClassStatus.WAITLIST
    if item.get("is_full") or raw == "Online Full":
        return ClassStatus.FULL
    return ClassStatus.UNKNOWN