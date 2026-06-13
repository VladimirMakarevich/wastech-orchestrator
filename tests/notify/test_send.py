"""Terminal notifications via the fake client: shape + failure semantics."""

from __future__ import annotations

import logging

from wastech_orchestrator.notify.telegram import TelegramNotifier, _Secrets

from .conftest import FakeTelegramClient


def _notifier(
    client: FakeTelegramClient,
    *,
    token: str = "bot-token-secret-1234",
    chat_id: str = "123456",
) -> TelegramNotifier:
    return TelegramNotifier(
        client=client,
        secrets=_Secrets(bot_token=token, chat_id=chat_id),
        ask_timeout_s=5,
        monotonic=lambda: 0.0,
    )


def test_send_done_contains_task_id_status_and_pr(fake_client: FakeTelegramClient) -> None:
    n = _notifier(fake_client)
    n.send_notification(
        task_id="task-001", final_status="done", pr_url="https://example/pr/9", reason=None
    )
    assert len(fake_client.sent) == 1
    text = fake_client.sent[0]["text"]
    assert "task-001" in text
    assert "done" in text
    assert "https://example/pr/9" in text


def test_send_failed_includes_reason(fake_client: FakeTelegramClient) -> None:
    n = _notifier(fake_client)
    n.send_notification(
        task_id="task-002", final_status="failed", pr_url=None, reason="strict_isolation"
    )
    assert len(fake_client.sent) == 1
    text = fake_client.sent[0]["text"]
    assert "task-002" in text and "failed" in text and "strict_isolation" in text
    assert "pr=" not in text  # no PR URL → field omitted


def test_send_includes_contacts_as_plain_text(fake_client: FakeTelegramClient) -> None:
    n = _notifier(fake_client)
    n.send_notification(
        task_id="task-contacts",
        final_status="done",
        pr_url=None,
        reason=None,
        contacts=("@owner", "@ops"),
    )

    assert "contacts=@owner @ops" in fake_client.sent[0]["text"]


def test_send_manual_action_required(fake_client: FakeTelegramClient) -> None:
    n = _notifier(fake_client)
    n.send_notification(
        task_id="task-003",
        final_status="manual_action_required",
        pr_url=None,
        reason="stuck: max_fix_cycles",
    )
    assert len(fake_client.sent) == 1
    assert "manual_action_required" in fake_client.sent[0]["text"]


def test_send_failure_is_swallowed(fake_client: FakeTelegramClient) -> None:
    fake_client.send_error = RuntimeError("network down")
    n = _notifier(fake_client)
    # Must not raise — best-effort by contract.
    n.send_notification(task_id="task-004", final_status="done", pr_url=None, reason=None)
    assert fake_client.sent == []


def test_send_failure_log_redacts_token_and_chat_id(
    fake_client: FakeTelegramClient, caplog
) -> None:
    token = "bot-token-secret-1234"
    chat_id = "987654321098"
    fake_client.send_error = RuntimeError(f"request {token} chat {chat_id}")
    n = _notifier(fake_client, token=token, chat_id=chat_id)

    with caplog.at_level(logging.WARNING, logger="wastech_orchestrator.notify.telegram"):
        n.send_notification(task_id="task-005", final_status="failed", pr_url=None, reason=None)

    # Render only the user-facing surfaces (message + structured fields); stdlib LogRecord
    # ephemera (``created``, ``thread``, …) can incidentally contain a chat-id-like substring.
    rendered_parts: list[str] = []
    for record in caplog.records:
        rendered_parts.append(record.getMessage())
        fields = getattr(record, "logfmt_fields", None)
        if isinstance(fields, dict):
            rendered_parts.extend(str(value) for value in fields.values())
    rendered = "\n".join(rendered_parts)
    assert token not in rendered
    assert chat_id not in rendered
    assert "[REDACTED]" in rendered


def test_outgoing_message_is_redacted_and_bounded(fake_client: FakeTelegramClient) -> None:
    token = "bot-token-secret-1234"
    chat_id = "987654321098"
    n = _notifier(fake_client, token=token, chat_id=chat_id)

    n.send_notification(
        task_id="task-long",
        final_status="failed",
        pr_url=None,
        reason=(token + chat_id + "x" * 5000),
    )

    text = fake_client.sent[0]["text"]
    assert len(text) <= 4096
    assert token not in text
    assert chat_id not in text
    assert "message truncated" in text
