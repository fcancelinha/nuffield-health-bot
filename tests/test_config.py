from __future__ import annotations

import pytest

from nuffield_bot.composition import _build_token_provider
from nuffield_bot.config import Settings


def test_defaults_target_both_reformer_variants():
    assert Settings().class_name_filters == [
        "Reformer Pilates - Signature",
        "Reformer Pilates - Foundations",
    ]


def test_filters_parse_from_comma_separated_env(monkeypatch):
    monkeypatch.setenv("CLASS_NAME_FILTERS", "Foo Class, Bar Class ,")
    assert Settings().class_name_filters == ["Foo Class", "Bar Class"]


def test_blank_secrets_are_treated_as_unset(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "")
    monkeypatch.setenv("NOTIFY_WHATSAPP_TOKEN", "  ")
    settings = Settings()
    assert settings.bearer_token is None
    assert settings.notify_whatsapp_token is None


def test_auto_login_without_token_uses_browser(monkeypatch):
    monkeypatch.setenv("AUTO_LOGIN", "true")
    monkeypatch.setenv("BEARER_TOKEN", "")
    assert type(_build_token_provider(Settings())).__name__ == "BrowserTokenProvider"


def test_auto_login_with_token_seeds_the_browser_provider(monkeypatch):
    monkeypatch.setenv("AUTO_LOGIN", "true")
    monkeypatch.setenv("BEARER_TOKEN", "h.e30.s")
    assert type(_build_token_provider(Settings())).__name__ == "SeededTokenProvider"


def test_no_auth_configured_fails_loudly(monkeypatch):
    monkeypatch.setenv("AUTO_LOGIN", "false")
    monkeypatch.setenv("BEARER_TOKEN", "")
    with pytest.raises(RuntimeError, match="No auth configured"):
        _build_token_provider(Settings())
