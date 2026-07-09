"""Behavior of the delivered ``check_journey`` prose gate (packaged/tools/check_journey).

Runs the shipped script out-of-process through ``sys.executable`` (so the test is deterministic and
OS-independent — it never relies on the ``+x`` bit or the shebang) with a crafted stdin payload, and
asserts each violation type per mode, a clean pass, and the diff/task scope resolution incl. the
vacuous pass. The script is located via ``importlib.resources`` so it works from a source tree or a
wheel, exactly as the runtime resolves it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from pathlib import Path

_CLEAN_RU = """\
<!-- Language: RU -->
## Purpose

Зачем эта часть существует и какую роль играет в путешествии.

## Emotional point

Что должно остаться у читателя после прочтения.

---

# Pages RU

## Part 1: Начало пути

### 1.1 Первый блок

#### 1.1.1 Первая страница

Спокойный вводный абзац, который вводит читателя в тему без лишнего шума и обещаний.

### 1.2 Второй блок

#### 1.2.1 Вторая страница

Ещё один абзац, продолжающий мысль и мягко подводящий к следующему шагу.

## Notes

- рабочая заметка
"""


def _script() -> resources.abc.Traversable:
    return resources.files("wastech_orchestrator").joinpath("packaged", "tools", "check_journey")


def _run(repo: Path, *, diff: Path | None, mode: str, task: Path | None = None) -> tuple[int, dict]:
    payload = {
        "task_id": "t",
        "node_id": "constraints",
        "subtask_order": None,
        "paths": {
            "repo": str(repo),
            "diff_path": str(diff) if diff is not None else None,
            "task_path": str(task) if task is not None else None,
        },
        "args": {"mode": mode},
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


def _chapter_repo(tmp_path: Path, content: str, rel: str = "chapters/part1_ru.md") -> Path:
    repo = tmp_path / "repo"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")
    return repo


def _diff_for(tmp_path: Path, rel: str = "chapters/part1_ru.md") -> Path:
    diff = tmp_path / "current.diff"
    diff.write_text(f"diff --git a/{rel} b/{rel}\n+++ b/{rel}\n", encoding="utf-8")
    return diff


def _violations(out: dict) -> list[str]:
    return [v for file_issues in out["data"]["violations"].values() for v in file_issues]


# -- clean + scope ------------------------------------------------------------


def test_clean_ru_chapter_passes(tmp_path: Path) -> None:
    repo = _chapter_repo(tmp_path, _CLEAN_RU)
    code, out = _run(repo, diff=_diff_for(tmp_path), mode="ru")
    assert code == 0
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == ["chapters/part1_ru.md"]


def test_no_diff_and_no_task_is_vacuous_pass(tmp_path: Path) -> None:
    repo = _chapter_repo(tmp_path, _CLEAN_RU)
    code, out = _run(repo, diff=None, mode="ru")
    assert code == 0
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == []


def test_task_path_fallback_when_no_diff(tmp_path: Path) -> None:
    repo = _chapter_repo(tmp_path, _CLEAN_RU)
    task = tmp_path / "task.md"
    task.write_text("Edit chapters/part1_ru.md for tone.", encoding="utf-8")
    code, out = _run(repo, diff=None, mode="ru", task=task)
    assert out["outcome"] == "pass"
    assert out["data"]["checked"] == ["chapters/part1_ru.md"]


# -- RU structural violations -------------------------------------------------


def test_missing_purpose_fails(tmp_path: Path) -> None:
    broken = _CLEAN_RU.replace(
        "## Purpose\n\nЗачем эта часть существует и какую роль играет в путешествии.\n\n", ""
    )
    repo = _chapter_repo(tmp_path, broken)
    code, out = _run(repo, diff=_diff_for(tmp_path), mode="ru")
    assert code == 1 and out["outcome"] == "fail"
    assert any("## Purpose" in v for v in _violations(out))


def test_skipped_heading_level_fails(tmp_path: Path) -> None:
    broken = _CLEAN_RU.replace("### 1.1 Первый блок\n\n", "")  # ## Part -> #### with no ###
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path), mode="ru")
    assert out["outcome"] == "fail"
    assert any("hierarchy" in v for v in _violations(out))


def test_service_label_heading_fails(tmp_path: Path) -> None:
    broken = _CLEAN_RU.replace("### 1.1 Первый блок", "### 1.1 Философия")
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path), mode="ru")
    assert out["outcome"] == "fail"
    assert any("service-label" in v for v in _violations(out))


def test_ai_antithesis_pattern_fails(tmp_path: Path) -> None:
    broken = _CLEAN_RU.replace(
        "Спокойный вводный абзац, который вводит читателя в тему без лишнего шума и обещаний.",
        "Это не просто список функций, а честный разговор о времени.",
    )
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path), mode="ru")
    assert out["outcome"] == "fail"
    assert any("antithesis" in v for v in _violations(out))


def test_extra_title_in_page_fails(tmp_path: Path) -> None:
    broken = _CLEAN_RU.replace(
        "#### 1.1.1 Первая страница\n",
        "#### 1.1.1 Первая страница\n\n##### Лишний заголовок\n",
    )
    repo = _chapter_repo(tmp_path, broken)
    _, out = _run(repo, diff=_diff_for(tmp_path), mode="ru")
    assert out["outcome"] == "fail"
    assert any("extra title" in v for v in _violations(out))


# -- EN length / paragraph rules (not applied in RU mode) ---------------------


def test_en_page_over_hard_maximum_fails(tmp_path: Path) -> None:
    long_para = "Слово " * 200  # ~1200 chars, one paragraph
    content = _CLEAN_RU.replace(
        "Спокойный вводный абзац, который вводит читателя в тему без лишнего шума и обещаний.",
        long_para.strip(),
    )
    repo = _chapter_repo(tmp_path, content, rel="chapters/part1_en.md")
    _, out = _run(repo, diff=_diff_for(tmp_path, "chapters/part1_en.md"), mode="en")
    assert out["outcome"] == "fail"
    assert any("hard maximum" in v for v in _violations(out))


def test_en_page_too_many_paragraphs_fails(tmp_path: Path) -> None:
    # A page well inside the length band but with four paragraphs.
    para = "Ровный абзац умеренной длины, чтобы страница читалась спокойно и ровно. "
    four = "\n\n".join([para] * 4)
    content = _CLEAN_RU.replace(
        "Спокойный вводный абзац, который вводит читателя в тему без лишнего шума и обещаний.",
        four,
    )
    repo = _chapter_repo(tmp_path, content, rel="chapters/part1_en.md")
    _, out = _run(repo, diff=_diff_for(tmp_path, "chapters/part1_en.md"), mode="en")
    assert out["outcome"] == "fail"
    assert any("paragraphs" in v for v in _violations(out))


def test_ru_mode_does_not_apply_length_limit(tmp_path: Path) -> None:
    # A short RU page passes in ru mode (RU is intentionally not length-gated) but the same content
    # fails in en mode — proving the mode split.
    repo = _chapter_repo(tmp_path, _CLEAN_RU)
    _, ru = _run(repo, diff=_diff_for(tmp_path), mode="ru")
    assert ru["outcome"] == "pass"
    repo_en = _chapter_repo(tmp_path, _CLEAN_RU, rel="chapters/part1_en.md")
    _, en = _run(repo_en, diff=_diff_for(tmp_path, "chapters/part1_en.md"), mode="en")
    assert en["outcome"] == "fail"
    assert any("minimum" in v for v in _violations(en))
