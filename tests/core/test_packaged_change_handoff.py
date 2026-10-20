"""The packaged role prompts use the change hand-off: overview, base and full diff.

A review judges what changed and starts from the overview; a fix starts from the findings and uses
the overview only to locate files; documentation picks the paths it covers from the overview; a
content critic that judges a revision as a whole says it reads the whole change, so the footer's
where-to-start sentence cannot steer it away from the full diff.
"""

from __future__ import annotations

import pytest
from tests.conftest import BUILTIN_FLOWS_DIR

from wastech_orchestrator.core.flow.prompt import render_role_prompt
from wastech_orchestrator.diff_overview import LAST_COMMIT_TITLE

_SHA = "3f9c2a7e1d4b5a6c8e0f1a2b3c4d5e6f7a8b9c0d"
_EDITED = {
    "task_path": "/x/task.md",
    "plan_path": "/x/plan.md",
    "diff_path": "/x/current.diff",
    "change_overview_path": "/x/change-overview.txt",
    "diff_base": _SHA,
}
_BEFORE_ANY_EDIT = {"task_path": "/x/task.md", "plan_path": "/x/plan.md"}


def _render(role_file: str, variables: dict[str, object | None]) -> str:
    return render_role_prompt(BUILTIN_FLOWS_DIR, role_file, variables)


def test_the_review_starts_from_the_overview_and_accounts_for_every_path() -> None:
    rendered = _render("implementation/review.md", _EDITED)
    assert rendered.startswith(
        "Review this task's change against the task and plan. Start from the change overview and "
        "account for every path it lists."
    )
    # The cumulative-diff rule still stands.
    assert "The diff may be cumulative" in rendered
    # Before any edit there is no overview, and no dangling clause about it.
    assert "change overview" not in _render("implementation/review.md", _BEFORE_ANY_EDIT)


def test_a_subtask_review_judges_the_since_last_commit_section() -> None:
    # The footer's own sentence names the section by its full title inside a decomposed run; the
    # prompt only has to say that it is the one this review judges.
    subtask = {**_EDITED, "subtask_order": 2, "subtask_count": 3, "subtask_spec_path": "/x/s.md"}
    rendered = _render("implementation/review.md", subtask)
    assert (
        "Its change is the overview's since-last-commit section, if any, else the whole overview."
        in rendered
    )
    assert "since-last-commit" not in _render("implementation/review.md", _EDITED)
    assert "last commit" in LAST_COMMIT_TITLE


@pytest.mark.parametrize(
    "role_file", ["implementation/fixing.md", "implementation/fixing.continue.md"]
)
def test_a_fix_starts_from_the_findings_and_uses_the_overview_to_locate_files(
    role_file: str,
) -> None:
    rendered = _render(role_file, _EDITED)
    assert "findings" in rendered and "check log" in rendered
    assert "only to locate the files" in rendered
    assert "open the full diff only when a finding cannot be placed without it" in rendered


def test_the_first_fix_round_names_the_overview_only_when_there_is_one() -> None:
    assert "change overview at /x/change-overview.txt only" in _render(
        "implementation/fixing.md", _EDITED
    )
    without = _render("implementation/fixing.md", _BEFORE_ANY_EDIT)
    assert "Use the change overview only to locate" in without


def test_documentation_works_from_the_overview_and_keeps_the_full_diff() -> None:
    rendered = _render("implementation/documentation.md", _EDITED)
    assert "The change overview at /x/change-overview.txt lists every changed path" in rendered
    assert "decide which of those paths your documentation covers" in rendered
    assert "The full diff, at /x/current.diff, holds all of it." in rendered
    assert "change overview" not in _render("implementation/documentation.md", _BEFORE_ANY_EDIT)


@pytest.mark.parametrize(
    "role_file",
    [
        "blog_article/tone_style.md",
        "blog_article_revise/tone_style.md",
        "content_chapter/story_critic.md",
        "content_translate/en_critic.md",
    ],
)
def test_a_critic_of_the_whole_revision_says_it_reads_the_whole_change(role_file: str) -> None:
    raw = (BUILTIN_FLOWS_DIR / role_file).read_text(encoding="utf-8")
    assert "{diff_path}" in raw
    rendered = _render(role_file, _EDITED)
    assert "the whole" in rendered and "/x/current.diff" in rendered
    assert "the change overview only tells you how large it is" in rendered
