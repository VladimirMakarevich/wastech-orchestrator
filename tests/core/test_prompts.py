"""Unit tests for the safe prompt renderer (:func:`render_prompt` + the allowlist, spec)."""

from __future__ import annotations

from wastech_orchestrator.core.prompts import ALLOWED_PROMPT_VARS, render_prompt


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
    } == ALLOWED_PROMPT_VARS
