"""A pending human answer is abandonable: the stop ladder reaches a wait that is already in flight.

The claim gate and every HITL prompt wait from inside a watch tick, and ``watch_loop`` consults its
stop channels only *around* ticks — so without a cancellation predicate down here a daemon waiting
for an answer can only be killed. These tests pin the predicate's path (notifier → client → the
poll loop), the outcome it produces (a distinct ``cancelled``, never an approval), and the poll
chunk that decides how fast a stop is noticed.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import ClassVar

import pytest
import telegram

from wastech_orchestrator.notify.telegram import (
    TelegramNotifier,
    _AskCancelled,
    _HttpTelegramClient,
    _Secrets,
)

from .conftest import FakeTelegramClient

# The daemon's soft stop waits 30s before escalating to a tree kill, so the wait must notice a stop
# well inside that. Anything under this ceiling keeps the cooperative rung of the ladder working.
_STOP_PATIENCE_S = 30


def _notifier(client: FakeTelegramClient, *, cancelled: bool) -> TelegramNotifier:
    return TelegramNotifier(
        client=client,
        secrets=_Secrets(bot_token="bot-token-secret-1234", chat_id="123456"),
        ask_timeout_s=28800,  # the shipped-style 8h HITL ceiling: the point is it is not waited out
        monotonic=lambda: 100.0,
        wall_clock=lambda: 1000.0,
        is_cancelled=lambda: cancelled,
    )


def test_a_pending_stop_is_never_read_as_an_approval(fake_client: FakeTelegramClient) -> None:
    result = _notifier(fake_client, cancelled=True).ask_human(
        question="Start this task next?",
        context="Task 002d",
        task_id="002d",
        kind="approval",
        timeout_s=28800,
    )

    assert result.answered is False
    assert result.approved is not True
    assert result.failure == "cancelled"  # distinct from timeout: nobody was asked, nobody was late


def test_no_prompt_is_sent_for_a_question_that_cannot_be_waited_for(
    fake_client: FakeTelegramClient,
) -> None:
    _notifier(fake_client, cancelled=True).ask_human(
        question="Start this task next?",
        context="Task 002d",
        task_id="002d",
        kind="approval",
        timeout_s=28800,
    )

    assert fake_client.prompts == []  # asking would leave the operator a button nothing reads
    assert fake_client.deadlines == []  # and the wait is never entered


def test_an_uncancelled_wait_is_unchanged(fake_client: FakeTelegramClient) -> None:
    result = _notifier(fake_client, cancelled=False).ask_human(
        question="A or B?",
        context="ctx",
        task_id="task-001",
        kind="question",
        timeout_s=5,
    )

    assert fake_client.prompts and fake_client.deadlines  # asked, and waited
    assert result.failure == "timeout"  # the fake has no reply queued


class _SilentBot:
    """A ``telegram.Bot`` that never has an update to hand back, recording every poll."""

    polls: ClassVar[list[int]] = []

    def __init__(self, token: str) -> None:
        self._token = token

    async def __aenter__(self) -> _SilentBot:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def get_updates(
        self,
        *,
        timeout: int = 0,  # noqa: ASYNC109 - mirrors getUpdates' own long-poll parameter
        **_: object,
    ) -> tuple[object, ...]:
        type(self).polls.append(timeout)
        return ()


def test_the_poll_loop_gives_up_between_requests_when_a_stop_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram, "Bot", _SilentBot)
    _SilentBot.polls = []
    client = _HttpTelegramClient(bot_token="token")
    # Stop asked for after the first request comes back empty — the shape of a real stop arriving
    # mid-wait, rather than one that was already pending when the wait started.
    calls = {"n": 0}

    def cancelled_after_first_poll() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    started = time.monotonic()
    with pytest.raises(_AskCancelled):
        client.poll_reply(
            chat_id="42",
            prompt_message_id=10,
            update_offset=1,
            interaction_id="hitl-1",
            kind="approval",
            deadline_monotonic=time.monotonic() + 28800,  # eight hours away
            is_cancelled=cancelled_after_first_poll,
        )

    assert time.monotonic() - started < _STOP_PATIENCE_S


def test_one_long_poll_stays_inside_the_stop_ladders_patience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Telegram allows a 50s long poll and a longer poll is cheaper, but the predicate is only read
    # between requests — so the chunk is what bounds how long `stop` waits for the gate to notice.
    monkeypatch.setattr(telegram, "Bot", _SilentBot)
    _SilentBot.polls = []
    client = _HttpTelegramClient(bot_token="token")
    calls = {"n": 0}

    def cancelled_after_first_poll() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(_AskCancelled):
        client.poll_reply(
            chat_id="42",
            prompt_message_id=10,
            update_offset=1,
            interaction_id="hitl-1",
            kind="approval",
            deadline_monotonic=time.monotonic() + 28800,
            is_cancelled=cancelled_after_first_poll,
        )

    assert _SilentBot.polls, "no request was made"
    assert max(_SilentBot.polls) < _STOP_PATIENCE_S
