"""``ask_human`` answer/timeout paths exercised with a fake client + injected clock."""

from __future__ import annotations

from wastech_orchestrator.notify.telegram import TelegramNotifier, _Secrets

from .conftest import FakeTelegramClient


def _notifier(client: FakeTelegramClient) -> TelegramNotifier:
    return TelegramNotifier(
        client=client,
        secrets=_Secrets(bot_token="bot-token-secret-1234", chat_id="123456"),
        ask_timeout_s=5,
        monotonic=lambda: 100.0,
    )


def test_question_returns_text(fake_client: FakeTelegramClient) -> None:
    fake_client.replies = ["use option B"]
    n = _notifier(fake_client)
    result = n.ask_human(
        question="A or B?",
        context="some context",
        task_id="task-001",
        kind="question",
        timeout_s=5,
    )
    assert result.answered is True
    assert result.text == "use option B"
    assert result.approved is None
    assert result.timed_out is False
    # The prompt + the recognised kind are both in the sent message body.
    assert "task-001" in fake_client.sent[0]["text"]
    assert "question" in fake_client.sent[0]["text"]


def test_approval_yes_maps_to_true(fake_client: FakeTelegramClient) -> None:
    fake_client.replies = ["yes"]
    n = _notifier(fake_client)
    result = n.ask_human(question="proceed?", context="", task_id="t", kind="approval", timeout_s=5)
    assert result.answered is True and result.approved is True


def test_approval_no_maps_to_false(fake_client: FakeTelegramClient) -> None:
    fake_client.replies = ["No"]
    n = _notifier(fake_client)
    result = n.ask_human(question="proceed?", context="", task_id="t", kind="approval", timeout_s=5)
    assert result.answered is True and result.approved is False


def test_approval_unknown_reply_is_answered_but_unknown(fake_client: FakeTelegramClient) -> None:
    fake_client.replies = ["maybe later"]
    n = _notifier(fake_client)
    result = n.ask_human(question="proceed?", context="", task_id="t", kind="approval", timeout_s=5)
    assert result.answered is True
    assert result.approved is None
    assert result.text == "maybe later"


def test_no_reply_is_deterministic_timeout(fake_client: FakeTelegramClient) -> None:
    # ``replies`` empty → poll_reply returns None → timeout.
    n = _notifier(fake_client)
    result = n.ask_human(
        question="anything?", context="", task_id="t", kind="question", timeout_s=2
    )
    assert result.timed_out is True
    assert result.answered is False
    assert result.text is None


def test_timeout_is_capped_by_config(fake_client: FakeTelegramClient) -> None:
    n = _notifier(fake_client)
    n.ask_human(question="anything?", context="", task_id="t", kind="question", timeout_s=60)
    assert fake_client.deadlines == [105.0]


def test_send_failure_returns_timeout(fake_client: FakeTelegramClient) -> None:
    fake_client.send_error = RuntimeError("smtp-style outage")
    n = _notifier(fake_client)
    result = n.ask_human(question="proceed?", context="", task_id="t", kind="approval", timeout_s=2)
    # Cannot deliver the prompt → deterministic timeout, no exception escapes.
    assert result.timed_out is True
    assert result.answered is False


def test_poll_failure_returns_timeout(fake_client: FakeTelegramClient) -> None:
    fake_client.poll_error = RuntimeError("network down")
    n = _notifier(fake_client)
    result = n.ask_human(question="proceed?", context="", task_id="t", kind="question", timeout_s=2)
    assert result.timed_out is True
    assert result.answered is False
