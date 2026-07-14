"""Staged task-file creation: the ``tasks/preparing/`` folder, the atomic ``promote`` move (core +
CLI), the atomic ``enqueue`` copy, and the scanner's blindness to the staging folder."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.schema import OrchestratorConfig

_ConfigFactory = Callable[..., OrchestratorConfig]

_ROOT = "---\nid: {id}\ntitle: T\n---\n## Description\n\nbody\n"


def _stage(config: OrchestratorConfig, name: str, text: str) -> Path:
    prep = cli.preparing_dir(config)
    prep.mkdir(parents=True, exist_ok=True)
    path = prep / name
    path.write_text(text, encoding="utf-8")
    return path


# --- path helper + install layout -------------------------------------------------------


def test_preparing_dir_is_sibling_of_pending(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    assert cli.preparing_dir(config) == cli.pending_dir(config).parent / "preparing"
    assert "tasks/preparing" in cli.REPO_TASK_DIRS


# --- scanner blindness ------------------------------------------------------------------


def test_scanner_never_sees_the_preparing_folder(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    _stage(config, "draft.md", _ROOT.format(id="draft"))
    # The daemon only ever scans pending_dir; a file staged in preparing/ is invisible there.
    assert cli.select_pending(cli.pending_dir(config)) == []
    assert cli.scan_pending_sorted(cli.pending_dir(config), "default") == []


# --- promote_tasks: single file ---------------------------------------------------------


def test_promote_single_file_moves_into_pending(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    _stage(config, "t1.md", _ROOT.format(id="t1"))
    moved, errors = cli.promote_tasks(config, target="t1")
    assert moved == ["t1.md"]
    assert errors == []
    assert (cli.pending_dir(config) / "t1.md").is_file()
    assert not (cli.preparing_dir(config) / "t1.md").exists()


def test_promote_unknown_target_reports_error(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    cli.preparing_dir(config).mkdir(parents=True)
    moved, errors = cli.promote_tasks(config, target="nope")
    assert moved == []
    assert any("not a staged file" in e for e in errors)


def test_promote_refuses_to_clobber_a_queued_task(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    _stage(config, "t1.md", _ROOT.format(id="t1"))
    pending = cli.pending_dir(config)
    pending.mkdir(parents=True)
    (pending / "t1.md").write_text("already queued\n", encoding="utf-8")
    moved, errors = cli.promote_tasks(config, target="t1")
    assert moved == []
    assert any("already in pending" in e for e in errors)
    # The queued file is untouched and the staged draft is left in place, not lost.
    assert (pending / "t1.md").read_text(encoding="utf-8") == "already queued\n"
    assert (cli.preparing_dir(config) / "t1.md").is_file()


# --- promote_tasks: decomposition batch -------------------------------------------------


def _stage_deco(config: OrchestratorConfig) -> None:
    root = (
        "---\nid: epic\ntitle: Epic\n"
        "subtasks:\n  - subtasks/01-a.md\n  - subtasks/02-b.md\n---\n## Description\n\nx\n"
    )
    _stage(config, "epic.md", root)
    subs = cli.preparing_dir(config) / "subtasks"
    subs.mkdir(parents=True, exist_ok=True)
    (subs / "01-a.md").write_text(
        "---\ntitle: A\n---\n## Acceptance criteria\n\n- [ ] a\n", "utf-8"
    )
    (subs / "02-b.md").write_text(
        "---\ntitle: B\n---\n## Acceptance criteria\n\n- [ ] b\n", "utf-8"
    )


def test_promote_deco_root_pulls_its_subtasks(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    _stage_deco(config)
    moved, errors = cli.promote_tasks(config, target="epic")
    assert errors == []
    pending = cli.pending_dir(config)
    assert (pending / "epic.md").is_file()
    assert (pending / "subtasks" / "01-a.md").is_file()
    assert (pending / "subtasks" / "02-b.md").is_file()
    # Nothing left staged, and the root is only reported after its specs (specs move first).
    assert cli.select_pending(cli.preparing_dir(config)) == []
    assert moved[-1] == "epic.md"


def test_promote_all_moves_toplevel_and_subtasks(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    _stage_deco(config)
    _stage(config, "solo.md", _ROOT.format(id="solo"))
    moved, errors = cli.promote_tasks(config, all_files=True)
    assert errors == []
    pending = cli.pending_dir(config)
    assert {p.name for p in cli.select_pending(pending)} == {"epic.md", "solo.md"}
    assert (pending / "subtasks" / "01-a.md").is_file()
    assert (pending / "subtasks" / "02-b.md").is_file()
    assert cli.select_pending(cli.preparing_dir(config)) == []


def test_promote_all_empty_reports_nothing_staged(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    cli.preparing_dir(config).mkdir(parents=True)
    moved, errors = cli.promote_tasks(config, all_files=True)
    assert moved == []
    assert any("nothing staged" in e for e in errors)


def test_read_subtask_refs_drops_traversal(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    bad = (
        "---\nid: epic\ntitle: Epic\n"
        "subtasks:\n  - ../escape.md\n  - subtasks/01-ok.md\n---\n## Description\n\nx\n"
    )
    path = _stage(config, "epic.md", bad)
    assert cli._read_subtask_refs(path) == ["subtasks/01-ok.md"]


# --- enqueue atomic copy ----------------------------------------------------------------


def test_atomic_copy_lands_file_without_scan_candidate_temp(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    src = tmp_path / "task.md"
    src.write_text(_ROOT.format(id="t1"), encoding="utf-8")
    pending = cli.pending_dir(config)
    cli._atomic_copy(src, pending / "task.md")
    assert (pending / "task.md").read_text(encoding="utf-8") == _ROOT.format(id="t1")
    # No temp left behind, and none of the temp names would ever be a scan candidate (.md/.json).
    assert cli.select_pending(pending) == [pending / "task.md"]
    assert list(pending.glob("*.tmp")) == []


# --- cmd_promote (CLI wrapper) ----------------------------------------------------------


def test_cmd_promote_moves_and_reports(
    monkeypatch: pytest.MonkeyPatch,
    make_git_config: _ConfigFactory,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_git_config(tmp_path / "clone")
    _stage(config, "t1.md", _ROOT.format(id="t1"))
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    monkeypatch.setattr(cli, "_configure_runtime_logging", lambda _args: None)
    args = argparse.Namespace(target="t1", all_files=False)
    rc = cli.cmd_promote(args)
    assert rc == 0
    assert (cli.pending_dir(config) / "t1.md").is_file()
    assert "t1.md -> pending" in capsys.readouterr().out


def test_cmd_promote_returns_1_on_error(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    cli.preparing_dir(config).mkdir(parents=True)
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    monkeypatch.setattr(cli, "_configure_runtime_logging", lambda _args: None)
    args = argparse.Namespace(target="ghost", all_files=False)
    assert cli.cmd_promote(args) == 1
