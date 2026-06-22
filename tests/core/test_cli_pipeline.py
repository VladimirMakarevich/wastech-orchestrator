"""Tests for the ``run`` / ``watch`` CLI wiring and the end-to-end happy path."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.core.orchestrator import PipelineResult
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.state_store import StateStore, TaskRow


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    for handler in pkg.handlers:
        handler.close()
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


# --- watch_once unit tests (fake orchestrator) -------------------------------------------


class _FakeOrch:
    def __init__(self, *, resume=None, runs=None) -> None:
        self._resume = resume
        self._runs = list(runs or [])
        self.run_calls: list[str] = []
        self.resume_calls = 0
        self.refresh_calls = 0

    def resume(self):
        self.resume_calls += 1
        return self._resume

    def acquire_slot(self, task_id: str) -> bool:
        return True

    def refresh_repo(self) -> None:
        self.refresh_calls += 1

    def run_task(self, task_file: str):
        self.run_calls.append(task_file)
        return self._runs.pop(0)


def _pending(tmp_path: Path, *names: str) -> Path:
    folder = tmp_path / "pending"
    folder.mkdir()
    for name in names:
        (folder / name).write_text("x", encoding="utf-8")
    return folder


def _done(task_id: str) -> PipelineResult:
    return PipelineResult(task_id=task_id, final_status=Status.DONE)


def test_watch_auto_off_processes_one(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=False)
    orch = _FakeOrch(runs=[_done("a"), _done("b")])
    folder = _pending(tmp_path, "a.md", "b.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(results) == 1
    assert len(orch.run_calls) == 1  # only the first pending task


def test_watch_auto_on_processes_all(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=True)
    orch = _FakeOrch(runs=[_done("a"), _done("b")])
    folder = _pending(tmp_path, "a.md", "b.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(results) == 2
    assert len(orch.run_calls) == 2


def test_watch_manual_blocks_continuation(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=True)
    manual = PipelineResult(task_id="a", final_status=Status.MANUAL_ACTION_REQUIRED)
    orch = _FakeOrch(runs=[manual, _done("b")])
    folder = _pending(tmp_path, "a.md", "b.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(results) == 1  # the manual task blocks the second
    assert results[0].final_status is Status.MANUAL_ACTION_REQUIRED


def test_watch_resume_manual_blocks(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=True)
    manual = PipelineResult(task_id="r", final_status=Status.MANUAL_ACTION_REQUIRED)
    orch = _FakeOrch(resume=manual, runs=[_done("a")])
    folder = _pending(tmp_path, "a.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert results == [manual]
    assert orch.run_calls == []  # resume's manual outcome blocks picking pending


# --- watch_loop unit tests (periodic discovery) ------------------------------------


def test_watch_loop_refreshes_each_tick_and_sleeps_between(
    make_git_config, git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_git_config(git_repo.clone)
    orch = _FakeOrch()
    ticks = {"n": 0}

    def fake_watch_once(_o, _c, _f):
        ticks["n"] += 1
        return [_done(f"t{ticks['n']}")]

    monkeypatch.setattr(cli, "watch_once", fake_watch_once)
    sleeps: list[float] = []
    results = cli.watch_loop(
        orch, config, tmp_path, poll_interval=60, max_iterations=3, sleep_fn=sleeps.append
    )  # type: ignore[arg-type]
    assert orch.refresh_calls == 3  # repo refreshed before every tick
    assert ticks["n"] == 3
    assert sleeps == [60, 60]  # slept between ticks, never after the last
    assert len(results) == 3


def test_watch_loop_single_pass_when_poll_zero(
    make_git_config, git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_git_config(git_repo.clone)
    orch = _FakeOrch()
    monkeypatch.setattr(cli, "watch_once", lambda _o, _c, _f: [])
    sleeps: list[float] = []
    cli.watch_loop(orch, config, tmp_path, poll_interval=0, sleep_fn=sleeps.append)  # type: ignore[arg-type]
    assert orch.refresh_calls == 1  # one tick (still refreshes before scanning)
    assert sleeps == []  # no loop, no sleep


# --- end-to-end via main() with fake CLIs ------------------------------------------------


def _write_cli_config(
    project: Path,
    clone: Path,
    *,
    claude_cmd: str,
    codex_cmd: str,
    create_pr: bool = False,
    auto_mode: bool = False,
) -> Path:
    env = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]
    env_lines = "\n".join(f"    - {e}" for e in env)
    config = project / "config.yaml"
    config.write_text(
        f"""
