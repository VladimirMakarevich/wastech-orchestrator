"""Unit tests for the Claude ``stream-json`` event parser."""

from __future__ import annotations

import json

import pytest

from wastech_orchestrator.providers.base import ErrorClass, ProviderError
from wastech_orchestrator.providers.claude import parse_stream_json


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_well_formed_stream_populates_all_fields() -> None:
    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "sess-42"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "I implemented it.",
            "session_id": "sess-42",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "structured_output": {"summary": "done"},
        },
    )
    parsed = parse_stream_json(stream)
    assert parsed.succeeded is True
    assert parsed.session_id == "sess-42"
    assert parsed.final_message == "I implemented it."
    assert parsed.usage == {"input_tokens": 100, "output_tokens": 50}
    assert parsed.structured_output == {"summary": "done"}


def test_is_error_marks_not_succeeded() -> None:
    parsed = parse_stream_json(
        _stream({"type": "result", "subtype": "error_during_execution", "is_error": True})
    )
    assert parsed.succeeded is False


def test_error_subtype_marks_not_succeeded() -> None:
    parsed = parse_stream_json(_stream({"type": "result", "subtype": "error_max_turns"}))
    assert parsed.succeeded is False


def test_non_dict_structured_output_is_ignored() -> None:
    parsed = parse_stream_json(
        _stream({"type": "result", "subtype": "success", "structured_output": "not-a-dict"})
    )
    assert parsed.structured_output is None


def test_stray_non_json_line_is_tolerated_with_terminal_event() -> None:
    stream = "garbage not json\n" + _stream({"type": "result", "subtype": "success"})
    parsed = parse_stream_json(stream)
    assert parsed.succeeded is True


def test_missing_terminal_event_is_invalid_output() -> None:
    stream = _stream({"type": "assistant", "message": {"content": []}})  # no result event
    with pytest.raises(ProviderError) as exc:
        parse_stream_json(stream)
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT


def test_empty_stream_is_invalid_output() -> None:
    with pytest.raises(ProviderError) as exc:
        parse_stream_json("")
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT


def test_fully_malformed_stream_is_invalid_output() -> None:
    with pytest.raises(ProviderError) as exc:
        parse_stream_json("not json at all\n{still not json")
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT
