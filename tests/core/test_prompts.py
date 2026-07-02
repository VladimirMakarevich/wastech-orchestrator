"""Unit tests for the safe prompt renderer (:func:`render_prompt` + the allowlist, spec)."""

from __future__ import annotations

from importlib import resources

import pytest

from wastech_orchestrator.core.prompts import (
    ALLOWED_PROMPT_VARS,
    referenced_variables,
    render_prompt,
)


def test_render_substitutes_only_allowlisted_names() -> None:
    template = "task {task_id} stage {stage} at {repo_path}"
    out = render_prompt(template, {"task_id": "T-1", "stage": "review", "repo_path": "/r"})
    assert out == "task T-1 stage review at /r"


def test_render_none_value_becomes_empty() -> None:
    assert render_prompt("plan={plan_path}.", {"plan_path": None}) == "plan=."


def test_render_leaves_unknown_and_literal_braces_verbatim() -> None:
    # Unknown name and literal code/JSON braces must pass through untouched (no KeyError).
    template = 'keep {unknown} and {"json": 1} and {task_id}'
    out = render_prompt(template, {"task_id": "T-1"})
    assert out == 'keep {unknown} and {"json": 1} and T-1'


def test_conditional_block_kept_when_var_present() -> None:
    template = (
        "lead {?subtask_spec_path}subtask {subtask_order} of {subtask_count}: "
        "{subtask_spec_path}{/subtask_spec_path} tail"
    )
    out = render_prompt(
        template,
        {"subtask_order": 2, "subtask_count": 3, "subtask_spec_path": "specs/2.md"},
    )
    assert out == "lead subtask 2 of 3: specs/2.md tail"


def test_conditional_block_dropped_when_var_absent_none_or_empty() -> None:
    template = (
        "lead {?subtask_spec_path}subtask {subtask_order} of "
        "{subtask_count}{/subtask_spec_path} tail"
    )
    # absent
    assert render_prompt(template, {}) == "lead  tail"
    # explicit None
    assert render_prompt(template, {"subtask_spec_path": None}) == "lead  tail"
    # empty string
    assert render_prompt(template, {"subtask_spec_path": ""}) == "lead  tail"


def test_conditional_block_non_allowlisted_or_unclosed_left_verbatim() -> None:
    # a non-allowlisted block name passes through untouched (safe-renderer contract)
    assert render_prompt("{?unknown}body{/unknown}", {}) == "{?unknown}body{/unknown}"
    # an unbalanced/unclosed block is not a block — left verbatim, inner vars still substitute
    assert render_prompt("{?subtask_spec_path}x {task_id}", {"task_id": "T-1"}) == (
        "{?subtask_spec_path}x T-1"
    )


def test_allowlist_matches_documented_variables() -> None:
    assert {
        "task_id",
        "stage",
        "repo_path",
        "repo",  # flow-engine alias for repo_path (flow-engine P1.3)
        "task_path",
        "plan_path",
        "diff_path",
        "checks_path",
        "review_path",
        "subtask_order",
        "subtask_count",
        "subtask_spec_path",
        "skills_path",
        "memory_path",  # per-node retrieval packet path (memory subsystem, phase 03)
    } == ALLOWED_PROMPT_VARS


def test_referenced_variables_extracts_bare_and_conditional_tokens() -> None:
    # Both a bare {name} and a {?name}...{/name} block name are reported; literal code/JSON braces
    # (which do not match the token shape) are ignored, like the renderer leaves them verbatim.
    template = 'use {plan_path} and {?memory_path}see {memory_path}{/memory_path} keep {"json": 1}'
    assert referenced_variables(template) == {"plan_path", "memory_path"}


def test_referenced_variables_reports_unknown_names_without_judging() -> None:
    # It extracts, it does not filter against the allowlist — that is the lint's job.
    assert referenced_variables("a {plna_path} b {task_id}") == {"plna_path", "task_id"}


def test_memory_path_conditional_block_kept_and_dropped() -> None:
    # The packaged role prompts wrap the memory reference in {?memory_path}...{/memory_path} so it
    # is present only when a packet was built, and disappears cleanly when memory is empty (AC-R4).
    template = "plan {?memory_path}see brief at {memory_path}{/memory_path} done"
    assert render_prompt(template, {"memory_path": "/logs/t1/memory/planning.md"}) == (
        "plan see brief at /logs/t1/memory/planning.md done"
    )
    assert render_prompt(template, {"memory_path": None}) == "plan  done"
    assert render_prompt(template, {}) == "plan  done"


@pytest.mark.parametrize("role", ["planning", "implementation", "review", "fixing"])
def test_packaged_default_role_prompts_reference_memory_path(role: str) -> None:
    # 03.4 / AC-R1: the four default role prompts reference {memory_path} inside a conditional block
    # so they render the reference when memory is on and drop it cleanly when memory is off (AC-R4).
    template = (
        resources.files("wastech_orchestrator")
        .joinpath("packaged", "flows", "implementation", f"{role}.md")
        .read_text(encoding="utf-8")
    )
    assert "{?memory_path}" in template and "{memory_path}" in template
    on = render_prompt(template, {"memory_path": "/logs/t1/memory/brief.md"})
    assert "/logs/t1/memory/brief.md" in on
    off = render_prompt(template, {"memory_path": None})
    assert "memory_path" not in off  # no dangling variable or block marker when memory is empty
