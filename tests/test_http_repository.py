from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from tenacity import stop_after_attempt

from nuffield_bot.adapters.auth import (
    SeededTokenProvider,
    StaticTokenProvider,
    jwt_exp,
)
from nuffield_bot.adapters.http_repository import (
    BOOK_URL,
    HttpClassRepository,
    _map_status,
    _retry_after_seconds,
)
from nuffield_bot.config import Settings
from nuffield_bot.domain.models import ClassStatus


async def _no_sleep(_seconds: float) -> None:
    """Collapse tenacity's backoff so retry tests stay fast."""


@pytest.fixture
def fast_retries(monkeypatch):
    """tenacity binds its sleep at decoration time, so patching asyncio.sleep
    has no effect; patch the retry controller itself. The booking budget is
    wall-clock (`stop_after_delay`), so cap attempts too or the test spins for
    the full 25s."""
    for fn in (HttpClassRepository._book_once, HttpClassRepository.find_upcoming):
        monkeypatch.setattr(fn.retry, "sleep", _no_sleep)
    monkeypatch.setattr(
        HttpClassRepository._book_once.retry, "stop", stop_after_attempt(5)
    )

AVAILABLE_ITEM = {
    "sfid": "a2PNz000009djyQMAQ",
    "title": "Reformer Pilates - Signature",
    "from_date": "2026-10-21T12:00:00+00:00",
    "to_date": "2026-10-21T12:55:00+00:00",
    "status": "Online",
    "is_full": False,
    "attendees": 3,
    "attendance_capacity": 8,
    "has_waitlist": False,
}


def _repo(handler, tokens=None) -> tuple[HttpClassRepository, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    repo = HttpClassRepository(
        client,
        tokens or StaticTokenProvider("tok"),
        subscription_key="sub-key",
        origin="https://origin.example",
    )
    return repo, client


def test_parses_item_into_gym_class():
    cls = HttpClassRepository._to_gym_class(AVAILABLE_ITEM)
    assert cls.id == "a2PNz000009djyQMAQ"
    assert cls.name == "Reformer Pilates - Signature"
    assert cls.duration_minutes == 55
    assert cls.capacity == 8
    assert cls.booked == 3
    assert cls.spots_left == 5
    assert cls.status == ClassStatus.AVAILABLE
    assert cls.start.tzinfo is not None


def test_status_mapping():
    assert _map_status({"status": "Online", "is_full": False}) == ClassStatus.AVAILABLE
    assert _map_status({"status": "Online Full", "is_full": True}) == ClassStatus.FULL
    assert (
        _map_status({"status": "Online Full", "has_waitlist": True})
        == ClassStatus.WAITLIST
    )
    assert _map_status({"status": "Canceled"}) == ClassStatus.UNKNOWN
    # A full class still open for its waitlist is treated as WAITLIST
    assert (
        _map_status({"status": "Online Full", "is_full": False, "has_waitlist": True})
        == ClassStatus.WAITLIST
    )


@pytest.mark.asyncio
async def test_find_upcoming_filters_and_authenticates():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        items = [
            AVAILABLE_ITEM,
            {**AVAILABLE_ITEM, "sfid": "x", "title": "BODYPUMP™"},
        ]
        return httpx.Response(200, json={"items": items})

    repo, client = _repo(handler)
    try:
        classes = await repo.find_upcoming("loc", ["Reformer Pilates"], 8)
    finally:
        await client.aclose()

    assert seen["auth"] == "Bearer tok"
    assert [c.name for c in classes] == ["Reformer Pilates - Signature"]


@pytest.mark.asyncio
async def test_find_upcoming_matches_both_configured_titles():
    titles = [
        "Reformer Pilates - Signature",
        "Reformer Pilates - Foundations",
        "Reformer Pilates - Advanced",
        "Yoga Flow",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        items = [
            {**AVAILABLE_ITEM, "sfid": f"s{i}", "title": t}
            for i, t in enumerate(titles)
        ]
        return httpx.Response(200, json={"items": items})

    repo, client = _repo(handler)
    try:
        classes = await repo.find_upcoming("loc", Settings().class_name_filters, 8)
    finally:
        await client.aclose()

    assert [c.name for c in classes] == [
        "Reformer Pilates - Signature",
        "Reformer Pilates - Foundations",
    ]


@pytest.mark.asyncio
async def test_find_upcoming_rejects_bare_string_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [{**AVAILABLE_ITEM, "title": "BODYPUMP™"}]}
        )

    repo, client = _repo(handler)
    try:
        classes = await repo.find_upcoming("loc", "Reformer Pilates", 8)
    finally:
        await client.aclose()

    assert classes == []


@pytest.mark.asyncio
async def test_book_success_returns_reference():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == BOOK_URL
        assert json.loads(request.content) == {"reservation": "c1"}
        return httpx.Response(
            200, json={"status": "BOOKED", "heroku_id": "ref-123", "reservation": "c1"}
        )

    repo, client = _repo(handler)
    try:
        result = await repo.book("c1")
    finally:
        await client.aclose()

    assert result.success is True
    assert "ref-123" in result.message


@pytest.mark.asyncio
async def test_book_failure_returns_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="already booked")

    repo, client = _repo(handler)
    try:
        result = await repo.book("c1")
    finally:
        await client.aclose()

    assert result.success is False
    assert "400" in result.message


@pytest.mark.asyncio
async def test_book_reports_waitlist_position():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "BOOKED", "heroku_id": "r", "waitlist_position": 3}
        )

    repo, client = _repo(handler)
    try:
        result = await repo.book("c1")
    finally:
        await client.aclose()

    assert result.success is True
    assert "WAITLIST #3" in result.message


