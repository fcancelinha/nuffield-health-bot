from datetime import time
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    nuffield_email: str
    nuffield_password: SecretStr
    # Short-lived Bearer token for the booking API (scope booking_api_access).
    # Takes priority over auto_login when set.
    bearer_token: SecretStr | None = None

    # Automatically log in with a headless browser to fetch/refresh the token.
    # Requires the optional `playwright` dependency + `playwright install chromium`.
    auto_login: bool = False
    login_url: str = "https://www.nuffieldhealth.com/account/idaaslogin"
    # Verified against the B2C sign-in page (b2c_1a_nuffv2_signinorsignuploa1).
    # Note the email field is type="text", so an input[type='email'] match fails.
    login_email_selector: str = "#email"
    login_password_selector: str = "#password"
    login_submit_selector: str = "#next"
    login_headless: bool = True
    # Where a successful login lands. Re-visited only if the post-submit page
    # made no booking-API call we could lift a token from.
    post_login_url: str = "https://my.nuffieldhealth.com/"

    # A background task keeps the token at least this far from expiry, so a
    # login never happens inside the release burst. Tokens last ~60 min.
    token_min_validity_seconds: float = 900.0
    token_check_interval_seconds: float = 60.0

    # The booking API sits behind Azure APIM and rejects any call without this
    # key. Captured from the booking microsite; BrowserTokenProvider relearns it
    # on each login, so this is only the bootstrap value.
    api_subscription_key: str = "882ee8ab406042dd9da8045dc58874a3"
    api_origin: str = "https://nh-booking-microsite.nuffieldhealth.com"

    # Salesforce facility id for the gym (the `location` query param)
    location_id: str = "a2T4J000001JJgMUAW"
    # Case-insensitive title substrings; a class matches if ANY applies.
    # NoDecode stops pydantic-settings JSON-parsing the env value so the
    # comma-separated form below reaches the validator.
    class_name_filters: Annotated[list[str], NoDecode] = [
        "Reformer Pilates - Signature",
        "Reformer Pilates - Foundations",
    ]
    target_days_ahead: int = 8

    @field_validator("class_name_filters", mode="before")
    @classmethod
    def _split_filters(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator(
        "bearer_token",
        "notify_whatsapp_token",
        "notify_whatsapp_phone_number_id",
        "notify_whatsapp_recipient",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # A commented-out .env entry leaves "" behind; treat it as absent.
        return None if isinstance(value, str) and not value.strip() else value

    # When true, detect and notify but never POST a real booking
    dry_run: bool = True
    # Only act from the release time onwards each day. Before then the bot
    # polls and logs but never books, so it can be started the night before.
    book_only_after_release: bool = True
    # Join the waitlist when a matching class is full
    join_waitlist: bool = True
    # How often to re-read our bookings looking for a waitlist place turning
    # into a real one. Nuffield sends no email for that, and an unnoticed
    # promotion can become a no-show fee. Skipped during the release burst.
    booking_check_interval_seconds: float = 600.0

    # Only book classes starting within this window (local to the configured timezone)
    class_time_from: time = time(11, 0)
    class_time_to: time = time(14, 0)

    # Polling cadence
    poll_interval_seconds: float = 5.0
    # 0.2s here exhausted the APIM rate limit 32s before the 2026-10-15 release
    # and the booking POST itself was refused with 429. Polling fast is
    # worthless if it starves the one request that matters.
    burst_interval_seconds: float = 1.0
    burst_jitter_seconds: float = 0.2
    burst_window_seconds: int = 45
    # How early to start bursting. The target day does not exist in the API
    # until the release, so polling hard beforehand spends rate-limit quota on
    # guaranteed-empty responses: on 2026-10-16 the 429s began 26s *before* the
    # drop. A few seconds of lead covers clock skew, nothing more.
    burst_lead_seconds: float = 5.0

    # When bookings open (local to the configured timezone)
    release_hour: int = 7
    release_minute: int = 0
    timezone: str = "Europe/London"

    # WhatsApp Cloud API (Meta Graph). Leave unset to fall back to log notifications.
    notify_whatsapp_token: SecretStr | None = None
    notify_whatsapp_phone_number_id: str | None = None
    notify_whatsapp_recipient: str | None = None
