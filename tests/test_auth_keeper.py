from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from nuffield_bot.adapters.auth import (
    BrowserTokenProvider,
    SeededTokenProvider,
    keep_fresh,
)


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"h.{payload.rstrip('=')}.s"


def _provider(**kw) -> BrowserTokenProvider:
    return BrowserTokenProvider(
        email="e",
        password="p",
        login_url="u",
        email_selector="#email",
        password_selector="#password",
        submit_selector="#next",
        post_login_url="",
        **kw,
    )


class _StubCapture:
    """Stands in for the Playwright login."""

    def __init__(self, provider: BrowserTokenProvider, ttl: float = 3600) -> None:
        self.calls = 0
        self.fail_times = 0
        self.ttl = ttl
        provider._capture = self._capture  # type: ignore[method-assign]

    async def _capture(self) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("login flaked")
        return _jwt(time.time() + self.ttl)


@pytest.mark.asyncio
async def test_ensure_valid_for_refreshes_before_expiry_not_after():
    p = _provider()
    cap = _StubCapture(p, ttl=1000)

    await p.get_token()
    assert cap.calls == 1

    # Comfortably valid for the next 60s: no login.
    await p.ensure_valid_for(60)
    assert cap.calls == 1

    # Would not survive the next 1200s: refresh now, ahead of the burst.
    # (The keeper's 60s cadence clears the 30s anti-storm throttle; this test
    # calls back-to-back, so step past it explicitly.)
    p._last_login = 0.0
    await p.ensure_valid_for(1200)
    assert cap.calls == 2


@pytest.mark.asyncio
async def test_valid_token_is_served_while_a_refresh_is_in_flight():
    """The booking path must not queue behind a login."""
    p = _provider()
    _StubCapture(p)
    await p.get_token()
    p._last_login = 0.0  # bypass the anti-storm throttle

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_capture() -> str:
        started.set()
        await release.wait()
        return _jwt(time.time() + 3600)

    p._capture = slow_capture  # type: ignore[method-assign]
    refresh = asyncio.create_task(p.get_token(force_refresh=True))
    await started.wait()

    # A login is mid-flight; a normal read still returns immediately.
    token = await asyncio.wait_for(p.get_token(), timeout=0.5)
    assert token

    release.set()
    await refresh


@pytest.mark.asyncio
async def test_keeper_retries_a_flaky_login_well_before_expiry():
    p = _provider()
    cap = _StubCapture(p)
    cap.fail_times = 2  # first two logins fail

    task = asyncio.create_task(keep_fresh(p, min_validity=900, interval=0.01))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if p._cached:
            break
    task.cancel()

    # Recovered without ever exposing the failure to the booking path.
    assert cap.calls == 3
    assert p._cached is not None


@pytest.mark.asyncio
async def test_keeper_survives_persistent_failure():
    p = _provider()
    cap = _StubCapture(p)
    cap.fail_times = 10_000

    task = asyncio.create_task(keep_fresh(p, min_validity=900, interval=0.01))
    await asyncio.sleep(0.1)
    still_running = not task.done()
    task.cancel()

    assert still_running, "keeper must not die on a failing login"
    assert cap.calls > 1


@pytest.mark.asyncio
async def test_seeded_provider_delegates_keep_alive_to_the_browser():
    inner = _provider()
    cap = _StubCapture(inner)
    seeded = SeededTokenProvider(_jwt(time.time() + 120), inner)

    # Seed is valid for 120s but not for the 900s we need: refresh via browser.
    await seeded.ensure_valid_for(900)

    assert cap.calls == 1
