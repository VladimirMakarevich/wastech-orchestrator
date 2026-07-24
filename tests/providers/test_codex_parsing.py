"""Unit tests for the Codex JSONL event parser."""

from __future__ import annotations

import json

import pytest

from wastech_orchestrator.providers.base import ErrorClass, ProviderError, UsageScope
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


def test_usage_read_from_terminal_event() -> None:
    # F22: codex-cli 0.139.0's terminal `turn.completed` event carries `usage` directly (no
    # separate `usage`/`token_count` event) — mirrors claude.py's parse_stream_json.
    stream = _stream({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}})
    parsed = parse_events(stream)
    assert parsed.usage == {"input_tokens": 7, "output_tokens": 3}


def test_normalized_usage_is_cumulative_with_derived_uncached() -> None:
    # Codex reports input inclusive of the cached subset, so uncached is derived; scope is always
    # session-cumulative and there is no cache-creation counter.
    stream = _stream(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 141464,
                "cached_input_tokens": 76288,
                "output_tokens": 8329,
                "reasoning_output_tokens": 5935,
            },
        }
    )
    nu = parse_events(stream).normalized_usage
    assert nu is not None
    assert nu.scope is UsageScope.SESSION_CUMULATIVE
    assert nu.input_total == 141464
    assert nu.cache_read == 76288
    assert nu.cache_write is None
    assert nu.uncached_input == 141464 - 76288
    assert nu.output_total == 8329
    assert nu.reasoning_output == 5935
    assert nu.cost is None  # VF-8: Codex emits no dollar figure → cost stays None


def test_normalized_usage_absent_when_no_usage_emitted() -> None:
    # No usage payload → no normalized record (so the no-work guard keeps its "absent never fires").
    parsed = parse_events(_stream({"type": "result", "status": "success"}))
    assert parsed.normalized_usage is None


def test_schema_requested_falls_back_to_last_message_json() -> None:
    # F19: on codex-cli 0.139.0 a schema-constrained run's `turn.completed` carries no `output`
    # field at all — the structured result instead lands in the last-message file. When a schema
    # was requested and the event stream had no `output`, parse the last message as the result.
    stream = _stream({"type": "turn.completed", "status": "success"})
    parsed = parse_events(stream, last_message_text='{"findings": []}', schema_requested=True)
    assert parsed.structured_output == {"findings": []}


def test_schema_not_requested_ignores_last_message_json() -> None:
    # Without a requested schema, an incidentally JSON-shaped last message must not be guessed into
    # structured_output — only a schema-requested run engages the fallback.
    stream = _stream({"type": "turn.completed", "status": "success"})
    parsed = parse_events(stream, last_message_text='{"findings": []}', schema_requested=False)
    assert parsed.structured_output is None


def test_schema_requested_unparseable_last_message_stays_none() -> None:
    # Fail-closed: a schema was requested but the last message is not valid JSON — leave
    # structured_output at None rather than guessing (the evaluator runner then fails closed).
    stream = _stream({"type": "turn.completed", "status": "success"})
    parsed = parse_events(stream, last_message_text="Looks good, no issues.", schema_requested=True)
    assert parsed.structured_output is None


def test_schema_requested_event_output_wins_over_last_message() -> None:
    # When the event stream DOES carry a terminal `output` dict, it stays authoritative — the
    # last-message fallback only engages when the event stream had none.
    stream = _stream(
        {"type": "result", "status": "success", "output": {"findings": ["from-event"]}}
    )
    parsed = parse_events(
        stream, last_message_text='{"findings": ["from-last-message"]}', schema_requested=True
    )
    assert parsed.structured_output == {"findings": ["from-event"]}
