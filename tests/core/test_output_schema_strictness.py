"""Regression guard: every provider ``output_schema`` constant must be OpenAI-strict.

codex CLI passes ``--output-schema`` to OpenAI Structured Outputs in **strict** mode, whose schema
validator rejects a request with a 400 unless every ``object`` node — top-level and nested —
satisfies **both** invariants:

* ``additionalProperties: false`` (else ``'additionalProperties' is required to be supplied and to
  be false``). ``_FINDINGS_SCHEMA`` shipped without it once and every codex evaluator turn crashed
  deterministically.
* ``required`` lists **every** key in ``properties`` (else ``'required' is required to be an array
  including every key in properties``). ``DELTA_OUTPUT_SCHEMA``'s ``scope`` object had no
  ``required`` at all, so the supervisor finalize turn crashed on codex and only survived via
  the silent claude fallback. Optionality is preserved by making optional fields
  nullable (``["string", "null"]`` etc.), not by dropping them from ``required``.

The original smoke test validated its own simplified example schema, not the real constant, so it
missed
the regress. This test walks the literal battle constants that reach a provider as ``output_schema``
and fails if any object node breaks either invariant — so a future schema addition cannot repeat
both crashes. Nullability itself is not checked here: it is how we keep fields optional, not part
of the
strict contract. Flow-authored ``node.output_schema`` (operator-supplied YAML, parsed in
``agent.py``) is not a Python literal and so is out of this guard's reach.
"""

from __future__ import annotations

from typing import Any

import pytest

from wastech_orchestrator.core.flow.nodes.evaluator import _FINDINGS_SCHEMA
from wastech_orchestrator.core.hitl import typed_output_schema
from wastech_orchestrator.core.supervisor import (
    _FOLLOW_UPS_SCHEMA,
    _HANDOFF_SCHEMA,
    _finalize_schema,
)
from wastech_orchestrator.memory.delta import DELTA_OUTPUT_SCHEMA

#: Every schema constant/factory that is (or is nested into) a provider ``output_schema``. Keyed by
#: a human-readable name so a failure points at the exact source constant.
_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "evaluator._FINDINGS_SCHEMA": _FINDINGS_SCHEMA,
    "hitl.typed_output_schema('human_input')": typed_output_schema("human_input") or {},
    "hitl.typed_output_schema('planning')": typed_output_schema("planning") or {},
    "supervisor._FOLLOW_UPS_SCHEMA": _FOLLOW_UPS_SCHEMA,
    "supervisor._HANDOFF_SCHEMA": _HANDOFF_SCHEMA,
    # All three shapes: the dynamic ``required`` must list exactly the present keys in each branch.
    "supervisor._finalize_schema(delta+follow_ups)": _finalize_schema(
        with_delta=True, with_follow_ups=True
    ),
    "supervisor._finalize_schema(delta)": _finalize_schema(with_delta=True, with_follow_ups=False),
    "supervisor._finalize_schema(follow_ups)": _finalize_schema(
        with_delta=False, with_follow_ups=True
    ),
    "memory.delta.DELTA_OUTPUT_SCHEMA": DELTA_OUTPUT_SCHEMA,
}


def _nonstrict_object_paths(schema: Any, loc: str = "$") -> list[str]:
    """Return the json-path of every ``type: object`` node missing ``additionalProperties: false``.

    Recurses the whole structure generically (properties/items/enum/…), so it catches an object node
    anywhere it hides — including a ``type: ["object", "null"]`` union.
    """
    problems: list[str] = []
    if isinstance(schema, dict):
        node_type = schema.get("type")
        is_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
        if is_object and schema.get("additionalProperties") is not False:
            problems.append(loc)
        for key, value in schema.items():
            problems.extend(_nonstrict_object_paths(value, f"{loc}.{key}"))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            problems.extend(_nonstrict_object_paths(value, f"{loc}[{index}]"))
    return problems


@pytest.mark.parametrize("name", sorted(_OUTPUT_SCHEMAS))
def test_output_schema_object_nodes_are_strict(name: str) -> None:
    problems = _nonstrict_object_paths(_OUTPUT_SCHEMAS[name])
    assert not problems, (
        f"{name}: object nodes missing additionalProperties:false at {problems} — codex CLI "
        "rejects such a schema with a 400"
    )


def test_walker_flags_a_nonstrict_object() -> None:
    # Guard the guard: a schema with a nested object lacking additionalProperties must be caught,
    # otherwise the parametrized test above could pass vacuously.
    bad = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"inner": {"type": "object", "properties": {"x": {"type": "string"}}}},
    }
    assert _nonstrict_object_paths(bad) == ["$.properties.inner"]


