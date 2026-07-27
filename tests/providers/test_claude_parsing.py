"""Unit tests for the Claude ``stream-json`` event parser."""

from __future__ import annotations

import json

import pytest

from wastech_orchestrator.providers.base import ErrorClass, ProviderError, UsageScope
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


def test_failure_subtype_carries_the_error_subtype() -> None:
    parsed = parse_stream_json(_stream({"type": "result", "subtype": "error_max_turns"}))
    assert parsed.succeeded is False
    assert parsed.failure_subtype == "error_max_turns"


def test_success_has_no_failure_subtype() -> None:
    parsed = parse_stream_json(_stream({"type": "result", "subtype": "success", "is_error": False}))
    assert parsed.succeeded is True
    assert parsed.failure_subtype is None


def test_normalized_usage_sums_three_input_fields_per_invocation() -> None:
    # Claude's real input is input + cache_creation + cache_read; reading only input_tokens would
    # show a misleadingly tiny figure. Scope is per-invocation (each run is self-contained).
    stream = _stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {
                "input_tokens": 35,
                "cache_creation_input_tokens": 122738,
                "cache_read_input_tokens": 560305,
                "output_tokens": 9000,
            },
        }
    )
    nu = parse_stream_json(stream).normalized_usage
    assert nu is not None
    assert nu.scope is UsageScope.PER_INVOCATION
    assert nu.input_total == 35 + 122738 + 560305
    assert nu.uncached_input == 35
    assert nu.cache_write == 122738
    assert nu.cache_read == 560305
    assert nu.output_total == 9000
    assert nu.reasoning_output is None  # Claude folds reasoning into output
    assert nu.cost is None  # no total_cost_usd emitted → cost stays None (never guessed)


def test_normalized_usage_captures_total_cost_usd() -> None:
    # The terminal ``result`` event carries ``total_cost_usd`` as a SIBLING of ``usage`` (not
    # inside it); it maps onto the per-invocation ``cost`` so provider_attempts.usage_cost fills.
    stream = _stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": 0.0428,
            "usage": {"input_tokens": 35, "output_tokens": 9000},
        }
    )
    nu = parse_stream_json(stream).normalized_usage
    assert nu is not None
    assert nu.cost == 0.0428


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
