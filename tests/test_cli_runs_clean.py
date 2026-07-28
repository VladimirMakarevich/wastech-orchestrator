"""`worc runs clean`: the manual half of run retention (the mode an operator turns on to analyze).

Same shape and same active-task guard as `logs clean`; the difference is the quarantine opt-in and
that `--keep N` counts *tasks*, not directories.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.runtime_layout import runs_root

_ROOTS = ("control-bundles", "instruction-bundles", "exchange-seals")


@pytest.fixture(autouse=True)
def _idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "has_active_task", lambda _config: False)


def _config(local_path: Path) -> OrchestratorConfig:
    text = (
        "repo:\n"
        '  url: "git@example.com:o/r.git"\n'
        f'  local_path: "{local_path.as_posix()}"\n'
        "agents:\n"
        "  allowed: [codex]\n"
        "  providers:\n"
        "    codex:\n"
        "      command: codex\n"
    )
    return loads_config(text).config


def _seed(local_path: Path, task_ids: list[str], *, quarantine: bool = False) -> Path:
    """Seed ``.worc/runs/`` with per-task state, oldest task first by mtime."""
    parent = runs_root(local_path / ".worc")
    roots = (*_ROOTS, "exchange-quarantine") if quarantine else _ROOTS
    for i, task_id in enumerate(task_ids):
        for root in roots:
            task_dir = parent / root / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
            stamp = 1_700_000_000 + i * 100
            os.utime(task_dir, (stamp, stamp))
    return parent


def _ns(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {"keep": None, "include_quarantine": False, "yes": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_bare_clean_removes_every_task(tmp_path: Path) -> None:
    parent = _seed(tmp_path, ["t1", "t2"])
    assert cli._cmd_runs_clean(_ns(yes=True), _config(tmp_path)) == 0
    for root in _ROOTS:
        assert list((parent / root).iterdir()) == []


def test_keep_n_retains_the_newest_tasks_across_all_roots(tmp_path: Path) -> None:
    parent = _seed(tmp_path, ["t1", "t2", "t3"])
    assert cli._cmd_runs_clean(_ns(keep=1), _config(tmp_path)) == 0
    for root in _ROOTS:
        assert {p.name for p in (parent / root).iterdir()} == {"t3"}


def test_quarantine_survives_and_is_named_in_the_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = _seed(tmp_path, ["t1"], quarantine=True)
    assert cli._cmd_runs_clean(_ns(yes=True), _config(tmp_path)) == 0
    assert (parent / "exchange-quarantine" / "t1").is_dir()
    assert "--include-quarantine" in capsys.readouterr().out


def test_include_quarantine_takes_the_evidence_too(tmp_path: Path) -> None:
    parent = _seed(tmp_path, ["t1"], quarantine=True)
    assert cli._cmd_runs_clean(_ns(yes=True, include_quarantine=True), _config(tmp_path)) == 0
    assert not (parent / "exchange-quarantine" / "t1").exists()


def test_include_quarantine_confirms_even_under_a_bounded_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --keep N>0 skips the prompt because pruning caches is routine. Deleting tainted-exchange
    # evidence is not, so the opt-in re-arms the confirmation at any scope.
    parent = _seed(tmp_path, ["t1", "t2"], quarantine=True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert cli._cmd_runs_clean(_ns(keep=1, include_quarantine=True), _config(tmp_path)) == 0
    assert (parent / "exchange-quarantine" / "t1").is_dir()  # declined → nothing removed
    assert (parent / "control-bundles" / "t1").is_dir()


def test_bare_clean_aborts_when_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = _seed(tmp_path, ["t1"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert cli._cmd_runs_clean(_ns(), _config(tmp_path)) == 0
    assert "aborted" in capsys.readouterr().out
    assert (parent / "exchange-seals" / "t1").is_dir()


def test_active_task_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = _seed(tmp_path, ["t1"])
    monkeypatch.setattr(cli, "has_active_task", lambda _config: True)
    assert cli._cmd_runs_clean(_ns(yes=True), _config(tmp_path)) == 1
    assert "a task is active" in capsys.readouterr().out
    assert (parent / "control-bundles" / "t1").is_dir()


def test_negative_keep_is_rejected(tmp_path: Path) -> None:
    _seed(tmp_path, ["t1"])
    assert cli._cmd_runs_clean(_ns(keep=-1), _config(tmp_path)) == 2


def test_nothing_to_remove_when_runs_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._cmd_runs_clean(_ns(), _config(tmp_path)) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_quarantine_only_state_reports_how_to_reach_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The routine sweep finds nothing, but there IS evidence sitting there — say so, or the operator
    # reads "nothing to remove" beside a directory that is still on disk.
    parent = runs_root(tmp_path / ".worc") / "exchange-quarantine" / "tainted"
    parent.mkdir(parents=True)
    assert cli._cmd_runs_clean(_ns(), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "nothing to remove" in out
    assert "--include-quarantine" in out
