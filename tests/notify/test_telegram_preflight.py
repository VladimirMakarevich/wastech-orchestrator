"""Unit tests for :func:`check_telegram_preflight` (preflight Telegram health check)."""

from __future__ import annotations

from wastech_orchestrator.config.schema import TelegramConfig
from wastech_orchestrator.notify.telegram import check_telegram_preflight

_CFG_DISABLED = TelegramConfig(
    enabled=False,
    bot_token_env="TG_TOKEN",
    chat_id_env="TG_CHAT",
    ask_timeout_s=60,
)

_CFG_ENABLED = TelegramConfig(
    enabled=True,
    bot_token_env="TG_TOKEN",
    chat_id_env="TG_CHAT",
    ask_timeout_s=60,
)

_ENV = {"TG_TOKEN": "123:AAFakeToken", "TG_CHAT": "987654321"}


class _FakeClient:
    def __init__(self, *, username: str = "mybot", raise_exc: Exception | None = None) -> None:
        self._username = username
        self._raise = raise_exc

    def send_message(self, **_: object) -> None: ...  # pragma: no cover

    def poll_reply(self, **_: object) -> str | None:  # pragma: no cover
        return None

    def get_me(self) -> str:
        if self._raise is not None:
            raise self._raise
        return self._username

    def get_chat(self, **_: object) -> str:
        return "project-chat"

    def get_webhook_url(self) -> str:
        return ""

    def check_polling(self) -> None:
        return None


def _factory(username: str = "mybot", *, raise_exc: Exception | None = None):
    def make(_secrets: object, _cfg: object) -> _FakeClient:
        return _FakeClient(username=username, raise_exc=raise_exc)

    return make


def test_disabled_returns_skip() -> None:
    ok, line = check_telegram_preflight(_CFG_DISABLED)
    assert ok is True
    assert "SKIP" in line
    assert "disabled" in line


def test_enabled_missing_both_env_vars() -> None:
    ok, line = check_telegram_preflight(_CFG_ENABLED, {})
    assert ok is False
    assert "FAIL" in line
    assert "TG_TOKEN" in line
    assert "TG_CHAT" in line


def test_enabled_missing_one_env_var() -> None:
    ok, line = check_telegram_preflight(_CFG_ENABLED, {"TG_TOKEN": "tok"})
    assert ok is False
    assert "FAIL" in line
    assert "TG_CHAT" in line
    assert "TG_TOKEN" not in line


def test_enabled_get_me_succeeds() -> None:
    ok, line = check_telegram_preflight(_CFG_ENABLED, _ENV, client_factory=_factory("coolbot"))
    assert ok is True
    assert "OK" in line
    assert "@coolbot" in line
    assert "polling ready" in line


def test_enabled_get_me_fails_token_redacted() -> None:
    exc = RuntimeError("Unauthorized: 123:AAFakeToken is invalid")
    ok, line = check_telegram_preflight(_CFG_ENABLED, _ENV, client_factory=_factory(raise_exc=exc))
    assert ok is False
    assert "FAIL" in line
    assert "API error" in line
    # The bot token must not appear verbatim in the report line
    assert "123:AAFakeToken" not in line


def test_enabled_non_numeric_chat_id_fails() -> None:
    ok, line = check_telegram_preflight(
        _CFG_ENABLED,
        {"TG_TOKEN": "123:AAFakeToken", "TG_CHAT": "@channel"},
        client_factory=_factory(),
    )
    assert ok is False
    assert "numeric" in line


def test_enabled_zero_chat_id_fails() -> None:
    ok, line = check_telegram_preflight(
        _CFG_ENABLED,
        {"TG_TOKEN": "123:AAFakeToken", "TG_CHAT": "0"},
        client_factory=_factory(),
    )
    assert ok is False
    assert "numeric" in line


def test_enabled_webhook_conflict_fails() -> None:
    class _WebhookClient(_FakeClient):
        def get_webhook_url(self) -> str:
            return "https://example.test/hook"

    ok, line = check_telegram_preflight(
        _CFG_ENABLED,
        _ENV,
        client_factory=lambda _secrets, _cfg: _WebhookClient(),
    )
    assert ok is False
    assert "webhook" in line