orchestrator:
  auto_mode:
    enabled: {str(auto_mode).lower()}
  poll_interval_seconds: 0
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(clone)!r}
  base_branch: "main"
  branch_prefix: "agent"
agents:
  allowed: [claude, codex]
  providers:
    claude:
      command: {claude_cmd!r}
      primary: true
    codex:
      command: {codex_cmd!r}
security:
  allowed_environment:
{env_lines}
validation:
  quarantine_folder: {str(project / "rejected")!r}
checks:
  commands: []
git:
  create_pull_request: {str(create_pr).lower()}
  pr_base: "main"
""",
        encoding="utf-8",
    )
    return config


def _complete_task_file(path: Path, task_id: str) -> None:
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\n---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )


def test_cmd_run_happy_path(
    git_repo, fake_cli, git_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    claude_cmd = fake_cli("success_edit", "claude")
    codex_cmd = fake_cli("success_edit", "codex")
    config = _write_cli_config(project, git_repo.clone, claude_cmd=claude_cmd, codex_cmd=codex_cmd)
    task_file = project / "task-100.md"
    _complete_task_file(task_file, "task-100")
    operator_log = project / "operator.jsonl"

    code = cli.main(
        [
            "--config",
            str(config),
            "--log-format",
            "json",
            "--log-file",
            str(operator_log),
            "--heartbeat-seconds",
            "0",
            "run",
            str(task_file),
        ]
    )
    assert code == 0
    # One commit on the task branch; the agent's change is committed; back on main.
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    branch = "agent/task-100-add-a-thing"
    committed = git_run(["show", "--name-only", "--format=", branch], git_repo.clone)
    assert "agent_change.py" in committed
    # Artifacts + exactly one ledger record under the gitignored .worc/ home in the repo.
    worc = git_repo.clone / ".worc"
    assert (worc / "logs" / "task-100" / "summary.md").exists()
    ledger_lines = (worc / "logs" / "completed.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger_lines) == 1
    assert json.loads(ledger_lines[0])["final_status"] == "done"
    # The task file moved into its lifecycle folder (done), out of the project root.
    assert (project / "done" / "task-100.md").exists()
    messages = {
        json.loads(line)["msg"] for line in operator_log.read_text(encoding="utf-8").splitlines()
    }
    # The orchestrator-owned preamble/terminal still emit progress markers via `_observe`; per-stage
    # / commit / push progress now lives in `node_runs` + structured provider/git logging (the
    # engine node runners do not wrap each step in `_observe`).
    assert {
        "branch preparation started",
        "branch preparation completed",
        "terminal cleanup started",
        "terminal cleanup completed",
    } <= messages


def test_in_repo_commit_stores_task_and_summary_not_logs(
    git_repo, fake_cli, git_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-repo audit footprint: the task (moved to done/) + its summary.md are committed; logs/ is
    not. Code change and task lifecycle are separate commits on the branch."""
    project = tmp_path / "project"
    project.mkdir()
    claude_cmd = fake_cli("success_edit", "claude")
    codex_cmd = fake_cli("success_edit", "codex")
    config = _write_cli_config(
        project,
        git_repo.clone,
        claude_cmd=claude_cmd,
        codex_cmd=codex_cmd,
    )
    # The task lives in the repo's own tasks/pending (how a teammate hands work over via git).
    task_file = git_repo.clone / "tasks" / "pending" / "task-300.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    _complete_task_file(task_file, "task-300")

    code = cli.main(["--config", str(config), "--heartbeat-seconds", "0", "run", str(task_file)])
    assert code == 0

    branch = "agent/task-300-add-a-thing"
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    tracked = git_run(["ls-tree", "-r", "--name-only", branch], git_repo.clone)
    assert "tasks/done/task-300.md" in tracked  # task moved into done/ and committed
    assert "tasks/done/task-300.summary.md" in tracked  # summary committed next to the task
    assert "agent_change.py" in tracked  # the code change
    assert ".worc/" not in tracked  # plan/review/stage-logs/summary.json never enter git
    # Code and task lifecycle are distinct commits on the branch.
    subjects = git_run(["log", "--format=%s", "main.." + branch], git_repo.clone)
    assert "feat(task-300)" in subjects
    assert "audit trail for task-300" in subjects
    # summary.json stays a local-only working artifact under .worc/logs/.
    assert (git_repo.clone / ".worc" / "logs" / "task-300" / "summary.json").exists()