def _required_incomplete_object_paths(schema: Any, loc: str = "$") -> list[str]:
    """Return the json-path of every ``type: object`` node whose ``required`` omits a property key.

    OpenAI strict mode requires ``required`` to list every key in ``properties`` on each object
    node. Recurses generically like :func:`_nonstrict_object_paths`, including ``type: ["object",
    "null"]`` unions. Each problem carries the missing keys so a failure names them.
    """
    problems: list[str] = []
    if isinstance(schema, dict):
        node_type = schema.get("type")
        is_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
        properties = schema.get("properties")
        if is_object and isinstance(properties, dict):
            required = schema.get("required")
            required_set = set(required) if isinstance(required, list) else set()
            missing = sorted(key for key in properties if key not in required_set)
            if missing:
                problems.append(f"{loc} (missing {missing})")
        for key, value in schema.items():
            problems.extend(_required_incomplete_object_paths(value, f"{loc}.{key}"))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            problems.extend(_required_incomplete_object_paths(value, f"{loc}[{index}]"))
    return problems


@pytest.mark.parametrize("name", sorted(_OUTPUT_SCHEMAS))
def test_output_schema_required_lists_every_property(name: str) -> None:
    problems = _required_incomplete_object_paths(_OUTPUT_SCHEMAS[name])
    assert not problems, (
        f"{name}: object nodes whose 'required' omits a property key at {problems} — codex CLI "
        "rejects such a schema with a 400. Add the key to 'required' and make it nullable to "
        "keep it optional."
    )


def test_required_walker_flags_an_incomplete_object() -> None:
    # Guard the guard: a nested object whose required omits a property must be caught, otherwise the
    # parametrized test above could pass vacuously.
    bad = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    assert _required_incomplete_object_paths(bad) == ["$ (missing ['b'])"]


def _maxlength_paths(schema: Any, loc: str = "$") -> list[str]:
    """Return the json-path of every node carrying a ``maxLength`` bound."""
    problems: list[str] = []
    if isinstance(schema, dict):
        if "maxLength" in schema:
            problems.append(loc)
        for key, value in schema.items():
            problems.extend(_maxlength_paths(value, f"{loc}.{key}"))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            problems.extend(_maxlength_paths(value, f"{loc}[{index}]"))
    return problems


#: The supervisor's authored-prose schemas — the summary and the follow-up records. Named apart from
#: the rest because a length bound on generated prose is the failure mode below; the HITL schemas'
#: bounds cap operator-facing question text the node composes from fields it already holds.
_PROSE_SCHEMAS = [
    "supervisor._FOLLOW_UPS_SCHEMA",
    "supervisor._finalize_schema(delta+follow_ups)",
    "supervisor._finalize_schema(delta)",
    "supervisor._finalize_schema(follow_ups)",
]


@pytest.mark.parametrize("name", _PROSE_SCHEMAS)
def test_no_maxlength_bound_on_authored_prose(name: str) -> None:
    # A hard length bound the model overshoots is rejected with a message that does not say how to
    # comply, and the observed end state of that loop is a collapsed generation: one finalize turn
    # was rejected three times for a schema violation and then published `summary: "test"` as its PR
    # body. So the ≤80-character follow-up title is asked for in the field's `description` and never
    # enforced by the validator — length expectations here belong in prose, not in the schema.
    problems = _maxlength_paths(_OUTPUT_SCHEMAS[name])
    assert not problems, (
        f"{name}: maxLength at {problems} — a bound the model overshoots costs the whole turn. "
        "State the expectation in the field's 'description' instead."
    )


@pytest.mark.parametrize(
    "name",
    [
        "supervisor._FOLLOW_UPS_SCHEMA",
        "supervisor._finalize_schema(delta+follow_ups)",
        "supervisor._finalize_schema(follow_ups)",
    ],
)
def test_follow_ups_schema_states_that_the_key_is_mandatory(name: str) -> None:
    # `follow_ups` is in `required` (strict mode leaves no choice), while a prompt saying "leave
    # the array empty when nothing qualifies" reads to a model as permission to omit the key. The
    # rejection that follows never explains the fix, so the schema has to.
    schema = _OUTPUT_SCHEMAS[name]
    node = schema if schema.get("type") == ["array", "null"] else schema["properties"]["follow_ups"]
    description = node.get("description", "")
    assert "ALWAYS present" in description and "empty array" in description
