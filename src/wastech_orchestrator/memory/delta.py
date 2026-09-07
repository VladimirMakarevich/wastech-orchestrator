"""Candidate memory delta — the structured output the supervisor emits at finalize.

The supervisor (the single LLM touch in the write path) proposes *what to remember*; this module
defines the contract and a tolerant, model-free parser that turns the raw structured output into
typed **candidate** records. Candidates are not stored records:

* They carry a ``trust_hint`` that is **advisory only** — :meth:`MemoryService.apply_delta` assigns
  the final trust deterministically; a candidate can never self-certify to a durable
  level.
* ``evidence`` entries are **pointers** (artifact path / commit / symbol), never raw content.

All *semantic* validation (non-empty evidence, path/symbol existence, promotion) lives in
``apply_delta`` (02.4), not here — this parser is purely structural. Like the codebase's other
structured-output parsers it is best-effort: malformed entries are skipped, fully unusable
input yields ``None``, and it **never raises**. The provider turn is constrained by
:data:`DELTA_OUTPUT_SCHEMA` (``additionalProperties: False``), so extra fields are rejected at
generation time and never reach the parser.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from wastech_orchestrator.memory.records import Evidence, LongTermKind, Relationship, Scope

# Lesson kinds the supervisor may propose. Failures are a separate list with their own shape, so
# ``failure`` is intentionally excluded here.
_LESSON_KINDS: frozenset[LongTermKind] = frozenset(
    {LongTermKind.SEMANTIC, LongTermKind.PROCEDURAL, LongTermKind.REVIEWER}
)


@dataclass(frozen=True)
class CandidateLesson:
    """A proposed long-term lesson (semantic/procedural/reviewer). ``trust_hint`` is advisory."""

    kind: LongTermKind
    subject: str
    statement: str
    rationale: str | None = None
    scope: Scope = field(default_factory=Scope)
    evidence: tuple[Evidence, ...] = ()
    trust_hint: str | None = None


@dataclass(frozen=True)
class CandidateFailure:
    """A proposed failure record (its ``signature`` is the normalized failure subject)."""

    signature: str
    paths: tuple[str, ...] = ()
    remedy: str | None = None
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class CandidateEntity:
    """A proposed entity card (file / module / context / dependency / owner)."""

    entity_id: str
    entity_type: str
    paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    summary: str = ""
    relationships: tuple[Relationship, ...] = ()
    risk_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateDelta:
    """The whole proposal: lessons + failures + entities (any of which may be empty)."""

    lessons: tuple[CandidateLesson, ...] = ()
    failures: tuple[CandidateFailure, ...] = ()
    entities: tuple[CandidateEntity, ...] = ()

    def is_empty(self) -> bool:
        return not (self.lessons or self.failures or self.entities)


# The strict provider schema for the candidate delta.
# ``additionalProperties: False`` everywhere forbids unexpected fields at generation time. Task 02.2
# nests this under a ``{summary, memory_delta}`` finalize schema so summary + delta ride one turn.
_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "minLength": 1},
        "ref": {"type": "string", "minLength": 1},
    },
    "required": ["type", "ref"],
}
# Nullable so a strict-required optional field can be omitted by emitting ``null``: OpenAI
# strict mode forces every ``properties`` key into ``required``, so optionality is expressed by the
# type union, not by absence. Every use below is an optional field; the tolerant readers treat
# ``null`` identically to an absent key.
_OPT_STR_ARRAY: dict[str, Any] = {
    "type": ["array", "null"],
    "items": {"type": "string", "minLength": 1},
}
_OPT_EVIDENCE_ARRAY: dict[str, Any] = {"type": ["array", "null"], "items": _EVIDENCE_SCHEMA}
DELTA_OUTPUT_SCHEMA: dict[str, Any] = {
    # Nullable root (``["object", "null"]``): nested as the finalize turn's ``memory_delta``, so a
    # step with nothing to record emits ``memory_delta: null`` rather than a hollow object.
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "lessons": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["semantic", "procedural", "reviewer"]},
                    "subject": {"type": "string", "minLength": 1},
                    "statement": {"type": "string", "minLength": 1},
                    "rationale": {"type": ["string", "null"]},
                    "scope": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": {
                            "paths": _OPT_STR_ARRAY,
                            "symbols": _OPT_STR_ARRAY,
                            "nodes": _OPT_STR_ARRAY,
                        },
                        "required": ["paths", "symbols", "nodes"],
                    },
                    "evidence": _OPT_EVIDENCE_ARRAY,
                    "trust_hint": {"type": ["string", "null"]},
                },
                "required": [
                    "kind",
                    "subject",
                    "statement",
                    "rationale",
                    "scope",
                    "evidence",
                    "trust_hint",
                ],
            },
        },
        "failures": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "signature": {"type": "string", "minLength": 1},
                    "paths": _OPT_STR_ARRAY,
                    "remedy": {"type": ["string", "null"]},
                    "evidence": _OPT_EVIDENCE_ARRAY,
                },
                "required": ["signature", "paths", "remedy", "evidence"],
            },
        },
        "entities": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "entity_id": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "minLength": 1},
                    "paths": _OPT_STR_ARRAY,
                    "symbols": _OPT_STR_ARRAY,
                    "summary": {"type": ["string", "null"]},
                    "relationships": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "type": {"type": "string", "minLength": 1},
                                "target": {"type": "string", "minLength": 1},
                            },
                            "required": ["type", "target"],
                        },
                    },
                    "risk_notes": _OPT_STR_ARRAY,
                },
                "required": [
                    "entity_id",
                    "type",
                    "paths",
                    "symbols",
                    "summary",
                    "relationships",
                    "risk_notes",
                ],
            },
        },
    },
    "required": ["lessons", "failures", "entities"],
}


# --- tolerant structural readers (model output → typed; never raise) ---


def _nonempty_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _opt_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _str_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _parse_evidence(items: Any) -> tuple[Evidence, ...]:
    if not isinstance(items, list):
        return ()
    out: list[Evidence] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        kind = _nonempty_str(item, "type")
        ref = _nonempty_str(item, "ref")
        if kind is not None and ref is not None:
            out.append(Evidence(type=kind, ref=ref))
    return tuple(out)


def _parse_scope(raw: Any) -> Scope:
    if not isinstance(raw, Mapping):
        return Scope()
    return Scope(
        paths=_str_tuple(raw, "paths"),
        symbols=_str_tuple(raw, "symbols"),
        nodes=_str_tuple(raw, "nodes"),
    )


def _parse_relationships(items: Any) -> tuple[Relationship, ...]:
    if not isinstance(items, list):
        return ()
    out: list[Relationship] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        kind = _nonempty_str(item, "type")
        target = _nonempty_str(item, "target")
        if kind is not None and target is not None:
            out.append(Relationship(type=kind, target=target))
    return tuple(out)


def _parse_lesson(raw: Any) -> CandidateLesson | None:
    if not isinstance(raw, Mapping):
        return None
    kind_raw = _nonempty_str(raw, "kind")
    subject = _nonempty_str(raw, "subject")
    statement = _nonempty_str(raw, "statement")
    if kind_raw is None or subject is None or statement is None:
        return None
    try:
        kind = LongTermKind(kind_raw)
    except ValueError:
        return None
    if kind not in _LESSON_KINDS:
        return None
    return CandidateLesson(
        kind=kind,
        subject=subject,
        statement=statement,
        rationale=_opt_str(raw, "rationale"),
        scope=_parse_scope(raw.get("scope")),
        evidence=_parse_evidence(raw.get("evidence")),
        trust_hint=_opt_str(raw, "trust_hint"),
    )


def _parse_failure(raw: Any) -> CandidateFailure | None:
    if not isinstance(raw, Mapping):
        return None
    signature = _nonempty_str(raw, "signature")
    if signature is None:
        return None
    return CandidateFailure(
        signature=signature,
        paths=_str_tuple(raw, "paths"),
        remedy=_opt_str(raw, "remedy"),
        evidence=_parse_evidence(raw.get("evidence")),
    )


def _parse_entity(raw: Any) -> CandidateEntity | None:
    if not isinstance(raw, Mapping):
        return None
    entity_id = _nonempty_str(raw, "entity_id")
    entity_type = _nonempty_str(raw, "type")
    if entity_id is None or entity_type is None:
        return None
    return CandidateEntity(
        entity_id=entity_id,
        entity_type=entity_type,
        paths=_str_tuple(raw, "paths"),
        symbols=_str_tuple(raw, "symbols"),
        summary=_opt_str(raw, "summary") or "",
        relationships=_parse_relationships(raw.get("relationships")),
        risk_notes=_str_tuple(raw, "risk_notes"),
    )


def _parse_list[T](items: Any, parse_one: Callable[[Any], T | None]) -> tuple[T, ...]:
    if not isinstance(items, list):
        return ()
    return tuple(parsed for parsed in (parse_one(item) for item in items) if parsed is not None)


def parse_delta(raw: Any) -> CandidateDelta | None:
    """Parse raw structured output into a :class:`CandidateDelta`, or ``None`` if unusable.

    Best-effort and never raises: a non-mapping input, or one with no parseable record in any tier,
    yields ``None`` (the caller skips the memory write — publish is never blocked). Malformed
    individual entries are skipped; well-formed ones are kept.
    """
    if not isinstance(raw, Mapping):
        return None
    delta = CandidateDelta(
        lessons=_parse_list(raw.get("lessons"), _parse_lesson),
        failures=_parse_list(raw.get("failures"), _parse_failure),
        entities=_parse_list(raw.get("entities"), _parse_entity),
    )
    return None if delta.is_empty() else delta