@pytest.mark.asyncio
async def test_requests_carry_the_apim_subscription_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"items": []})

    repo, client = _repo(handler)
    try:
        await repo.find_upcoming("loc", ["Reformer"], 8)
    finally:
        await client.aclose()

    # Without this header APIM answers 401 "Access denied due to missing
    # subscription key", regardless of how valid the bearer token is.
    assert seen["ocp-apim-subscription-key"] == "sub-key"
    assert seen["origin"] == "https://origin.example"
    assert seen["x-transaction-id"]


@pytest.mark.asyncio
async def test_key_learned_from_login_overrides_configured_key():
    class ProviderWithKey(StaticTokenProvider):
        subscription_key = "learned-key"

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"items": []})

    repo, client = _repo(handler, tokens=ProviderWithKey("tok"))
    try:
        await repo.find_upcoming("loc", ["Reformer"], 8)
    finally:
        await client.aclose()

    assert seen["ocp-apim-subscription-key"] == "learned-key"


@pytest.mark.asyncio
async def test_401_is_not_retried_into_a_login_storm():
    calls = {"n": 0}

    class CountingProvider:
        def __init__(self) -> None:
            self.logins = 0

        async def get_token(self, force_refresh: bool = False) -> str:
            if force_refresh:
                self.logins += 1
            return "tok"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="Access denied")

    tokens = CountingProvider()
    repo, client = _repo(handler, tokens=tokens)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await repo.find_upcoming("loc", ["Reformer"], 8)
    finally:
        await client.aclose()

    # One attempt plus a single re-auth retry, not three tenacity rounds.
    assert calls["n"] == 2
    assert tokens.logins == 1


RATE_LIMIT_BODY = (
    '{ "statusCode": 429, "message": "Rate limit is exceeded. '
    'Try again in 2 seconds." }'
)


@pytest.mark.asyncio
async def test_throttled_booking_is_retried_until_it_succeeds(fast_retries):
    """2026-10-15: a 429 on the POST was swallowed and reported as a hard
    failure while 7 spots were still free. It must retry instead."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text=RATE_LIMIT_BODY)
        return httpx.Response(200, json={"status": "BOOKED", "heroku_id": "ref-9"})

    repo, client = _repo(handler)
    try:
        result = await repo.book("c1")
    finally:
        await client.aclose()

    assert calls["n"] == 3
    assert result.success is True
    assert "ref-9" in result.message


@pytest.mark.asyncio
async def test_waitlist_response_counts_as_success():
    """The API answers WAITLIST when the class filled before the POST landed.
    That is a successful join, not the '❌ Failed to book' we reported."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "WAITLIST", "heroku_id": "r-1", "waitlist_position": 2},
        )

    repo, client = _repo(handler)
    try:
        result = await repo.book("c1")
    finally:
        await client.aclose()

    assert result.success is True
    assert "WAITLIST #2" in result.message


@pytest.mark.asyncio
async def test_booking_gives_up_gracefully_when_throttling_never_clears(
    fast_retries,
):

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=RATE_LIMIT_BODY)

    repo, client = _repo(handler)
    try:
        result = await repo.book("c1")
    finally:
        await client.aclose()

    # A BookingResult, not an exception escaping into the tick loop.
    assert result.success is False
    assert "429" in result.message


def test_retry_wait_honours_the_api_hint():
    response = httpx.Response(429, text=RATE_LIMIT_BODY)
    assert _retry_after_seconds(response) == 2.0

    header = httpx.Response(429, headers={"retry-after": "3"}, text="nope")
    assert _retry_after_seconds(header) == 3.0

    assert _retry_after_seconds(httpx.Response(429, text="no hint")) is None


@pytest.mark.asyncio
async def test_listing_still_fails_fast_rather_than_blocking_the_loop(fast_retries):
    """The booking POST fights; a listing poll should not hold up the tick."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text=RATE_LIMIT_BODY)

    repo, client = _repo(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await repo.find_upcoming("loc", ["Reformer"], 8)
    finally:
        await client.aclose()

    assert calls["n"] == 3  # bounded by stop_after_attempt(3)


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"h.{payload.rstrip('=')}.s"


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def get_token(self, force_refresh: bool = False) -> str:
        self.calls += 1
        return "from-browser"


@pytest.mark.asyncio
async def test_seeded_provider_serves_valid_seed_without_browser():
    seed = _jwt(time.time() + 3600)
    fallback = _RecordingProvider()
    provider = SeededTokenProvider(seed, fallback)

    assert await provider.get_token() == seed
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_seeded_provider_skips_expired_seed():
    fallback = _RecordingProvider()
    provider = SeededTokenProvider(_jwt(time.time() - 1), fallback)

    assert await provider.get_token() == "from-browser"
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_seeded_provider_discards_seed_after_force_refresh():
    seed = _jwt(time.time() + 3600)
    fallback = _RecordingProvider()
    provider = SeededTokenProvider(seed, fallback)

    assert await provider.get_token() == seed
    assert await provider.get_token(force_refresh=True) == "from-browser"
    # Seed is dropped for good, so later calls keep using the browser.
    assert await provider.get_token() == "from-browser"
    assert fallback.calls == 2


def test_jwt_exp_reads_expiry():
    # {"exp": 1791977935} base64url, unsigned payload is enough for parsing.
    token = "h.eyJleHAiOjE3OTE5Nzc5MzV9.s"
    assert jwt_exp(token) == 1791977935.0
    assert jwt_exp("not-a-jwt") == 0.0


