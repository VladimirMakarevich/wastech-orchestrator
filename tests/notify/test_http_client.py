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
    message: _Message | None
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

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> bool:
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


@dataclass
class _Ack:
    id: str
    text: str | None
    show_alert: bool


def _ack_recording_bot() -> type[_FakeBot]:
    """A fake bot base that records every ``answer_callback_query`` call (id, text, alert)."""

    class _AckBot(_FakeBot):
        acks: list[_Ack]

        async def answer_callback_query(
            self, callback_query_id: str, text: str | None = None, show_alert: bool = False
        ) -> bool:
            type(self).acks.append(_Ack(callback_query_id, text, show_alert))
            return True

    return _AckBot


def test_http_client_acknowledges_matching_and_near_miss_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every press in our chat is acknowledged (§4.1): the near-miss "wrong" (right chat + message,
    # but stale data) gets a stale alert; the matching "right" returns the answer.
    base = _ack_recording_bot()
    base.acks = []

    class _CallbackBot(base):  # type: ignore[misc, valid-type]
        async def get_updates(self, **_: object) -> tuple[_Update, ...]:
            prompt = _Message(_Chat(42), "approve", message_id=10)
            return (
                _Update(1, callback_query=_Callback(id="wrong", message=prompt, data="hitl:x:yes")),
                _Update(
                    2, callback_query=_Callback(id="right", message=prompt, data="hitl:hitl-1:no")
                ),
            )

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
    assert [a.id for a in base.acks] == ["wrong", "right"]  # both acknowledged, not just the match
    wrong, right = base.acks
    assert wrong.show_alert is True and "no longer active" in (wrong.text or "")  # stale near-miss
    assert right.show_alert is False and "Denied" in (right.text or "")


def test_http_client_acknowledges_callback_with_no_message(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    base = _ack_recording_bot()
    base.acks = []

    class _NoMsgBot(base):  # type: ignore[misc, valid-type]
        async def get_updates(
            self, *, offset: int | None = None, **_: object
        ) -> tuple[_Update, ...]:
            if offset is not None and offset > 1:  # offset-aware like the real API: deliver once
                return ()
            return (
                _Update(
                    1, callback_query=_Callback(id="ghost", message=None, data="hitl:hitl-1:yes")
                ),
            )

    monkeypatch.setattr(telegram, "Bot", _NoMsgBot)
    client = _HttpTelegramClient(bot_token="token")

    with caplog.at_level(logging.WARNING):
        reply = client.poll_reply(
            chat_id="42",
            prompt_message_id=10,
            update_offset=1,
            interaction_id="hitl-1",
            kind="approval",
            deadline_monotonic=time.monotonic() + 0.2,
        )

    assert reply is None  # inaccessible message → not actionable, polls to the deadline
    assert [a.id for a in base.acks] == ["ghost"]  # still acknowledged (spinner cleared)
    assert "near-miss" in caplog.text and "message_none" in caplog.text


def test_http_client_does_not_acknowledge_foreign_chat_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _ack_recording_bot()
    base.acks = []

    class _ForeignBot(base):  # type: ignore[misc, valid-type]
        async def get_updates(
            self, *, offset: int | None = None, **_: object
        ) -> tuple[_Update, ...]:
            if offset is not None and offset > 1:
                return ()
            foreign = _Message(_Chat(99), "x", message_id=10)
            return (
                _Update(
                    1,
                    callback_query=_Callback(id="foreign", message=foreign, data="hitl:hitl-1:yes"),
                ),
            )

    monkeypatch.setattr(telegram, "Bot", _ForeignBot)
    client = _HttpTelegramClient(bot_token="token")

    reply = client.poll_reply(
        chat_id="42",
        prompt_message_id=10,
        update_offset=1,
        interaction_id="hitl-1",
        kind="approval",
        deadline_monotonic=time.monotonic() + 0.2,
    )

    assert reply is None
    assert base.acks == []  # a foreign chat's callback is never acknowledged


def test_http_client_near_miss_log_carries_no_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    token = "bot-token-secret-1234"
    base = _ack_recording_bot()
    base.acks = []

    class _Bot(base):  # type: ignore[misc, valid-type]
        async def get_updates(
            self, *, offset: int | None = None, **_: object
        ) -> tuple[_Update, ...]:
            if offset is not None and offset > 1:
                return ()
            prompt = _Message(_Chat(424242), "x", message_id=99)  # chat id is a secret (§12.15)
            return (
                _Update(
                    1, callback_query=_Callback(id="c", message=prompt, data="hitl:hitl-1:yes")
                ),
            )

    monkeypatch.setattr(telegram, "Bot", _Bot)
    client = _HttpTelegramClient(bot_token=token)

    with caplog.at_level(logging.WARNING):
        client.poll_reply(
            chat_id="424242",
            prompt_message_id=10,  # mismatch → near-miss (wrong_message_id)
            update_offset=1,
            interaction_id="hitl-1",
            kind="approval",
            deadline_monotonic=time.monotonic() + 0.2,
        )

    assert "wrong_message_id" in caplog.text
    assert token not in caplog.text and "424242" not in caplog.text  # no token, no raw chat id


def test_http_client_getupdates_conflict_surfaces_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConflictBot(_FakeBot):
        async def get_updates(self, **_: object) -> tuple[_Update, ...]:
            raise telegram.error.Conflict("terminated by other getUpdates request")

    monkeypatch.setattr(telegram, "Bot", _ConflictBot)
    client = _HttpTelegramClient(bot_token="token")

    with pytest.raises(RuntimeError, match="only one poller may run per bot token"):
        client.poll_reply(
            chat_id="42",
            prompt_message_id=10,
            update_offset=1,
            interaction_id="hitl-1",
            kind="approval",
            deadline_monotonic=time.monotonic() + 1,
        )
