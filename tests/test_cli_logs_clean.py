"""`worc logs clean`: sweep the whole logs root, keep the ledger unless --all, guard the live state.

The command's contract is that "clean" means a clean `.worc/logs/`: per-task dirs *and* the daemon
logs beside them. The two guards are asserted here as well — an active task refuses the whole
command, a live watch daemon only holds back the files it has open.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig


@pytest.fixture(autouse=True)
def _idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to an idle orchestrator: no active task, no live daemon (both guards inert)."""
    monkeypatch.setattr(cli, "has_active_task", lambda _config: False)
    monkeypatch.setattr(cli, "_daemon_alive", lambda _config: False)


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


def _seed_logs(
    local_path: Path,
    task_ids: list[str],
    *,
    ledger: bool = True,
    daemon_logs: bool = False,
) -> Path:
    """Create ``<local>/.worc/logs/`` with one dir per task id + optional ledger / daemon logs.

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
    if daemon_logs:
        for name in ("daemon.log", "daemon.log.1", "daemon-startup.log"):
            (logs_root / name).write_text("noise\n", encoding="utf-8")
    return logs_root


def _ns(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {"keep": None, "all": False, "yes": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_bare_clean_leaves_only_the_ledger(tmp_path: Path) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2"], daemon_logs=True)
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(yes=True), config) == 0
    assert {p.name for p in logs_root.iterdir()} == {"completed.jsonl"}


def test_bare_clean_names_the_flag_that_takes_the_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_logs(tmp_path, ["t1"], daemon_logs=True)
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(yes=True), config) == 0
    out = capsys.readouterr().out
    assert "logs clean --all" in out  # not a bare "(ledger kept)" the operator has to decode


def test_keep_n_keeps_newest_dirs_and_still_removes_daemon_logs(tmp_path: Path) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2", "t3"], daemon_logs=True)
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(keep=1), config) == 0
    assert {p.name for p in logs_root.iterdir()} == {"t3", "completed.jsonl"}


def test_keep_n_with_all_honors_both_flags(tmp_path: Path) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2", "t3"], daemon_logs=True)
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(keep=1, all=True), config) == 0
    # keep N task dirs, remove the rest + the daemon logs + the ledger — no flag silently ignored.
    assert {p.name for p in logs_root.iterdir()} == {"t3"}


def test_all_removes_ledger_too(tmp_path: Path) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2"], daemon_logs=True)
    config = _config(tmp_path)
    assert cli._cmd_logs_clean(_ns(all=True, yes=True), config) == 0
    assert list(logs_root.iterdir()) == []


def test_bare_clean_aborts_when_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logs_root = _seed_logs(tmp_path, ["t1", "t2"], daemon_logs=True)
    config = _config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert cli._cmd_logs_clean(_ns(), config) == 0
    assert "aborted" in capsys.readouterr().out
    assert {p.name for p in logs_root.iterdir()} == {
        "t1",
        "t2",
        "completed.jsonl",
        "daemon.log",
        "daemon.log.1",
        "daemon-startup.log",
    }


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


def test_active_task_refuses_the_whole_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logs_root = _seed_logs(tmp_path, ["t1"], daemon_logs=True)
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "has_active_task", lambda _config: True)
    assert cli._cmd_logs_clean(_ns(yes=True), config) == 1
    assert "a task is active" in capsys.readouterr().out
    assert (logs_root / "t1").is_dir()  # the running task's own artifacts are untouched


def test_live_daemon_keeps_only_the_daemon_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Not the same refusal as an active task: the task dirs are still pruned, and only the files the
    # daemon holds open are held back — identically on Windows (where the unlink would fail) and
    # POSIX (where it would succeed), so the command is not platform-dependent.
    logs_root = _seed_logs(tmp_path, ["t1", "t2"], daemon_logs=True)
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "_daemon_alive", lambda _config: True)
    assert cli._cmd_logs_clean(_ns(yes=True), config) == 0
    assert {p.name for p in logs_root.iterdir()} == {
        "completed.jsonl",
        "daemon.log",
        "daemon.log.1",
        "daemon-startup.log",
    }
    out = capsys.readouterr().out
    assert "a watch daemon is running" in out
    assert "a task is active" not in out  # distinct messages for the two cases
