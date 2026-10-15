from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from datetime import UTC, datetime

import structlog

logger = structlog.get_logger()

API_HOST = "api.nuffieldhealth.com"
SUBSCRIPTION_KEY_HEADER = "ocp-apim-subscription-key"
LOGIN_HOST = "account.nuffieldhealth.com"
# The B2C submit intermittently no-ops; re-click while still on the form.
SUBMIT_ATTEMPTS = 3
SUBMIT_WAIT_SECONDS = 20
# Refresh a little before the token actually expires.
EXPIRY_SKEW_SECONDS = 60
# Floor between browser logins, so a run of 401s can't hammer the login page.
MIN_LOGIN_INTERVAL_SECONDS = 30


def human_time(ts: float) -> str:
    """Unix seconds as local wall-clock, for logs a human has to read at 07:00."""
    if not ts:
        return "unknown"
    return (
        datetime.fromtimestamp(ts, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    )


def jwt_exp(token: str) -> float:
    """Return a JWT's `exp` claim as a unix timestamp, or 0 if it can't be read."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp", 0))
    except (IndexError, ValueError, binascii.Error):
        return 0.0


async def keep_fresh(provider, min_validity: float, interval: float) -> None:
    """Hold the token well clear of expiry in the background, so the booking
    path never has to stop and log in. A failed login is retried every
    `interval` for the whole `min_validity` lead time, which is what turns a
    single flaky login from a missed class into a non-event."""
    while True:
        try:
            await provider.ensure_valid_for(min_validity)
        except Exception:
            logger.exception("auth.keeper_failed")
        await asyncio.sleep(interval)


def is_fresh(token: str | None, exp: float) -> bool:
    return bool(token) and time.time() < exp - EXPIRY_SKEW_SECONDS


class StaticTokenProvider:
    """Returns a token supplied up front (e.g. from BEARER_TOKEN)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_token(self, force_refresh: bool = False) -> str:
        return self._token

    async def ensure_valid_for(self, seconds: float) -> None:
        """Nothing to do: a pasted token cannot be renewed."""


class SeededTokenProvider:
    """
    Serves a pre-supplied token until its `exp` passes, then defers to another
    provider (normally the browser login) for every token after that.
    """

    def __init__(self, seed: str, fallback) -> None:
        self._seed: str | None = seed
        self._exp = jwt_exp(seed)
        self._fallback = fallback

    @property
    def subscription_key(self) -> str | None:
        return getattr(self._fallback, "subscription_key", None)

    async def ensure_valid_for(self, seconds: float) -> None:
        if self._seed and time.time() < self._exp - seconds:
            return
        await self._fallback.ensure_valid_for(seconds)

    async def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and is_fresh(self._seed, self._exp):
            return self._seed  # type: ignore[return-value]
        if self._seed is not None:
            logger.info("auth.seed_token_exhausted", expired_at=human_time(self._exp))
            self._seed = None
        return await self._fallback.get_token(force_refresh)


class BrowserTokenProvider:
    """
    Logs in through the real IDAAS page with a headless browser and captures the
    Bearer token from the first authenticated API request. Requires the optional
    `playwright` dependency and `playwright install chromium`.
    """

    def __init__(
        self,
        email: str,
        password: str,
        login_url: str,
        email_selector: str,
        password_selector: str,
        submit_selector: str,
        post_login_url: str,
        headless: bool = True,
        timeout_seconds: float = 90.0,
    ) -> None:
        self._post_login_url = post_login_url
        self._email = email
        self._password = password
        self._login_url = login_url
        self._email_selector = email_selector
        self._password_selector = password_selector
        self._submit_selector = submit_selector
        self._headless = headless
        self._timeout = timeout_seconds
        self._cached: str | None = None
        self._exp: float = 0.0
        self._last_login: float = 0.0
        self._lock = asyncio.Lock()
        # Relearned on every login; the repository prefers it over config.
        self.subscription_key: str | None = None

    async def get_token(self, force_refresh: bool = False) -> str:
        # Lock-free fast path: a booking must never queue behind a refresh that
        # is already under way when we are holding a perfectly good token.
        if not force_refresh and is_fresh(self._cached, self._exp):
            return self._cached  # type: ignore[return-value]
        async with self._lock:
            if is_fresh(self._cached, self._exp) and not force_refresh:
                return self._cached  # type: ignore[return-value]
            # During the release burst a 401 could otherwise drive a login per
            # poll; never re-login faster than this.
            since = time.time() - self._last_login
            if self._cached and since < MIN_LOGIN_INTERVAL_SECONDS:
                logger.warning("auth.refresh_throttled", seconds_since=round(since))
                return self._cached
            self._last_login = time.time()
            token = await self._capture()
            self._cached = token
            self._exp = jwt_exp(token)
            logger.info(
                "auth.token_refreshed",
                expires_at=human_time(self._exp),
                valid_for_min=round((self._exp - time.time()) / 60),
            )
            return token

    async def ensure_valid_for(self, seconds: float) -> None:
        """Refresh now if the cached token would not survive the next
        `seconds`. Called from a background keeper so a login never lands in
        the middle of the release burst."""
        if self._cached and time.time() < self._exp - seconds:
            return
        await self.get_token(force_refresh=True)

    async def _capture(self) -> str:
        from playwright.async_api import async_playwright

        loop = asyncio.get_running_loop()
        token_future: asyncio.Future[str] = loop.create_future()

        def on_request(request) -> None:
            if API_HOST not in request.url:
                return
            key = request.headers.get(SUBSCRIPTION_KEY_HEADER)
            if key:
                self.subscription_key = key
            auth = request.headers.get("authorization", "")
            if not token_future.done() and auth.lower().startswith("bearer "):
                token_future.set_result(auth.split(" ", 1)[1])

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self._headless)
            try:
                page = await browser.new_page()
                page.on("request", on_request)
                # Submitting before the B2C page has wired up its JS silently
                # does nothing and leaves us sitting on the form, so wait for
                # the network to settle and the button to exist first.
                await page.goto(self._login_url, wait_until="networkidle")
                await page.wait_for_selector(self._submit_selector, state="visible")
                await page.fill(self._email_selector, self._email)
                await page.fill(self._password_selector, self._password)

                for attempt in range(SUBMIT_ATTEMPTS):
                    await page.click(self._submit_selector)
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(token_future), timeout=SUBMIT_WAIT_SECONDS
                        )
                    except TimeoutError:
                        if not self._on_login_page(page):
                            break
                        logger.warning("auth.submit_retry", attempt=attempt + 1)

                # Logged in but no API call observed: nudge the landing page.
                if self._post_login_url and not self._on_login_page(page):
                    logger.info("auth.forcing_api_call", url=self._post_login_url)
                    await page.goto(self._post_login_url, wait_until="networkidle")
                try:
                    return await asyncio.wait_for(token_future, timeout=self._timeout)
                except TimeoutError:
                    logger.error(
                        "auth.capture_timeout",
                        final_url=page.url,
                        title=await page.title(),
                        hint="login likely failed; check credentials/selectors",
                    )
                    raise
            finally:
                await browser.close()

    @staticmethod
    def _on_login_page(page) -> bool:
        return LOGIN_HOST in page.url
