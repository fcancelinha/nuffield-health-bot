from __future__ import annotations

import pytest

from nuffield_bot.composition import _build_token_provider
from nuffield_bot.config import Settings


def test_filters_parse_from_comma_separated_env(monkeypatch):
    """pydantic-settings JSON-decodes list fields straight from the env, which
    rejected the comma-separated form until the field was marked NoDecode."""
    monkeypatch.setenv("CLASS_NAME_FILTERS", "Foo Class, Bar Class ,")
    assert Settings().class_name_filters == ["Foo Class", "Bar Class"]


def test_blank_secrets_are_treated_as_unset(monkeypatch):
    """A commented-out .env entry leaves "" behind, which must not look like a
    configured token."""
    monkeypatch.setenv("BEARER_TOKEN", "")
    monkeypatch.setenv("NOTIFY_WHATSAPP_TOKEN", "  ")
    settings = Settings()
    assert settings.bearer_token is None
    assert settings.notify_whatsapp_token is None


@pytest.mark.parametrize(
    "auto_login,token,expected",
    [
        ("true", "", "BrowserTokenProvider"),
        ("true", "h.e30.s", "SeededTokenProvider"),
    ],
)
def test_auth_wiring(monkeypatch, auto_login, token, expected):
    monkeypatch.setenv("AUTO_LOGIN", auto_login)
    monkeypatch.setenv("BEARER_TOKEN", token)
    assert type(_build_token_provider(Settings())).__name__ == expected


def test_no_auth_configured_fails_loudly(monkeypatch):
    monkeypatch.setenv("AUTO_LOGIN", "false")
    monkeypatch.setenv("BEARER_TOKEN", "")
    with pytest.raises(RuntimeError, match="No auth configured"):
        _build_token_provider(Settings())
