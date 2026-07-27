"""Candidate-delta contract: tolerant structural parser; trust_hint stays advisory."""

from __future__ import annotations

from typing import Any

import pytest

from wastech_orchestrator.memory import (
    DELTA_OUTPUT_SCHEMA,
    LongTermKind,
    parse_delta,
)


def _valid_raw() -> dict[str, Any]:
    return {
        "lessons": [
            {
                "kind": "semantic",
                "subject": "config-schema",
                "statement": "bump docs with schema changes",
                "rationale": "docs-sync gate",
                "scope": {
                    "paths": ["src/config/"],
                    "symbols": ["OrchestratorConfig"],
                    "nodes": ["review"],
                },
                "evidence": [{"type": "repo_doc", "ref": "CLAUDE.md"}],
                "trust_hint": "human-curated",
            }
        ],
        "failures": [
            {
                "signature": "ruff E501",
                "paths": ["x.py"],
                "remedy": "wrap the line",
                "evidence": [{"type": "check", "ref": "ruff"}],
            }
        ],
        "entities": [
            {
                "entity_id": "module:cfg",
                "type": "module",
                "paths": ["src/config/schema.py"],
                "symbols": ["MemoryConfig"],
                "summary": "config",
                "relationships": [{"type": "depends_on", "target": "module:loader"}],
                "risk_notes": ["fragile"],
            }
        ],
    }


def test_valid_delta_parses_to_typed_records() -> None:
    delta = parse_delta(_valid_raw())
    assert delta is not None
    assert len(delta.lessons) == 1
    assert len(delta.failures) == 1
    assert len(delta.entities) == 1
    lesson = delta.lessons[0]
    assert lesson.kind is LongTermKind.SEMANTIC
    assert lesson.subject == "config-schema"
    assert lesson.scope.paths == ("src/config/",)
    assert lesson.evidence[0].ref == "CLAUDE.md"
    assert delta.failures[0].signature == "ruff E501"
    assert delta.entities[0].entity_type == "module"
    assert delta.entities[0].relationships[0].target == "module:loader"


def test_trust_hint_is_advisory_not_a_final_trust_level() -> None:
    delta = parse_delta(_valid_raw())
    assert delta is not None
    # The parser keeps trust_hint as a raw advisory string; it never assigns a final trust level.
    assert delta.lessons[0].trust_hint == "human-curated"
    assert not hasattr(delta.lessons[0], "trust_level")


@pytest.mark.parametrize(
    "raw",
    [None, "string", 123, [], {}, {"lessons": [], "failures": [], "entities": []}],
)
def test_unusable_input_returns_none(raw: Any) -> None:
    assert parse_delta(raw) is None


def test_malformed_entries_are_skipped_never_raised() -> None:
    raw = {
        "lessons": [
            {"kind": "semantic", "subject": "ok", "statement": "good"},  # valid
            {"kind": "semantic", "subject": "no statement"},  # missing required -> skip
            "not a dict",  # skip
            {"kind": "bogus", "subject": "x", "statement": "y"},  # bad kind -> skip
            {"kind": "failure", "subject": "x", "statement": "y"},  # not a lesson kind
        ]
    }
    delta = parse_delta(raw)
    assert delta is not None
    assert len(delta.lessons) == 1
    assert delta.lessons[0].subject == "ok"


def test_extra_fields_are_ignored_leniently() -> None:
    # The provider schema forbids extra fields; if any slip through, the parser ignores them rather
    # than discarding an otherwise-valid record.
    raw = {"lessons": [{"kind": "reviewer", "subject": "s", "statement": "x", "bonus": 1}]}
    delta = parse_delta(raw)
    assert delta is not None
    assert len(delta.lessons) == 1


def test_evidence_and_scope_tolerate_partial_shapes() -> None:
    raw = {
        "lessons": [
            {
                "kind": "semantic",
                "subject": "s",
                "statement": "x",
                "scope": "not-a-dict",
                "evidence": [{"type": "t"}, "bad", {"type": "t", "ref": "r"}],
            }
        ]
    }
    delta = parse_delta(raw)
    assert delta is not None
    lesson = delta.lessons[0]
    assert lesson.scope.paths == ()  # non-dict scope -> empty Scope
    assert len(lesson.evidence) == 1  # only the well-formed evidence pointer is kept
    assert lesson.evidence[0].ref == "r"


def test_failure_only_delta_is_valid() -> None:
    delta = parse_delta({"failures": [{"signature": "boom"}]})
    assert delta is not None
    assert len(delta.failures) == 1
    assert not delta.lessons


def test_schema_forbids_additional_properties() -> None:
    assert DELTA_OUTPUT_SCHEMA["additionalProperties"] is False
