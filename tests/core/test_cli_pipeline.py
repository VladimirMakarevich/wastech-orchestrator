"""Tests for the ``run`` / ``watch`` CLI wiring and the end-to-end happy path."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.core.orchestrator import (
    DependencyVerdict,
    Eligibility,
    PipelineResult,
)
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
    def __init__(self, *, resume=None, runs=None, notifier=None) -> None:
        self._resume = resume
        self._runs = list(runs or [])
        self.run_calls: list[str] = []
        self.resume_calls = 0
        self.refresh_calls = 0
        self.notifier = notifier  # the next-task gate (idea 27) reads this

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


def test_watch_resume_parked_blocks_continuation(make_git_config, git_repo, tmp_path: Path) -> None:
    # A B-lite soft pause (non-terminal RUNNING) holds the slot: watch_once returns early without
    # picking a pending task; the between-tick poll sleep is the cool-off, the next tick re-resumes.
    config = make_git_config(git_repo.clone, auto_mode=True)
    parked = PipelineResult(task_id="r", final_status=Status.RUNNING)
    orch = _FakeOrch(resume=parked, runs=[_done("a")])
    folder = _pending(tmp_path, "a.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert results == [parked]
    assert orch.run_calls == []  # the parked active task blocks picking pending


def test_summarize_watch_labels_parked_and_exit_code() -> None:
    # A parked RUNNING result gets a distinct, non-failure exit code and a "paused" summary line.
    parked = PipelineResult(task_id="r", final_status=Status.RUNNING)
    assert cli._summarize_watch([parked]) == 3
    assert cli._EXIT_BY_STATUS[Status.RUNNING] == 3


# --- next-task confirmation gate (idea 27) -----------------------------------------------


class _GateNotifier:
    """Records ask_human calls and returns a programmed approve/deny/timeout result."""

    def __init__(self, result) -> None:
        self._result = result
        self.asks = 0

    def ask_human(self, **kwargs):
        self.asks += 1
        return self._result


def _confirm_config(config):
    """Flip ``auto_mode.confirm_next_task`` on (the conftest builder has no knob for it)."""
    from dataclasses import replace

    return replace(
        config,
        orchestrator=replace(
            config.orchestrator,
            auto_mode=replace(config.orchestrator.auto_mode, confirm_next_task=True),
        ),
    )


def test_watch_confirm_next_task_approve_claims(make_git_config, git_repo, tmp_path: Path) -> None:
    from wastech_orchestrator.notify import AskResult

    config = _confirm_config(make_git_config(git_repo.clone, auto_mode=True))
    notifier = _GateNotifier(AskResult(answered=True, approved=True))
    orch = _FakeOrch(runs=[_done("a"), _done("b")], notifier=notifier)
    folder = _pending(tmp_path, "a.md", "b.md")
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(orch.run_calls) == 2  # both approvals → both claimed
    assert notifier.asks == 2


def test_watch_confirm_next_task_deny_stops(make_git_config, git_repo, tmp_path: Path) -> None:
    from wastech_orchestrator.notify import AskResult

    config = _confirm_config(make_git_config(git_repo.clone, auto_mode=True))
    notifier = _GateNotifier(AskResult(answered=True, approved=False))
    orch = _FakeOrch(runs=[_done("a")], notifier=notifier)
    folder = _pending(tmp_path, "a.md", "b.md")
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert orch.run_calls == []  # denied → not claimed, chaining stops for this cycle
    assert notifier.asks == 1


def test_watch_confirm_next_task_timeout_stops(make_git_config, git_repo, tmp_path: Path) -> None:
    from wastech_orchestrator.notify import AskResult

    config = _confirm_config(make_git_config(git_repo.clone, auto_mode=True))
    notifier = _GateNotifier(AskResult(answered=False, timed_out=True, failure="timeout"))
    orch = _FakeOrch(runs=[_done("a")], notifier=notifier)
    folder = _pending(tmp_path, "a.md")
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert orch.run_calls == []  # silence never advances an autonomous claim (fail-closed STOP)
    assert notifier.asks == 1


def test_watch_confirm_next_task_off_no_prompt(make_git_config, git_repo, tmp_path: Path) -> None:
    # Default (off): no gate, no notifier call — existing watch behavior unchanged.
    config = make_git_config(git_repo.clone, auto_mode=True)
    notifier = _GateNotifier(None)
    orch = _FakeOrch(runs=[_done("a"), _done("b")], notifier=notifier)
    folder = _pending(tmp_path, "a.md", "b.md")
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(orch.run_calls) == 2
    assert notifier.asks == 0


# --- watch_once dependency gating (``depends_on`` merge-gated scheduling) -----------


class _DepOrch(_FakeOrch):
    """Watch fake whose dependency verdicts are keyed by task id (default: eligible)."""

    def __init__(self, *, verdicts=None, runs=None) -> None:
        super().__init__(runs=runs)
        self.verdicts = verdicts or {}
        self.eligibility_calls: list[tuple[str, tuple[str, ...]]] = []
        self.rejected: list[str] = []

    def dependency_eligibility(self, task_id, depends_on, *, pending):
        self.eligibility_calls.append((task_id, tuple(depends_on)))
        return self.verdicts.get(task_id, DependencyVerdict(Eligibility.ELIGIBLE))

    def reject_dependency(self, task_file, detail):
        self.rejected.append(task_file)
        return PipelineResult(task_id=Path(task_file).stem, final_status=Status.FAILED)


def _dep_folder(tmp_path: Path, *specs: tuple[str, tuple[str, ...]]) -> Path:
    folder = tmp_path / "pending"
    folder.mkdir()
    for task_id, deps in specs:
        deps_yaml = "[" + ", ".join(f'"{d}"' for d in deps) + "]"
        front = f'---\nid: {task_id}\ntitle: "T"\ndepends_on: {deps_yaml}\n---\n'
        (folder / f"{task_id}.md").write_text(f"{front}\n## Description\n\nx\n", encoding="utf-8")
    return folder


def test_watch_skips_waiting_runs_later_independent(
    make_git_config, git_repo, tmp_path: Path
) -> None:
    # auto-mode off: an earlier-in-filename ineligible dependent is skipped so the slot still runs
    # the later independent task (the slot never idles on an unmerged dependency).
    config = make_git_config(git_repo.clone, auto_mode=False)
    orch = _DepOrch(
        verdicts={"task-1": DependencyVerdict(Eligibility.WAITING, "dep 'x' unmerged")},
        runs=[_done("task-2")],
    )
    folder = _dep_folder(tmp_path, ("task-1", ("x",)), ("task-2", ()))
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert [Path(p).stem for p in orch.run_calls] == ["task-2"]  # task-1 skipped
    assert orch.eligibility_calls == [("task-1", ("x",))]  # task-2 has no deps → no probe
    assert [r.task_id for r in results] == ["task-2"]


def test_watch_rejects_broken_dependent(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=False)
    orch = _DepOrch(
        verdicts={"task-1": DependencyVerdict(Eligibility.BROKEN, "depends on unknown task 'x'")},
        runs=[_done("task-2")],
    )
    folder = _dep_folder(tmp_path, ("task-1", ("x",)), ("task-2", ()))
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert [Path(p).stem for p in orch.rejected] == ["task-1"]  # terminally rejected
    assert [Path(p).stem for p in orch.run_calls] == ["task-2"]  # the reject did not eat the slot
    assert [r.final_status for r in results] == [Status.FAILED, Status.DONE]


# --- watch_once priority ordering --------------------------------------------------


def _prio_folder(tmp_path: Path, *specs: tuple[str, str | None, tuple[str, ...]]) -> Path:
    """Write pending files. Each spec is ``(filename/id stem, priority | None, depends_on)``."""
    folder = tmp_path / "pending"
    folder.mkdir()
    for stem, priority, deps in specs:
        lines = [f"id: {stem}", 'title: "T"']
        if priority is not None:
            lines.append(f"priority: {priority}")
        if deps:
            lines.append("depends_on: [" + ", ".join(f'"{d}"' for d in deps) + "]")
        front = "---\n" + "\n".join(lines) + "\n---\n"
        (folder / f"{stem}.md").write_text(f"{front}\n## Description\n\nx\n", encoding="utf-8")
    return folder


def test_watch_runs_eligible_in_priority_order(make_git_config, git_repo, tmp_path: Path) -> None:
    # Filenames are deliberately the reverse of priority order to prove priority — not the
    # filename — drives selection. An absent/unknown priority folds to ``mid`` (fail-open).
    config = make_git_config(git_repo.clone, auto_mode=True)
    orch = _FakeOrch(runs=[_done("x")] * 4)
    folder = _prio_folder(
        tmp_path,
        ("a-low", "low", ()),
        ("b-high", "high", ()),
        ("c-default", None, ()),  # → mid
        ("d-bogus", "urgent", ()),  # → mid (tolerated)
    )
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    # high first, then the two mids in filename order, then low.
    assert [Path(p).stem for p in orch.run_calls] == ["b-high", "c-default", "d-bogus", "a-low"]


def test_watch_priority_ties_break_by_filename(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=True)
    orch = _FakeOrch(runs=[_done("x")] * 3)
    folder = _prio_folder(
        tmp_path, ("z-high", "high", ()), ("a-high", "high", ()), ("m-low", "low", ())
    )
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert [Path(p).stem for p in orch.run_calls] == ["a-high", "z-high", "m-low"]


def test_watch_depends_on_beats_priority(make_git_config, git_repo, tmp_path: Path) -> None:
    # A higher-priority but WAITING dependent is skipped so a lower-priority eligible task runs —
    # depends_on is always stronger than priority (the slot never idles on an unmerged dependency).
    config = make_git_config(git_repo.clone, auto_mode=False)
    orch = _DepOrch(
        verdicts={"a-high": DependencyVerdict(Eligibility.WAITING, "dep 'x' unmerged")},
        runs=[_done("b-low")],
    )
    folder = _prio_folder(tmp_path, ("a-high", "high", ("x",)), ("b-low", "low", ()))
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert orch.eligibility_calls == [("a-high", ("x",))]  # high-priority probed first, then skip
    assert [Path(p).stem for p in orch.run_calls] == ["b-low"]
    assert [r.task_id for r in results] == ["b-low"]


# --- watch_once queue partitioning (multi-instance selector) ------------------------


def _queue_folder(tmp_path: Path, *specs: tuple[str, str | None]) -> Path:
    """Pending files tagged with a ``queue`` (``None`` ⇒ no queue field, folds to ``default``)."""
    folder = tmp_path / "pending"
    folder.mkdir()
    for task_id, queue in specs:
        q_line = f"queue: {queue}\n" if queue is not None else ""
        front = f'---\nid: {task_id}\ntitle: "T"\n{q_line}---\n'
        (folder / f"{task_id}.md").write_text(f"{front}\n## Description\n\nx\n", encoding="utf-8")
    return folder


def test_watch_picks_only_matching_queue(make_git_config, git_repo, tmp_path: Path) -> None:
    # config queue defaults to "default": the instance runs explicitly-default and untagged tasks
    # (untagged folds to default), and skips a task tagged for another queue.
    config = make_git_config(git_repo.clone, auto_mode=True)
    orch = _FakeOrch(runs=[_done("a-default"), _done("c-untagged")])
    folder = _queue_folder(
        tmp_path, ("a-default", "default"), ("b-backend", "backend"), ("c-untagged", None)
    )
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert [Path(p).stem for p in orch.run_calls] == ["a-default", "c-untagged"]


def test_watch_queue_selector_override_picks_other_queue(
    make_git_config, git_repo, tmp_path: Path
) -> None:
    # An explicit selector (the `worc watch --queue` override) wins over the config default: only
    # the matching task runs, the default-tagged one is invisible to this instance.
    config = make_git_config(git_repo.clone, auto_mode=True)
    orch = _FakeOrch(runs=[_done("b-backend")])
    folder = _queue_folder(tmp_path, ("a-default", "default"), ("b-backend", "backend"))
    cli.watch_once(orch, config, folder, queue="backend")  # type: ignore[arg-type]
    assert [Path(p).stem for p in orch.run_calls] == ["b-backend"]


# --- watch_loop unit tests (periodic discovery) ------------------------------------


def test_watch_loop_refreshes_each_tick_and_sleeps_between(
    make_git_config, git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_git_config(git_repo.clone)
    orch = _FakeOrch()
    ticks = {"n": 0}

    def fake_watch_once(_o, _c, _f, *, queue=None):
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
    monkeypatch.setattr(cli, "watch_once", lambda _o, _c, _f, *, queue=None: [])
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
  branch_prefix: "worc"
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
    branch = git_run(
        ["branch", "--list", "--format=%(refname:short)", "worc/*-task-100-add-a-thing"],
        git_repo.clone,
    )
    assert branch  # epoch-prefixed; resolve the actual name from the branch list
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

    branch = git_run(
        ["branch", "--list", "--format=%(refname:short)", "worc/*-task-300-add-a-thing"],
        git_repo.clone,
    )
    assert branch  # epoch-prefixed; resolve the actual name from the branch list
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
            branch="worc/task-active-active-task",
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
    assert "branch=worc/task-active-active-task" in output
    assert "fix_iterations=2" in output


def _seed_list_db(clone: Path, rows: list[TaskRow]) -> None:
    store = StateStore.open(clone / ".worc" / "state.db")
    for row in rows:
        store.insert_task(row)
    store.close()


def test_cmd_list_default_overview(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(
        git_repo.clone,
        [
            TaskRow(task_id="task-active", title="Active", status=Status.RUNNING),
            TaskRow(task_id="task-done", title="Done", status=Status.DONE),
        ],
    )
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    _complete_task_file(pending / "task-queued.md", "task-queued")

    code = cli.main(["--config", str(config), "list"])

    assert code == 0
    out = capsys.readouterr().out
    assert "active:" in out and "pending:" in out and "recent:" in out
    assert "task-active" in out  # the active section
    assert "task-queued" in out  # the file-derived pending section
    assert "task-done" in out  # the recent terminal section


def test_cmd_list_format_ids_is_bare(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(
        git_repo.clone,
        [
            TaskRow(task_id="task-active", title="Active", status=Status.RUNNING),
            TaskRow(task_id="task-done", title="Done", status=Status.DONE),
        ],
    )

    code = cli.main(["--config", str(config), "list", "--format", "ids"])

    assert code == 0
    out = capsys.readouterr().out
    assert "active:" not in out  # no section decoration
    assert set(out.split()) == {"task-active", "task-done"}  # every known id, bare


def test_cmd_list_scope_rerun_only_rerunnable(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(
        git_repo.clone,
        [
            TaskRow(task_id="task-fail", title="F", status=Status.FAILED),
            TaskRow(task_id="task-manual", title="M", status=Status.MANUAL_ACTION_REQUIRED),
            TaskRow(task_id="task-done", title="D", status=Status.DONE),
            TaskRow(task_id="task-run", title="R", status=Status.RUNNING),
        ],
    )

    code = cli.main(["--config", str(config), "list", "--format", "ids", "--scope", "rerun"])

    assert code == 0
    out = capsys.readouterr().out
    assert set(out.split()) == {"task-fail", "task-manual"}


def test_cmd_list_scope_status_implies_all_ids(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(
        git_repo.clone,
        [
            TaskRow(task_id="task-done", title="D", status=Status.DONE),
            TaskRow(task_id="task-run", title="R", status=Status.RUNNING),
        ],
    )

    # --scope alone implies the bare id list (it is completion-facing).
    code = cli.main(["--config", str(config), "list", "--scope", "status"])

    assert code == 0
    out = capsys.readouterr().out
    assert set(out.split()) == {"task-done", "task-run"}


def test_cmd_list_format_json(git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(git_repo.clone, [TaskRow(task_id="task-done", title="D", status=Status.DONE)])

    code = cli.main(["--config", str(config), "list", "--format", "json"])

    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert any(entry["task_id"] == "task-done" and entry["status"] == "done" for entry in data)


def test_cmd_list_pending_file_without_id_shown_by_filename(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "weird.md").write_text("no front matter\n", encoding="utf-8")

    code = cli.main(["--config", str(config), "list", "--pending"])

    assert code == 0
    out = capsys.readouterr().out
    assert "weird.md" in out


def test_cmd_list_no_tasks_notice(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")

    code = cli.main(["--config", str(config), "list"])

    assert code == 0
    assert "no tasks" in capsys.readouterr().out


def test_cmd_completion_bash(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["completion", "bash"])

    assert code == 0
    out = capsys.readouterr().out
    assert "worc list --format ids" in out  # single source of truth for ids
    assert "complete -F _worc worc wastech-orchestrator" in out
    assert "rerun" in out and "finalize" in out and "status" in out


def test_cmd_completion_zsh(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["completion", "zsh"])

    assert code == 0
    out = capsys.readouterr().out
    assert "worc list --format ids" in out
    assert "#compdef worc wastech-orchestrator" in out
    assert "compdef _worc worc wastech-orchestrator" in out


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


def test_cmd_run_refuses_unmerged_dependency(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    dependent = project / "task-b.md"
    dependent.write_text(
        '---\nid: task-b\ntitle: "B"\ndepends_on: ["task-a"]\n---\n\n'
        "## Description\n\nDo it.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )

    code = cli.main(["--config", str(config), "run", str(dependent)])
    assert code == 2  # refused: depends on an unknown/unmerged task
    assert "task-a" in capsys.readouterr().err
    # No side effect: the task file is left in place (not quarantined) and no ledger record exists.
    assert dependent.exists()
    assert not (git_repo.clone / ".worc" / "logs" / "completed.jsonl").exists()


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
        branch = git_run(
            ["branch", "--list", "--format=%(refname:short)", f"worc/*-{tid}-add-a-thing"],
            git_repo.clone,
        )
        assert branch  # epoch-prefixed; resolve the actual name
        tracked = git_run(["ls-tree", "-r", "--name-only", branch], git_repo.clone)
        assert f"tasks/done/{tid}.md" in tracked
        assert f"tasks/done/{tid}.summary.md" in tracked
