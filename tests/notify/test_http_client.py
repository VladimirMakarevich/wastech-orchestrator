"""The real adapter lifecycle is exercised with a fake ``telegram.Bot`` and no network."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import TracebackType

import pytest
import telegram

from wastech_orchestrator.notify.telegram import _HttpTelegramClient


@dataclass
class _Chat:
    id: int


@dataclass
class _Message:
    chat: _Chat
    text: str
    message_id: int = 10
    reply_to_message: _Message | None = None


@dataclass
class _Update:
    update_id: int
    message: _Message | None = None
    callback_query: _Callback | None = None


@dataclass
class _Callback:
    id: str
    message: _Message
    data: str


class _FakeBot:
    """Async context-manager fake matching the small ``telegram.Bot`` surface we use."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def __aenter__(self) -> _FakeBot:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def send_message(self, *, chat_id: str | int, text: str, **_: object) -> _Message:
        logging.getLogger("httpx").critical(
            "HTTP Request: https://api.telegram.org/bot%s/sendMessage", self._token
        )
        return _Message(_Chat(int(chat_id)), text)

    async def get_updates(self, **_: object) -> tuple[_Update, ...]:
        prompt = _Message(_Chat(42), "prompt", message_id=10)
        return (_Update(1, _Message(_Chat(42), "yes", message_id=11, reply_to_message=prompt)),)

    async def answer_callback_query(self, callback_query_id: str) -> bool:
        return bool(callback_query_id)


def test_http_client_uses_async_lifecycle_without_leaking_request_url(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    token = "bot-token-secret-1234"
    monkeypatch.setattr(telegram, "Bot", _FakeBot)
    client = _HttpTelegramClient(bot_token=token)

    with caplog.at_level(logging.CRITICAL):
        client.send_message(chat_id="42", text="hello")
        reply = client.poll_reply(
            chat_id="42",
            prompt_message_id=10,
            update_offset=1,
            interaction_id="hitl-1",
            kind="question",
            deadline_monotonic=time.monotonic() + 1,
        )

    assert reply is not None and reply.text == "yes"
    assert token not in caplog.text


def test_http_client_ignores_foreign_and_unrelated_question_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CorrelatedBot(_FakeBot):
        async def get_updates(self, **_: object) -> tuple[_Update, ...]:
            wrong_prompt = _Message(_Chat(42), "old", message_id=9)
            right_prompt = _Message(_Chat(42), "current", message_id=10)
            return (
                _Update(
                    1,
                    _Message(
                        _Chat(99),
                        "foreign",
                        message_id=11,
                        reply_to_message=right_prompt,
                    ),
                ),
                _Update(
                    2,
                    _Message(
                        _Chat(42),
                        "stale",
                        message_id=12,
                        reply_to_message=wrong_prompt,
                    ),
                ),
                _Update(
                    3,
                    _Message(
                        _Chat(42),
                        "matched",
                        message_id=13,
                        reply_to_message=right_prompt,
                    ),
                ),
            )

    monkeypatch.setattr(telegram, "Bot", _CorrelatedBot)
    client = _HttpTelegramClient(bot_token="token")

    reply = client.poll_reply(
        chat_id="42",
        prompt_message_id=10,
        update_offset=1,
        interaction_id="hitl-1",
        kind="question",
        deadline_monotonic=time.monotonic() + 1,
    )

    assert reply is not None and reply.text == "matched"


def test_http_client_accepts_only_matching_approval_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CallbackBot(_FakeBot):
        answered: list[str] = []

        async def get_updates(self, **_: object) -> tuple[_Update, ...]:
            prompt = _Message(_Chat(42), "approve", message_id=10)
            return (
                _Update(
                    1,
                    callback_query=_Callback(
                        id="wrong",
                        message=prompt,
                        data="hitl:other:yes",
                    ),
                ),
                _Update(
                    2,
                    callback_query=_Callback(
                        id="right",
                        message=prompt,
                        data="hitl:hitl-1:no",
                    ),
                ),
            )

        async def answer_callback_query(self, callback_query_id: str) -> bool:
            self.answered.append(callback_query_id)
            return True

    monkeypatch.setattr(telegram, "Bot", _CallbackBot)
    client = _HttpTelegramClient(bot_token="token")

    reply = client.poll_reply(
        chat_id="42",
        prompt_message_id=10,
        update_offset=1,
        interaction_id="hitl-1",
        kind="approval",
        deadline_monotonic=time.monotonic() + 1,
    )

    assert reply is not None and reply.approved is False
    assert _CallbackBot.answered == ["right"]
