"""``build_notifier`` resolves the transport only from env-named secrets (spec §4.7)."""

from __future__ import annotations

from wastech_orchestrator.config.schema import TelegramConfig
from wastech_orchestrator.notify import NullNotifier, TelegramNotifier, build_notifier


def _cfg(enabled: bool = True) -> TelegramConfig:
    return TelegramConfig(
        enabled=enabled,
        bot_token_env="TG_BOT_TOKEN_FOR_TEST",
        chat_id_env="TG_CHAT_ID_FOR_TEST",
        ask_timeout_s=5,
    )


def test_disabled_returns_null_notifier() -> None:
    notifier = build_notifier(
        _cfg(enabled=False),
        env={"TG_BOT_TOKEN_FOR_TEST": "t", "TG_CHAT_ID_FOR_TEST": "c"},
    )
    assert isinstance(notifier, NullNotifier)


def test_missing_token_env_returns_null_notifier() -> None:
    notifier = build_notifier(_cfg(), env={"TG_CHAT_ID_FOR_TEST": "c"})
    assert isinstance(notifier, NullNotifier)


def test_blank_token_env_returns_null_notifier() -> None:
    notifier = build_notifier(
        _cfg(),
        env={"TG_BOT_TOKEN_FOR_TEST": "   ", "TG_CHAT_ID_FOR_TEST": "c"},
    )
    assert isinstance(notifier, NullNotifier)


def test_missing_chat_id_env_returns_null_notifier() -> None:
    notifier = build_notifier(_cfg(), env={"TG_BOT_TOKEN_FOR_TEST": "t"})
    assert isinstance(notifier, NullNotifier)


def test_all_present_returns_telegram_notifier() -> None:
    # Inject a fake client factory so the real HTTP client is never constructed.
    sentinel: list[tuple[str, str]] = []

    class _Stub:
        def send_message(self, *, chat_id: str, text: str) -> None:
            sentinel.append((chat_id, text))

        def poll_reply(self, *, chat_id: str, deadline_monotonic: float) -> str | None:
            return None

    notifier = build_notifier(
        _cfg(),
        env={"TG_BOT_TOKEN_FOR_TEST": "real-bot-token", "TG_CHAT_ID_FOR_TEST": "42"},
        client_factory=lambda secrets, cfg: _Stub(),
    )
    assert isinstance(notifier, TelegramNotifier)


def test_null_notifier_ask_human_is_deterministic_timeout() -> None:
    notifier = build_notifier(_cfg(enabled=False))
    result = notifier.ask_human(
        question="q", context="", task_id="task-001", kind="question", timeout_s=1
    )
    assert result.failure == "transport_error"
    assert result.answered is False
    assert result.text is None
