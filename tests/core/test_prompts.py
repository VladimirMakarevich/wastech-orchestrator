"""Unit tests for the prompt-template layer (backlog: prompt_template_customization).

Covers the :class:`PromptTemplateStore` (packaged defaults + auto-detected ``<stage>.md`` template
files, append/replace, fallback when a file is absent) and the safe :func:`render_prompt`
substitution (allowlist + literal braces).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import PromptMode, PromptsConfig
from wastech_orchestrator.core.prompts import (
    ALLOWED_PROMPT_VARS,
    PromptTemplateStore,
    render_prompt,
)
from wastech_orchestrator.providers.base import Stage

_ROUTABLE = (
    Stage.REFINEMENT,
    Stage.PLANNING,
    Stage.IMPLEMENTATION,
    Stage.REVIEW,
    Stage.FIXING,
    Stage.SUMMARY,
)


def _packaged_text(stage: Stage) -> str:
    from importlib import resources

    resource = resources.files("wastech_orchestrator").joinpath(
        "templates", "prompts", f"{stage.value}.md"
    )
    return resource.read_text(encoding="utf-8").strip()


def test_defaults_render_packaged_templates() -> None:
    store = PromptTemplateStore(PromptsConfig())
    for stage in _ROUTABLE:
        assert store.resolved(stage) == _packaged_text(stage)
        assert store.resolved(stage)  # non-empty


def _override_dir(tmp_path: Path, **files: str) -> Path:
    """Create a templates dir holding ``<stage>.md`` files (keys are ``"implementation"`` etc.)."""
    tdir = tmp_path / "prompts"
    tdir.mkdir()
    for stem, body in files.items():
        (tdir / f"{stem}.md").write_text(body, encoding="utf-8")
    return tdir


def test_append_combines_default_then_file(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path, implementation="EXTRA RULES")
    store = PromptTemplateStore(PromptsConfig(templates_dir=str(tdir), mode=PromptMode.APPEND))
    resolved = store.resolved(Stage.IMPLEMENTATION)
    assert resolved == f"{_packaged_text(Stage.IMPLEMENTATION)}\n\nEXTRA RULES"
    # A stage with no file is untouched.
    assert store.resolved(Stage.REVIEW) == _packaged_text(Stage.REVIEW)


def test_replace_is_the_default_and_uses_file_only(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path, review="ONLY THIS RUBRIC")
    # PromptsConfig() defaults to replace mode.
    store = PromptTemplateStore(PromptsConfig(templates_dir=str(tdir)))
    assert store.resolved(Stage.REVIEW) == "ONLY THIS RUBRIC"
    # A stage without a file still falls back to the packaged default, even in replace mode.
    assert store.resolved(Stage.IMPLEMENTATION) == _packaged_text(Stage.IMPLEMENTATION)


def test_file_present_is_auto_detected_without_any_opt_in(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path, planning="MY PLAN GUIDANCE")
    store = PromptTemplateStore(PromptsConfig(templates_dir=str(tdir)))
    assert store.resolved(Stage.PLANNING) == "MY PLAN GUIDANCE"
    assert store.override_for(Stage.PLANNING) == "MY PLAN GUIDANCE"
    assert store.override_for(Stage.REVIEW) is None


def test_missing_file_falls_back_to_packaged_default_no_error(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path)  # empty — no <stage>.md files at all
    # No fail-closed path remains: an absent file is the normal fallback, never a ConfigError.
    store = PromptTemplateStore(PromptsConfig(templates_dir=str(tdir)))
    for stage in _ROUTABLE:
        assert store.resolved(stage) == _packaged_text(stage)
        assert store.override_for(stage) is None


def test_empty_templates_dir_forces_packaged_defaults(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path, implementation="SHOULD BE IGNORED")
    store = PromptTemplateStore(PromptsConfig(templates_dir=""))
    # Empty templates_dir is the explicit opt-out: every stage uses the packaged default, even
    # though a file exists on disk elsewhere.
    assert store.resolved(Stage.IMPLEMENTATION) == _packaged_text(Stage.IMPLEMENTATION)
    assert store.override_for(Stage.IMPLEMENTATION) is None
    # silence "unused" — tdir is intentionally not wired in
    assert (tdir / "implementation.md").exists()


def test_empty_file_warns_and_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tdir = _override_dir(tmp_path, implementation="   \n  ")  # whitespace-only → empty after strip
    # The library logger ("wastech_orchestrator") sets propagate=False, so pass a propagating
    # logger to make the warning visible to caplog (which captures on the root logger).
    test_logger = logging.getLogger("test.prompts.empty")
    with caplog.at_level(logging.WARNING):
        store = PromptTemplateStore(
            PromptsConfig(templates_dir=str(tdir)), logger=test_logger
        )
    assert store.resolved(Stage.IMPLEMENTATION) == _packaged_text(Stage.IMPLEMENTATION)
    assert any("packaged default" in rec.getMessage() for rec in caplog.records)


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
