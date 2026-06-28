"""`worc logs clean`: prune task dirs, preserve the ledger (unless --all), confirm gates."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig


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


def _seed_logs(local_path: Path, task_ids: list[str], *, ledger: bool = True) -> Path:
    """Create ``<local>/.worc/logs/`` with one dir per task id + an optional ledger.

    Returns the logs root. Task dirs are stamped with increasing mtimes so ``task_ids[-1]`` is the
    newest (``--keep`` retains the most recently modified).
    """
    logs_root = local_path / ".worc" / "logs"
    logs_root.mkdir(parents=True)
    for i, task_id in enumerate(task_ids):
        d = logs_root / task_id
        (d / "stages").mkdir(parents=True)
        (d / "current.diff").write_text("x", encoding="utf-8")
        os.utime(d, (1_700_000_000 + i, 1_700_000_000 + i))
    if ledger:
        (logs_root / "completed.jsonl").write_text('{"id":"x"}\n', encoding="utf-8")
    return logs_root


def _ns(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {"keep": None, "all": False, "yes": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_keep_n_keeps_newest_and_ledger(tmp_path: Path) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2", "t3"])
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(keep=1), config) == 0
    remaining = {p.name for p in logs_root.iterdir()}
    assert remaining == {"t3", "completed.jsonl"}  # newest task dir + the ledger


def test_bare_clean_removes_all_task_dirs_but_keeps_ledger(tmp_path: Path) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2"])
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(yes=True), config) == 0
    remaining = {p.name for p in logs_root.iterdir()}
    assert remaining == {"completed.jsonl"}


def test_all_removes_ledger_too(tmp_path: Path) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2"])
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(all=True, yes=True), config) == 0
    assert list(logs_root.iterdir()) == []


def test_bare_clean_aborts_when_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2"])
    config = _config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert cli._cmd_logs_clean(_ns(), config) == 0
    assert "aborted" in capsys.readouterr().out
    assert {p.name for p in logs_root.iterdir()} == {"t1", "t2", "completed.jsonl"}


def test_keep_zero_confirms_like_delete_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2"])
    config = _config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert cli._cmd_logs_clean(_ns(keep=0), config) == 0  # declined → nothing removed
    assert {p.name for p in logs_root.iterdir()} == {"t1", "t2", "completed.jsonl"}


def test_negative_keep_is_rejected(tmp_path: Path) -> None:
    _seed_logs(tmp_path, ["t1"])
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(keep=-1), config) == 2


def test_nothing_to_remove_when_logs_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".worc" / "logs").mkdir(parents=True)  # exists but no task dirs, no ledger
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(), config) == 0
    assert "nothing to remove" in capsys.readouterr().out
