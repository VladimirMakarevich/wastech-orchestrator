"""Every Telegram call made from inside a watch tick is bounded; ``poll_reply`` is not clipped.

The notifier runs on the daemon's own thread, and the stop ladder checks its channels only *around*
ticks — so a call that never returns takes the daemon's answer to ``stop`` with it. These tests pin
the bound at the layer that owns the synchronous contract (:func:`_run_sync`), the per-method policy
that decides which calls carry it, and the notifier's fail-soft handling of a call that gives up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any

import pytest
import telegram

from wastech_orchestrator.notify import telegram as telegram_module
from wastech_orchestrator.notify.telegram import (
    TelegramNotifier,
    _HttpTelegramClient,
    _run_sync,
    _Secrets,
)

# Generous ceiling for "returned promptly": the suite runs under xdist, so a tight bound would be
# flaky. Every test below races it against a coroutine that would otherwise take 20-30s.
_PROMPT_S = 10.0


def _notifier(client: object) -> TelegramNotifier:
    return TelegramNotifier(
        client=client,  # type: ignore[arg-type]
        secrets=_Secrets(bot_token="bot-token-secret-1234", chat_id="123456"),
        ask_timeout_s=5,
    )


def test_run_sync_abandons_a_coroutine_that_overruns_its_deadline() -> None:
    async def never() -> str:
        await asyncio.sleep(30)
        return "unreachable"

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _run_sync(never, timeout=0.1)
    assert time.monotonic() - started < _PROMPT_S


def test_run_sync_bounds_a_call_that_swallows_cancellation() -> None:
    # The hard half of the bound. ``asyncio.wait_for`` cancels the *await*, but a blocking call
    # already handed to the default executor keeps running, and ``asyncio.run``'s own close then
    # joins that executor for up to ``asyncio.constants.THREAD_JOIN_TIMEOUT`` (300s) — a second
    # unbounded stretch on the same call. Only abandoning the thread bounds the caller.
    async def uncancellable() -> str:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, time.sleep, 20)
        return "unreachable"

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _run_sync(uncancellable, timeout=0.1)
    assert time.monotonic() - started < _PROMPT_S


def test_run_sync_without_a_deadline_returns_the_value_from_inside_a_running_loop() -> None:
    # Regression pin for the reason the helper exists at all: a caller that already owns a loop
    # cannot run a coroutine on its own thread.
    async def answer() -> str:
        return "ok"

    async def from_loop() -> str:
        return _run_sync(answer)

    assert asyncio.run(from_loop()) == "ok"
    assert _run_sync(answer, timeout=5) == "ok"  # a deadline that is not hit is invisible


def test_only_poll_reply_runs_without_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-method policy: every call is bounded except the one carrying its own deadline."""
    recorded: list[tuple[str, float | None]] = []

    def spy[T](factory: Callable[[], Coroutine[Any, Any, T]], *, timeout: float | None = None) -> T:
        recorded.append((factory.__name__, timeout))
        return None  # type: ignore[return-value]

    monkeypatch.setattr(telegram_module, "_run_sync", spy)
    client = _HttpTelegramClient(bot_token="token")

    client.send_message(chat_id="42", text="hi")
    client.get_me()
    client.get_chat(chat_id="42")
    client.get_webhook_url()
    client.check_polling()
    client.send_prompt(chat_id="42", text="hi", kind="approval", interaction_id="i-1")
    client.poll_reply(
        chat_id="42",
        prompt_message_id=10,
        update_offset=1,
        interaction_id="i-1",
        kind="approval",
        deadline_monotonic=time.monotonic() + 28800,
    )

    bounded = [t for _name, t in recorded[:-1]]
    assert bounded and all(t is not None for t in bounded), recorded
    assert recorded[-1][1] is None, "poll_reply carries its own deadline and must not be clipped"


class _StalledBot:
    """A ``telegram.Bot`` whose every request never completes (a stalled network)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def __aenter__(self) -> _StalledBot:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def send_message(self, **_: object) -> None:
        await asyncio.sleep(30)


def test_a_stalled_send_gives_up_and_the_notifier_logs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(telegram, "Bot", _StalledBot)
    monkeypatch.setattr(telegram_module, "_CALL_TIMEOUT_S", 0.2)
    notifier = _notifier(_HttpTelegramClient(bot_token="bot-token-secret-1234"))

    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        notifier.send_notification(
            task_id="task-001", final_status="done", pr_url=None, reason=None
        )
    elapsed = time.monotonic() - started

    assert elapsed < _PROMPT_S, "a stalled send must not hold the tick"
    assert "send_notification send failed" in caplog.text
    assert "bot-token-secret-1234" not in caplog.text


def test_a_stalled_prompt_fails_closed_as_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram, "Bot", _StalledBot)
    monkeypatch.setattr(telegram_module, "_CALL_TIMEOUT_S", 0.2)
    notifier = _notifier(_HttpTelegramClient(bot_token="token"))

    result = notifier.ask_human(
        question="Start this task next?",
        context="Task 002d",
        task_id="002d",
        kind="approval",
        timeout_s=5,
    )

    assert result.answered is False
    assert result.failure == "transport_error"  # fail-closed: an undelivered prompt is never a yes