def test_cmd_status_reports_active_task(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(
        project,
        git_repo.clone,
        claude_cmd="claude",
        codex_cmd="codex",
    )
    db_path = git_repo.clone / ".worc" / "state.db"
    store = StateStore.open(db_path)
    store.insert_task(
        TaskRow(
            task_id="task-active",
            title="Active task",
            status=Status.RUNNING,
            branch="agent/task-active-active-task",
            fix_iterations=2,
            updated_at="2026-06-12T10:00:00+00:00",
        )
    )
    # The flow checkpoint surfaces where the engine will resume (replaces the granular-stage view).
    store.save_flow_checkpoint(
        "task-active",
        current_node="implementation",
        counters_json="{}",
        flow_fingerprint="fp",
        fix_iterations=2,  # checkpoint mirrors the task's fix counter
    )
    store.close()

    code = cli.main(["--config", str(config), "status"])

    assert code == 0
    output = capsys.readouterr().out
    assert "task_id=task-active" in output
    assert "status=running" in output
    assert "node=implementation" in output
    assert "branch=agent/task-active-active-task" in output
    assert "fix_iterations=2" in output


def test_cmd_run_rejected_task(git_repo, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    bad = project / "task-bad.md"
    bad.write_text("no front matter\n", encoding="utf-8")

    code = cli.main(["--config", str(config), "run", str(bad)])
    assert code == 1  # failed
    worc = git_repo.clone / ".worc"
    report = worc / "logs" / "task-bad" / "validation_report.json"
    assert report.exists()
    assert json.loads(report.read_text(encoding="utf-8"))["reason"] == "frontmatter_missing"
    # Quarantined, and no branch was created.
    assert (project / "rejected" / "task-bad.md").exists()


def test_cmd_watch_auto_mode_two_tasks(
    git_repo, fake_cli, git_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    # watch scans tasks/pending at the repo root (the tracked audit trail), not the cwd.
    (git_repo.clone / "tasks" / "pending").mkdir(parents=True)
    claude_cmd = fake_cli("success_edit", "claude")
    codex_cmd = fake_cli("success_edit", "codex")
    config = _write_cli_config(
        project, git_repo.clone, claude_cmd=claude_cmd, codex_cmd=codex_cmd, auto_mode=True
    )
    for tid in ("task-201", "task-202"):
        _complete_task_file(git_repo.clone / "tasks" / "pending" / f"{tid}.md", tid)

    monkeypatch.chdir(project)
    code = cli.main(["--config", str(config), "watch"])
    assert code == 0
    worc = git_repo.clone / ".worc"
    ledger_lines = (worc / "logs" / "completed.jsonl").read_text(encoding="utf-8").splitlines()
    ids = {json.loads(line)["id"] for line in ledger_lines}
    assert ids == {"task-201", "task-202"}  # both ran sequentially under auto mode
    # Each task left pending and was audit-committed (task + summary) on its own agent branch; the
    # working tree is back on base, so the committed files live in git history, not on disk.
    for tid in ("task-201", "task-202"):
        assert not (git_repo.clone / "tasks" / "pending" / f"{tid}.md").exists()
        branch = f"agent/{tid}-add-a-thing"
        tracked = git_run(["ls-tree", "-r", "--name-only", branch], git_repo.clone)
        assert f"tasks/done/{tid}.md" in tracked
        assert f"tasks/done/{tid}.summary.md" in tracked
