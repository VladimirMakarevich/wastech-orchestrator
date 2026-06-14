"""Unit tests for the prompt-template layer (backlog: prompt_template_customization).

Covers the :class:`PromptTemplateStore` (packaged defaults + operator overrides, append/replace,
strict-vs-fallback) and the safe :func:`render_prompt` substitution (allowlist + literal braces).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from wastech_orchestrator.config.loader import ConfigError
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
    tdir = tmp_path / "prompts"
    tdir.mkdir()
    for name, body in files.items():
        (tdir / name).write_text(body, encoding="utf-8")
    return tdir


def test_append_combines_default_then_override(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path, **{"implementation.md": "EXTRA RULES"})
    store = PromptTemplateStore(
        PromptsConfig(
            templates_dir=str(tdir),
            mode=PromptMode.APPEND,
            overrides=((Stage.IMPLEMENTATION, "implementation.md"),),
        )
    )
    resolved = store.resolved(Stage.IMPLEMENTATION)
    assert resolved == f"{_packaged_text(Stage.IMPLEMENTATION)}\n\nEXTRA RULES"
    # Stages without an override are untouched.
    assert store.resolved(Stage.REVIEW) == _packaged_text(Stage.REVIEW)


def test_replace_uses_override_only(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path, **{"review.md": "ONLY THIS RUBRIC"})
    store = PromptTemplateStore(
        PromptsConfig(
            templates_dir=str(tdir),
            mode=PromptMode.REPLACE,
            overrides=((Stage.REVIEW, "review.md"),),
        )
    )
    assert store.resolved(Stage.REVIEW) == "ONLY THIS RUBRIC"
    # A stage without an override still falls back to the packaged default, even in replace mode.
    assert store.resolved(Stage.IMPLEMENTATION) == _packaged_text(Stage.IMPLEMENTATION)


def test_missing_override_strict_raises(tmp_path: Path) -> None:
    tdir = _override_dir(tmp_path)  # empty
    with pytest.raises(ConfigError) as exc:
        PromptTemplateStore(
            PromptsConfig(
                templates_dir=str(tdir),
                strict=True,
                overrides=((Stage.IMPLEMENTATION, "implementation.md"),),
            )
        )
    assert any("implementation" in issue for issue in exc.value.issues)


def test_missing_override_non_strict_falls_back_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tdir = _override_dir(tmp_path)  # empty
    # The library logger ("wastech_orchestrator") sets propagate=False, so pass a propagating
    # logger to make the warning visible to caplog (which captures on the root logger).
    test_logger = logging.getLogger("test.prompts.fallback")
    with caplog.at_level(logging.WARNING):
        store = PromptTemplateStore(
            PromptsConfig(
                templates_dir=str(tdir),
                strict=False,
                overrides=((Stage.IMPLEMENTATION, "implementation.md"),),
            ),
            logger=test_logger,
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
