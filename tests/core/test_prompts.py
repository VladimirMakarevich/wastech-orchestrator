"""Unit tests for the safe prompt renderer (:func:`render_prompt` + the allowlist, spec §6)."""

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
