from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
import structlog

from nuffield_bot.adapters.auth import (
    BrowserTokenProvider,
    SeededTokenProvider,
    StaticTokenProvider,
    keep_fresh,
)
from nuffield_bot.adapters.clock import SystemClock
from nuffield_bot.adapters.http_repository import HttpClassRepository
from nuffield_bot.adapters.notifiers import LogNotifier, WhatsAppNotifier
from nuffield_bot.config import Settings
from nuffield_bot.domain.ports import Notifier, TokenProvider
from nuffield_bot.services.booking_service import BookingService

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def _build_token_provider(settings: Settings) -> TokenProvider:
    seed = settings.bearer_token.get_secret_value() if settings.bearer_token else None
    if not settings.auto_login:
        if seed:
            return StaticTokenProvider(seed)
        raise RuntimeError(
            "No auth configured. Set BEARER_TOKEN or enable AUTO_LOGIN "
            "with credentials."
        )
    browser = BrowserTokenProvider(
        email=settings.nuffield_email,
        password=settings.nuffield_password.get_secret_value(),
        login_url=settings.login_url,
        email_selector=settings.login_email_selector,
        password_selector=settings.login_password_selector,
        submit_selector=settings.login_submit_selector,
        post_login_url=settings.post_login_url,
        headless=settings.login_headless,
    )
    return SeededTokenProvider(seed, browser) if seed else browser


@asynccontextmanager
async def build_booking_service() -> AsyncIterator[BookingService]:
    settings = Settings()
    logger = structlog.get_logger()

    async with httpx.AsyncClient(
        timeout=10.0,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        http2=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        tokens = _build_token_provider(settings)
        repo = HttpClassRepository(
            client=client,
            token_provider=tokens,
            subscription_key=settings.api_subscription_key,
            origin=settings.api_origin,
        )
        keeper = asyncio.create_task(
            keep_fresh(
                tokens,
                settings.token_min_validity_seconds,
                settings.token_check_interval_seconds,
            )
        )

        notifier: Notifier
        if (
            settings.notify_whatsapp_token
            and settings.notify_whatsapp_phone_number_id
            and settings.notify_whatsapp_recipient
        ):
            notifier = WhatsAppNotifier(
                token=settings.notify_whatsapp_token.get_secret_value(),
                phone_number_id=settings.notify_whatsapp_phone_number_id,
                recipient=settings.notify_whatsapp_recipient,
                client=client,
            )
        else:
            notifier = LogNotifier()

        try:
            yield BookingService(
                repo=repo,
                notifier=notifier,
                clock=SystemClock(),
                settings=settings,
                logger=logger,
            )
        finally:
            # Cancelling mid-login can block on Playwright teardown; don't let
            # shutdown hang on it.
            keeper.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(keeper, timeout=10)
