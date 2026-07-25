"""Behavior of the delivered ``check_length`` size-floor gate (packaged/tools/check_length).

Runs the shipped script out-of-process through ``sys.executable`` (so the test is deterministic and
OS-independent — it never relies on the ``+x`` bit or the shebang) with a crafted stdin payload, and
asserts the char/paragraph floors, the heading-exclusion, and the diff-only scope resolution
(deliberately no task-text fallback — see the script's module docstring, finding F3) incl. the
vacuous pass. The script is located via
``importlib.resources`` so it works from a source tree or a wheel, exactly as the runtime resolves
it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from pathlib import Path

_LONG_DOC = """\
# Title

## Section

This is a comfortably long first paragraph that easily clears any reasonable minimum character
floor on its own, since it is written out in full sentences rather than a short stub.

This is a second paragraph, distinct from the first, so the document also clears a minimum
paragraph-count floor without relying on the heading text to pad it out.
"""

_SHORT_DOC = """\
# Title

Too short.
"""


def _script() -> resources.abc.Traversable:
    return resources.files("wastech_orchestrator").joinpath("packaged", "tools", "check_length")


def _run(
    repo: Path,
    *,
    diff: Path | None,
    args: dict[str, object] | None = None,
    task: Path | None = None,
) -> tuple[int, dict]:
    payload = {
        "task_id": "t",
        "node_id": "gate",
        "subtask_order": None,
        "paths": {
            "repo": str(repo),
            "diff_path": str(diff) if diff is not None else None,
            "task_path": str(task) if task is not None else None,
        },
        "args": args or {},
    }
    with resources.as_file(_script()) as path:
        proc = subprocess.run(
            [sys.executable, str(path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )
    return proc.returncode, json.loads(proc.stdout)


def _doc_repo(tmp_path: Path, content: str, rel: str = "content/article.md") -> Path:
    repo = tmp_path / "repo"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")
    return repo


def _diff_for(tmp_path: Path, rel: str = "content/article.md") -> Path:
    diff = tmp_path / "current.diff"
    diff.write_text(f"diff --git a/{rel} b/{rel}\n+++ b/{rel}\n", encoding="utf-8")
    return diff


def _violations(out: dict) -> list[str]:
    return [v for file_issues in out["data"]["violations"].values() for v in file_issues]


# -- clean + scope ------------------------------------------------------------


def test_long_document_passes_both_floors(tmp_path: Path) -> None:
    repo = _doc_repo(tmp_path, _LONG_DOC)
    code, out = _run(repo, diff=_diff_for(tmp_path), args={"min_chars": 100, "min_paragraphs": 2})
    assert code == 0
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == ["content/article.md"]


def test_no_diff_and_no_task_is_vacuous_pass(tmp_path: Path) -> None:
    repo = _doc_repo(tmp_path, _LONG_DOC)
    code, out = _run(repo, diff=None, args={"min_chars": 100})
    assert code == 0
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == []


def test_task_text_never_falls_back_when_diff_is_absent(tmp_path: Path) -> None:
    # Unlike check_chapter, a task body naming an unrelated .md (a rules/reference doc, say) must
    # NOT be picked up as the checked document when there is no diff — see finding F3: this exact
    # fallback once made the gate measure reference docs instead of the actual deliverable.
    repo = _doc_repo(tmp_path, _LONG_DOC)
    task = tmp_path / "task.md"
    task.write_text("Write content/article.md for the release.", encoding="utf-8")
    code, out = _run(repo, diff=None, args={"min_chars": 100}, task=task)
    assert code == 0
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == []


# -- floors ---------------------------------------------------------------


def test_below_min_chars_fails(tmp_path: Path) -> None:
    repo = _doc_repo(tmp_path, _SHORT_DOC)
    code, out = _run(repo, diff=_diff_for(tmp_path), args={"min_chars": 500})
    assert code == 1
    assert out["outcome"] == "fail"
    violations = _violations(out)
    assert any("chars" in v and "minimum" in v for v in violations)


def test_below_min_paragraphs_fails(tmp_path: Path) -> None:
    repo = _doc_repo(tmp_path, _SHORT_DOC)
    code, out = _run(repo, diff=_diff_for(tmp_path), args={"min_paragraphs": 3})
    assert code == 1
    assert out["outcome"] == "fail"
    violations = _violations(out)
    assert any("paragraphs" in v and "minimum" in v for v in violations)


def test_omitted_floors_never_fail_on_size(tmp_path: Path) -> None:
    repo = _doc_repo(tmp_path, _SHORT_DOC)
    code, out = _run(repo, diff=_diff_for(tmp_path), args={})
    assert code == 0
    assert out["outcome"] == "pass"


def test_zero_floors_never_fail_on_size(tmp_path: Path) -> None:
    repo = _doc_repo(tmp_path, _SHORT_DOC)
    code, out = _run(repo, diff=_diff_for(tmp_path), args={"min_chars": 0, "min_paragraphs": 0})
    assert code == 0
    assert out["outcome"] == "pass"


# -- heading exclusion ------------------------------------------------------


def test_headings_excluded_from_char_and_paragraph_counts(tmp_path: Path) -> None:
    doc = (
        "# Big Title That Is Actually Quite Long For A Heading\n\n"
        "## Another Fairly Long Subheading Here\n\n"
        "###### And A Sixth Level Heading Too\n\n"
        "One short paragraph.\n"
    )
    repo = _doc_repo(tmp_path, doc)
    # The heading text alone is well over 40 chars; only the one prose paragraph should count.
    code, out = _run(repo, diff=_diff_for(tmp_path), args={"min_chars": 40})
    assert code == 1
    assert out["outcome"] == "fail"
    violations = _violations(out)
    assert any(v.startswith("document is 20 chars") for v in violations)
