"""Fixtures and a recording fake client for the notify tests (no real network)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from wastech_orchestrator.config.schema import TelegramConfig
from wastech_orchestrator.observability.logging import LOGGER_NAME


@pytest.fixture(autouse=True)
def _enable_package_logger_propagation() -> Iterator[None]:
    """Ensure caplog (attached to root) can see notifier log records.

    Other tests in the suite may call ``configure_logging``, which sets
    ``propagate=False`` on the package logger to keep CLI output deterministic. That state
    leaks across tests and would silently drop the records ``caplog`` relies on here.
    """
    pkg = logging.getLogger(LOGGER_NAME)
    saved = pkg.propagate
    pkg.propagate = True
    try:
        yield
    finally:
        pkg.propagate = saved


@dataclass
class FakeTelegramClient:
    """Captures every ``send_message`` and answers ``poll_reply`` from a scripted queue.

    Tests load ``replies`` to control what ``poll_reply`` returns and may set ``send_error`` /
    ``poll_error`` to simulate transport failures. The deadline is ignored — the notifier already
    enforces ``timeout_s``; tests pin the clock with a fake ``monotonic``.
    """

    sent: list[dict[str, Any]] = field(default_factory=list)
    replies: list[str | None] = field(default_factory=list)
    send_error: Exception | None = None
    poll_error: Exception | None = None
    deadlines: list[float] = field(default_factory=list)

    def send_message(self, *, chat_id: str, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"chat_id": chat_id, "text": text})

    def poll_reply(self, *, chat_id: str, deadline_monotonic: float) -> str | None:
        if self.poll_error is not None:
            raise self.poll_error
        self.deadlines.append(deadline_monotonic)
        if not self.replies:
            return None
        return self.replies.pop(0)


@pytest.fixture
def fake_client() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def telegram_config() -> TelegramConfig:
    return TelegramConfig(
        enabled=True,
        bot_token_env="TG_BOT_TOKEN_FOR_TEST",
        chat_id_env="TG_CHAT_ID_FOR_TEST",
        ask_timeout_s=5,
    )
