"""Unit tests for the Codex JSONL event parser (spec §4.4)."""

from __future__ import annotations

import json

import pytest

from wastech_orchestrator.providers.base import ErrorClass, ProviderError
from wastech_orchestrator.providers.codex import parse_events


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_well_formed_stream_populates_all_fields() -> None:
    stream = _stream(
        {"type": "session", "session_id": "sess-42"},
        {"type": "message", "role": "assistant", "text": "I implemented it."},
        {"type": "usage", "input_tokens": 100, "output_tokens": 50},
        {"type": "result", "status": "success", "output": {"summary": "done"}},
    )
    parsed = parse_events(stream)
    assert parsed.succeeded is True
    assert parsed.session_id == "sess-42"
    assert parsed.final_message == "I implemented it."
    assert parsed.usage == {"input_tokens": 100, "output_tokens": 50}
    assert parsed.structured_output == {"summary": "done"}


def test_codex_exec_resume_parses_thread_started() -> None:
    # Durable sessions (P2.2): ``codex exec`` emits ``thread.started`` carrying the resumable
    # thread id, which the adapter normalizes into ``session_id``.
    stream = _stream(
        {"type": "thread.started", "thread_id": "thread-abc"},
        {"type": "message", "text": "resumed and finished"},
        {"type": "result", "status": "success"},
    )
    parsed = parse_events(stream)
    assert parsed.session_id == "thread-abc"
    assert parsed.succeeded is True


def test_failure_status_marks_not_succeeded() -> None:
    parsed = parse_events(_stream({"type": "result", "status": "failed"}))
    assert parsed.succeeded is False


def test_last_message_file_overrides_message_event() -> None:
    stream = _stream(
        {"type": "message", "text": "stream message"},
        {"type": "result", "status": "success"},
    )
    parsed = parse_events(stream, last_message_text="final message from file\n")
    assert parsed.final_message == "final message from file"


def test_non_dict_output_is_ignored() -> None:
    parsed = parse_events(_stream({"type": "result", "status": "ok", "output": "not-a-dict"}))
    assert parsed.structured_output is None


def test_stray_non_json_line_is_tolerated_with_terminal_event() -> None:
    stream = "garbage not json\n" + _stream({"type": "result", "status": "success"})
    parsed = parse_events(stream)
    assert parsed.succeeded is True


def test_missing_terminal_event_is_invalid_output() -> None:
    stream = _stream({"type": "message", "text": "hi"})  # no result event
    with pytest.raises(ProviderError) as exc:
        parse_events(stream)
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT


def test_empty_stream_is_invalid_output() -> None:
    with pytest.raises(ProviderError) as exc:
        parse_events("")
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT


def test_fully_malformed_stream_is_invalid_output() -> None:
    with pytest.raises(ProviderError) as exc:
        parse_events("not json at all\n{still not json")
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT
