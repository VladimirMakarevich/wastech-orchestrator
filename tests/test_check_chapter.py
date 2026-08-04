"""Behavior of the delivered ``check_chapter`` prose gate (packaged/tools/check_chapter).

Runs the shipped script out-of-process through ``sys.executable`` (so the test is deterministic and
OS-independent — it never relies on the ``+x`` bit or the shebang) with a crafted stdin payload, and
asserts each structural violation type, the opt-in length/paragraph rules, a clean pass, and the
diff/task scope resolution incl. the vacuous pass. The script is located via ``importlib.resources``
so it works from a source tree or a wheel, exactly as the runtime resolves it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

_CLEAN = """\
## Purpose

Why this part exists and the role it plays in the whole.

## Emotional point

What should stay with the reader afterwards.

---

# Pages

## Part 1: The beginning

### 1.1 First block

#### 1.1.1 First page

A calm opening paragraph that eases the reader into the topic without noise or promises.

### 1.2 Second block

#### 1.2.1 Second page

Another paragraph that carries the thought forward and gently leads to the next step.

## Notes

- working note
"""


def _script() -> resources.abc.Traversable:
    return resources.files("wastech_orchestrator").joinpath("packaged", "tools", "check_chapter")


def _run(
    repo: Path,
    *,
    diff: Path | None,
    args: dict | None = None,
    task: Path | None = None,
    child_encoding: str | None = None,
) -> tuple[int, dict]:
    payload = {
        "task_id": "t",
        "node_id": "constraints",
        "subtask_order": None,
        "paths": {
            "repo": str(repo),
            "diff_path": str(diff) if diff is not None else None,
            "task_path": str(task) if task is not None else None,
        },
        "args": args or {},
    }
    env = None
    if child_encoding is not None:
        env = {**os.environ, "PYTHONIOENCODING": child_encoding}
    with resources.as_file(_script()) as path:
        proc = subprocess.run(
            [sys.executable, str(path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=env,
        )
    return proc.returncode, json.loads(proc.stdout)


def _chapter_repo(tmp_path: Path, content: str, rel: str = "chapters/part1.md") -> Path:
    repo = tmp_path / "repo"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")
    return repo


def _diff_for(tmp_path: Path, rel: str = "chapters/part1.md") -> Path:
    diff = tmp_path / "current.diff"
    diff.write_text(f"diff --git a/{rel} b/{rel}\n+++ b/{rel}\n", encoding="utf-8")
    return diff


def _violations(out: dict) -> list[str]:
    return [v for file_issues in out["data"]["violations"].values() for v in file_issues]


# -- clean + scope ------------------------------------------------------------


def test_clean_chapter_passes(tmp_path: Path) -> None:
    repo = _chapter_repo(tmp_path, _CLEAN)
    code, out = _run(repo, diff=_diff_for(tmp_path))
    assert code == 0
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == ["chapters/part1.md"]


def test_no_diff_and_no_task_is_vacuous_pass(tmp_path: Path) -> None:
    repo = _chapter_repo(tmp_path, _CLEAN)
    code, out = _run(repo, diff=None)
    assert code == 0
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == []


def test_task_path_fallback_when_no_diff(tmp_path: Path) -> None:
    repo = _chapter_repo(tmp_path, _CLEAN)
    task = tmp_path / "task.md"
    task.write_text("Edit chapters/part1.md for tone.", encoding="utf-8")
    code, out = _run(repo, diff=None, task=task)
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == ["chapters/part1.md"]


# -- structural violations (always enforced) ----------------------------------


def test_missing_purpose_fails(tmp_path: Path) -> None:
    broken = _CLEAN.replace(
        "## Purpose\n\nWhy this part exists and the role it plays in the whole.\n\n", ""
    )
    repo = _chapter_repo(tmp_path, broken)
    code, out = _run(repo, diff=_diff_for(tmp_path))
    assert code == 1 and out["outcome"] == "fail"
    assert any("## Purpose" in v for v in _violations(out))


def test_skipped_heading_level_fails(tmp_path: Path) -> None:
    broken = _CLEAN.replace("### 1.1 First block\n\n", "")  # ## Part -> #### with no ###
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path))
    assert out["outcome"] == "fail"
    assert any("hierarchy" in v for v in _violations(out))


def test_service_label_heading_fails(tmp_path: Path) -> None:
    broken = _CLEAN.replace("### 1.1 First block", "### 1.1 Philosophy")
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path))
    assert out["outcome"] == "fail"
    assert any("service-label" in v for v in _violations(out))


def test_ai_antithesis_pattern_fails(tmp_path: Path) -> None:
    broken = _CLEAN.replace(
        "A calm opening paragraph that eases the reader into the topic without noise or promises.",
        "This is not just a list of features, but an honest conversation about time.",
    )
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path))
    assert out["outcome"] == "fail"
    assert any("antithesis" in v for v in _violations(out))


def test_extra_title_in_page_fails(tmp_path: Path) -> None:
    broken = _CLEAN.replace(
        "#### 1.1.1 First page\n",
        "#### 1.1.1 First page\n\n##### Extra heading\n",
    )
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path))
    assert out["outcome"] == "fail"
    assert any("extra title" in v for v in _violations(out))


# -- opt-in length / paragraph rules (only when `args` ask for them) -----------


def test_page_over_max_chars_fails(tmp_path: Path) -> None:
    long_para = "word " * 200  # ~1000 chars, one paragraph
    content = _CLEAN.replace(
        "A calm opening paragraph that eases the reader into the topic without noise or promises.",
        long_para.strip(),
    )
    repo = _chapter_repo(tmp_path, content)
    _, out = _run(repo, diff=_diff_for(tmp_path), args={"max_chars": 800})
    assert out["outcome"] == "fail"
    assert any("hard maximum" in v for v in _violations(out))


def test_page_below_min_chars_fails(tmp_path: Path) -> None:
    # The clean pages are short (~85 chars) — a 500-char floor fails them.
    repo = _chapter_repo(tmp_path, _CLEAN)
    _, out = _run(repo, diff=_diff_for(tmp_path), args={"min_chars": 500})
    assert out["outcome"] == "fail"
    assert any("minimum" in v for v in _violations(out))


def test_page_over_max_paragraphs_fails(tmp_path: Path) -> None:
    para = "An even paragraph of moderate length so the page reads calmly. "
    four = "\n\n".join([para.strip()] * 4)
    content = _CLEAN.replace(
        "A calm opening paragraph that eases the reader into the topic without noise or promises.",
        four,
    )
    repo = _chapter_repo(tmp_path, content)
    _, out = _run(repo, diff=_diff_for(tmp_path), args={"max_paragraphs": 3})
    assert out["outcome"] == "fail"
    assert any("paragraphs" in v for v in _violations(out))


# -- cross-platform stdout encoding -------------------------------------------


def test_reports_non_ascii_violations_under_a_legacy_child_encoding(tmp_path: Path) -> None:
    # A Windows host hands a piped child `cp1252`, which encodes neither `≤` (extra title) nor `→`
    # (hierarchy). The script used to die on `print` and the tool node saw an empty stdout instead
    # of the violation — a fail read as a launch error. The seam is forced here so the regression is
    # caught on every host, not only on the Windows runner.
    cases = (
        (
            _CLEAN.replace(
                "#### 1.1.1 First page\n", "#### 1.1.1 First page\n\n##### Extra heading\n"
            ),
            "≤1 Title per page",
        ),
        (_CLEAN.replace("### 1.1 First block\n\n", ""), "hierarchy must be ## → ### → ####"),
    )
    for index, (broken, expected) in enumerate(cases):
        repo = _chapter_repo(tmp_path / f"case{index}", broken)
        code, out = _run(repo, diff=_diff_for(tmp_path / f"case{index}"), child_encoding="cp1252")
        assert code == 1  # it reached the end and reported, rather than dying on the encode
        assert out["outcome"] == "fail"
        assert any(expected in v for v in _violations(out))


def test_length_rules_are_opt_in(tmp_path: Path) -> None:
    # With no length `args` the short clean chapter passes (structure only); the same content fails
    # the moment a min_chars floor is asked for — proving the length rules are opt-in per flow.
    repo = _chapter_repo(tmp_path, _CLEAN)
    _, without = _run(repo, diff=_diff_for(tmp_path), args={})
    assert without["outcome"] == "pass"
    _, with_floor = _run(repo, diff=_diff_for(tmp_path), args={"min_chars": 500})
    assert with_floor["outcome"] == "fail"
    assert any("minimum" in v for v in _violations(with_floor))
