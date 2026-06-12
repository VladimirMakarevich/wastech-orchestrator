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


@dataclass
class _Update:
    update_id: int
    message: _Message


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

    async def send_message(self, *, chat_id: str, text: str) -> None:
        logging.getLogger("httpx").critical(
            "HTTP Request: https://api.telegram.org/bot%s/sendMessage", self._token
        )

    async def get_updates(self, *, offset: int | None, timeout: int) -> tuple[_Update, ...]:
        return (_Update(1, _Message(_Chat(42), "yes")),)


def test_http_client_uses_async_lifecycle_without_leaking_request_url(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    token = "bot-token-secret-1234"
    monkeypatch.setattr(telegram, "Bot", _FakeBot)
    client = _HttpTelegramClient(bot_token=token)

    with caplog.at_level(logging.CRITICAL):
        client.send_message(chat_id="42", text="hello")
        reply = client.poll_reply(chat_id="42", deadline_monotonic=time.monotonic() + 1)

    assert reply == "yes"
    assert token not in caplog.text
