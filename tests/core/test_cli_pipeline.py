"""Tests for the ``run`` / ``watch`` CLI wiring and the end-to-end happy path."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.conftest import seed_builtin_flows

from wastech_orchestrator import cli
from wastech_orchestrator.core.orchestrator import (
    DependencyVerdict,
    Eligibility,
    PipelineResult,
)
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import KIND_PR
from wastech_orchestrator.ledger import Ledger, LedgerRecord
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow
from wastech_orchestrator.task.model import DEFAULT_QUEUE

# Every test here is a slow integration test (real git / subprocess / process tree).
# ``run``/``watch``/``rerun`` probe every allowed provider's credentials before starting. These
# tests are about command orchestration, not credentials, and the config names the real CLIs — so
# the gate is disarmed module-wide and asserted on directly by its own tests instead.
pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("no_provider_auth_gate")]


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
    def __init__(self, *, resume=None, runs=None, notifier=None, settled=None) -> None:
        self._resume = resume
        self._runs = list(runs or [])
        self.run_calls: list[str] = []
        self.resume_calls = 0
        self.refresh_calls = 0
        self.notifier = notifier  # the next-task gate reads this
        self._settled = set(settled or ())  # ids the orchestrator calls their own leftover file

    def resume(self):
        self.resume_calls += 1
        return self._resume

    def acquire_slot(self, task_id: str) -> bool:
        return True

    def settled_own_file(self, task_id: str, _task_file: Path) -> bool:
        return task_id in self._settled

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


def _pending_fm(tmp_path: Path, *ids: str) -> Path:
    """Like :func:`_pending` but writes real front matter so the scan extracts each ``id``."""
    folder = tmp_path / "pending"
    folder.mkdir(exist_ok=True)
    for task_id in ids:
        (folder / f"{task_id}.md").write_text(
            f"---\nid: {task_id}\ntitle: {task_id}\n---\nbody\n", encoding="utf-8"
        )
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


def test_watch_skips_settled_own_file(make_git_config, git_repo, tmp_path: Path) -> None:
    # A settled task's own file lingering in pending/ must be skipped, not re-run into a
    # duplicate_task_id reject; an independent pending task still runs. Which files count as a
    # settled task's own is the orchestrator's decision (Orchestrator.settled_own_file); this pins
    # that the scanner honors it and keeps going.
    config = make_git_config(git_repo.clone, auto_mode=True)
    folder = _pending_fm(tmp_path, "a", "b")
    orch = _FakeOrch(runs=[_done("b")], settled={"a"})
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert orch.run_calls == [str(folder / "b.md")]  # 'a' skipped, 'b' still runs
    assert [r.task_id for r in results] == ["b"]


def test_watch_reruns_when_settled_file_differs(make_git_config, git_repo, tmp_path: Path) -> None:
    # A *different* file colliding on an already-used id is not the task's own leftover, so it must
    # fall through to run_task (the gate then rejects it loudly as a duplicate id).
    config = make_git_config(git_repo.clone, auto_mode=False)
    folder = _pending_fm(tmp_path, "a")
    orch = _FakeOrch(runs=[_done("a")], settled=())
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert orch.run_calls == [str(folder / "a.md")]  # collision falls through to the gate


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


def test_watch_rate_limit_park_pauses_queue(make_git_config, git_repo, tmp_path: Path) -> None:
    # Queue circuit-breaker: when the first task parks on a rate limit (run_task → RUNNING), the
    # parked task holds the single slot, so acquire_slot() denies the next pending task and
    # watch_once stops the chain — no separate breaker; it falls out of the single-slot park.
    config = make_git_config(git_repo.clone, auto_mode=True)

    class _ParkingOrch(_FakeOrch):
        def __init__(self) -> None:
            super().__init__(
                runs=[
                    PipelineResult(task_id="a", final_status=Status.RUNNING),  # parked on the limit
                    _done("b"),
                ]
            )
            self._parked = False

        def acquire_slot(self, task_id: str) -> bool:
            return not self._parked  # a parked (RUNNING) task keeps owning the single slot

        def run_task(self, task_file: str):
            result = super().run_task(task_file)
            if result.final_status is Status.RUNNING:
                self._parked = True
            return result

    orch = _ParkingOrch()
    folder = _pending(tmp_path, "a.md", "b.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert [r.final_status for r in results] == [Status.RUNNING]  # only the parked task
    assert len(orch.run_calls) == 1  # the second pending task is never picked up


def test_summarize_watch_labels_parked_and_exit_code() -> None:
    # A parked RUNNING result gets a distinct, non-failure exit code and a "paused" summary line.
    parked = PipelineResult(task_id="r", final_status=Status.RUNNING)
    assert cli._summarize_watch([parked]) == 3
    assert cli._EXIT_BY_STATUS[Status.RUNNING] == 3


# --- next-task confirmation gate ---------------------------------------------------------


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


def test_watch_ties_break_by_natural_not_bytewise_filename(
    make_git_config, git_repo, tmp_path: Path
) -> None:
    # At equal priority the daemon claims files in natural (numeric-aware) order — p9-07 first, p10
    # last — matching what the operator reads in the file manager. Bytewise order would claim p10-01
    # first (``'1' < '9'``), which is exactly the reported symptom.
    config = make_git_config(git_repo.clone, auto_mode=True)
    orch = _FakeOrch(runs=[_done("x")] * 3)
    folder = _prio_folder(tmp_path, ("p10-01", None, ()), ("p9-9", None, ()), ("p9-07", None, ()))
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert [Path(p).stem for p in orch.run_calls] == ["p9-07", "p9-9", "p10-01"]


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

    def fake_watch_once(_o, _c, _f, *, queue=None, notes=None):
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
    monkeypatch.setattr(cli, "watch_once", lambda _o, _c, _f, *, queue=None, notes=None: [])
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
    # The orchestrator resolves flows only from the clone's ``.worc/flows/`` (no packaged fallback),
    # so deliver the built-ins there as ``worc install`` would.
    seed_builtin_flows(clone)
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
            # The progress markers asserted below are info records, and the shipped default level
            # is `warning` — an operator who wants the play-by-play asks for it the same way.
            "--log-level",
            "info",
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


def _seed_active_status_db(clone: Path) -> None:
    store = StateStore.open(clone / ".worc" / "state.db")
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


def test_cmd_status_reports_active_task(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(
        project,
        git_repo.clone,
        claude_cmd="claude",
        codex_cmd="codex",
    )
    _seed_active_status_db(git_repo.clone)
    monkeypatch.setattr(cli, "_daemon_alive", lambda _c: True)  # daemon live → plain "running"

    code = cli.main(["--config", str(config), "status"])

    assert code == 0
    output = capsys.readouterr().out
    assert "task_id=task-active" in output
    assert "status=running" in output
    assert "node=implementation" in output
    assert "branch=worc/task-active-active-task" in output
    assert "fix_iterations=2" in output


def test_cmd_status_running_without_daemon_shows_parked(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A RUNNING row with no live daemon is parked at its checkpoint, awaiting resume — status must
    # say so instead of a bare "running" that reads as "executing now".
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_active_status_db(git_repo.clone)
    monkeypatch.setattr(cli, "_daemon_alive", lambda _c: False)

    code = cli.main(["--config", str(config), "status"])

    assert code == 0
    output = capsys.readouterr().out
    assert "status=parked (no daemon)" in output
    assert "node=implementation" in output  # the resume checkpoint still shows


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


def _seed_pr_op(clone: Path, task_id: str, url: str) -> None:
    store = StateStore.open(clone / ".worc" / "state.db")
    store.record_publish_op(
        PublishOpRow(
            task_id=task_id,
            kind=KIND_PR,
            fingerprint="fp",
            status="completed",
            result_ref=url,
        )
    )
    store.close()


def _seed_rejects(clone: Path, records: list[LedgerRecord]) -> None:
    ledger = Ledger(clone / ".worc" / "logs")
    for record in records:
        ledger.append(record)


def _reject_record(task_id: str, reason: str, finished_at: str) -> LedgerRecord:
    # The shape the orchestrator appends for a gate reject: no branch, the id as the title, and the
    # reason code. There is no ``tasks`` row for such an id.
    return LedgerRecord(
        id=task_id,
        title=task_id,
        final_status=Status.FAILED.value,
        finished_at=finished_at,
        validation_reason=reason,
    )


def _seed_reject_fixture(clone: Path) -> None:
    """One id rejected twice with no row, one rejected then re-submitted and run, one plain row,
    and one whose ledger trace is not refusals only."""
    _seed_list_db(
        clone,
        [
            TaskRow(task_id="gh-10", title="Ten", status=Status.DONE),
            TaskRow(task_id="gh-11", title="Eleven", status=Status.DONE),
        ],
    )
    _seed_rejects(
        clone,
        [
            _reject_record("gh-9", "missing_description", "2026-09-11T01:00:00+00:00"),
            _reject_record("gh-10", "injection_suspected", "2026-09-11T01:05:00+00:00"),
            _reject_record("gh-12", "missing_description", "2026-09-11T01:30:00+00:00"),
            # A run that reached a terminal carries no reason, so gh-12's trace is no longer
            # refusals only — having no row is not on its own enough to call an id rejected.
            LedgerRecord(
                id="gh-12",
                title="Twelve",
                final_status=Status.DONE.value,
                finished_at="2026-09-11T01:40:00+00:00",
            ),
            _reject_record("gh-9", "injection_suspected", "2026-09-11T02:00:00+00:00"),
        ],
    )


def test_cmd_list_json_carries_pr_url(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The URL an external process needs is the one the completed `pr` publish op recorded; a task
    # that opened no PR carries the key anyway, as null, so the entry shape does not vary.
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(
        git_repo.clone,
        [
            TaskRow(task_id="task-pr", title="With PR", status=Status.DONE),
            TaskRow(task_id="task-nopr", title="No PR", status=Status.DONE),
        ],
    )
    _seed_pr_op(git_repo.clone, "task-pr", "https://example.test/o/r/pull/7")
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    _complete_task_file(pending / "task-queued.md", "task-queued")

    code = cli.main(["--config", str(config), "list", "--all", "--format", "json"])

    assert code == 0
    entries = {e["task_id"]: e for e in json.loads(capsys.readouterr().out)}
    assert entries["task-pr"]["pr_url"] == "https://example.test/o/r/pull/7"
    assert entries["task-nopr"]["pr_url"] is None
    # `--all` is the DB-row view: a queued file has no row yet and belongs to the other views.
    assert "task-queued" not in entries

    # The key is on the row wherever the row is shown, not only under `--all`.
    for flags in (["--recent", "5"], []):
        assert cli.main(["--config", str(config), "list", *flags, "--format", "json"]) == 0
        shown = {e["task_id"]: e for e in json.loads(capsys.readouterr().out)}
        assert shown["task-pr"]["pr_url"] == "https://example.test/o/r/pull/7", flags
        assert shown["task-nopr"]["pr_url"] is None, flags


def test_cmd_list_json_pending_entry_carries_null_pr_url(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--all` holds DB rows only, so the file-derived pending entry is asserted where it appears:
    # the default view and `--pending`.
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    _complete_task_file(pending / "task-queued.md", "task-queued")

    for flags in (["--pending"], []):
        code = cli.main(["--config", str(config), "list", *flags, "--format", "json"])

        assert code == 0
        entries = {e["task_id"]: e for e in json.loads(capsys.readouterr().out)}
        assert entries["task-queued"]["pr_url"] is None


def test_cmd_list_all_json_rejected_section(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_reject_fixture(git_repo.clone)
    ledger_path = git_repo.clone / ".worc" / "logs" / "completed.jsonl"
    before = ledger_path.read_bytes()

    code = cli.main(["--config", str(config), "list", "--all", "--format", "json"])

    assert code == 0
    # `list` reads the ledger the way it reads the store: it must never append to it.
    assert ledger_path.read_bytes() == before
    data = json.loads(capsys.readouterr().out)
    rejected = [e for e in data if e["status"] == "rejected"]
    assert [e["task_id"] for e in rejected] == ["gh-9"]
    assert rejected[0] == {
        "task_id": "gh-9",
        "status": "rejected",
        "title": None,
        "branch": None,
        "pr_url": None,
        # The latest of the two reject records wins — the ledger is append-only.
        "validation_reason": "injection_suspected",
        "rejected_at": "2026-09-11T02:00:00+00:00",
    }
    # An id that was re-submitted under the same name and ran has a row, so it is an ordinary task
    # again and must not be reported as rejected as well.
    ordinary = {e["task_id"]: e["status"] for e in data if e["status"] != "rejected"}
    assert ordinary == {"gh-10": "done", "gh-11": "done"}


def test_cmd_list_all_table_prints_rejected_section(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_reject_fixture(git_repo.clone)

    code = cli.main(["--config", str(config), "list", "--all"])

    assert code == 0
    out = capsys.readouterr().out
    assert "rejected:" in out
    # `rejected  <id>  (<reason>)` — the status column, the id, and the only fact the entry holds.
    assert next(line for line in out.splitlines() if "gh-9" in line).split() == [
        "rejected",
        "gh-9",
        "(injection_suspected)",
    ]
    # The human view gains no column: an ordinary row still reads status, id, title.
    assert next(line for line in out.splitlines() if "gh-10" in line).split() == [
        "done",
        "gh-10",
        "Ten",
    ]


def test_cmd_list_rejected_ids_stay_out_of_the_completion_surface(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A rejected id has no row, so no id-taking verb accepts it; neither the default view nor any
    # `--format ids` view may offer it.
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_reject_fixture(git_repo.clone)

    for argv in (
        ["list"],
        ["list", "--format", "ids"],
        ["list", "--all", "--format", "ids"],
        ["list", "--format", "json"],
    ):
        code = cli.main(["--config", str(config), *argv])

        assert code == 0
        out = capsys.readouterr().out
        assert "gh-9" not in out, argv
        assert "rejected" not in out, argv
        # The views still report everything they reported before, so the assertions above are
        # about the rejected id and not about an empty listing.
        assert "gh-10" in out, argv


def test_cmd_list_all_without_a_ledger_has_no_rejected_entries(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(git_repo.clone, [TaskRow(task_id="task-done", title="D", status=Status.DONE)])
    assert not (git_repo.clone / ".worc" / "logs" / "completed.jsonl").exists()

    code = cli.main(["--config", str(config), "list", "--all", "--format", "json"])

    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert [e["task_id"] for e in data] == ["task-done"]
    assert all(e["status"] != "rejected" for e in data)


def test_cmd_list_all_reports_a_torn_ledger_line_cleanly(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A half-written ledger line exits 2 with a message, not a traceback out of a listing.

    `list` is the first read-only command to read the ledger, so a torn append after a crash (or a
    hand edit) became newly reachable. The bad line is not skipped: `Ledger.records` also feeds the
    duplicate-id gate, and a line silently dropped there would let a re-submitted id through.
    """
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(git_repo.clone, [TaskRow(task_id="task-done", title="D", status=Status.DONE)])
    _seed_rejects(git_repo.clone, [_reject_record("gh-9", "injection_suspected", "2026-01-01")])
    completed = git_repo.clone / ".worc" / "logs" / "completed.jsonl"
    with completed.open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"id": "gh-10", "validation_re\n')

    code = cli.main(["--config", str(config), "list", "--all", "--format", "json"])

    assert code == 2
    out = capsys.readouterr().out
    assert out.startswith("error: cannot read the completed-tasks ledger at ")
    assert "completed.jsonl" in out
    # The listing produced no JSON at all rather than a half-built array.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cmd_list_default_view_survives_a_torn_ledger_line(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only `--all` reads the ledger, so the default view is unaffected by a torn line."""
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(git_repo.clone, [TaskRow(task_id="task-done", title="D", status=Status.DONE)])
    logs = git_repo.clone / ".worc" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "completed.jsonl").write_text("{not json\n", encoding="utf-8", newline="")

    code = cli.main(["--config", str(config), "list", "--format", "json"])

    assert code == 0
    assert [e["task_id"] for e in json.loads(capsys.readouterr().out)] == ["task-done"]


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


def test_cmd_list_pending_format_ids_reads_disk_queue(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--pending --format ids` composes the section focus with the id format — it lists queued
    # pending files (disk-derived) that have no DB row yet, matching the table view. The DB-only
    # `_list_ids` path printed nothing for them before.
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "queued.md").write_text(
        '---\nid: task-queued\ntitle: "Q"\n---\n\nbody\n', encoding="utf-8"
    )
    # A DB row for a different task must not appear under the pending focus.
    _seed_list_db(git_repo.clone, [TaskRow(task_id="task-done", title="D", status=Status.DONE)])

    code = cli.main(["--config", str(config), "list", "--pending", "--format", "ids"])

    assert code == 0
    assert set(capsys.readouterr().out.split()) == {"task-queued"}


def test_cmd_list_pending_matches_top_and_watch_order_and_queue_filter(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Defect 3: `worc list --pending` must show the *scheduler's* order and membership — priority-
    # ranked and queue-filtered — identical to what `top`/`ps` display and `watch` claims, not the
    # raw, unfiltered file-manager listing.
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)

    def _write(stem: str, *, priority: str | None = None, queue: str | None = None) -> None:
        lines = [f"id: {stem}", 'title: "T"']
        if priority is not None:
            lines.append(f"priority: {priority}")
        if queue is not None:
            lines.append(f"queue: {queue}")
        (pending / f"{stem}.md").write_text(
            "---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8"
        )

    _write("p10-01")  # mid, default
    _write("p9-07")  # mid, default
    _write("p9-2", priority="high")  # high, default → runs first
    _write("other", queue="backend")  # foreign queue → filtered out of every default-queue view

    # The scheduler's own order (what `watch_once` claims) for the served queue.
    watch_order = [p.stem for p, _ in cli.scan_pending_sorted(pending, DEFAULT_QUEUE)]
    assert watch_order == ["p9-2", "p9-07", "p10-01"]

    # `top` / the console `ps` view read the same function → same labels, foreign queue absent.
    cfg = cli.load_config_for(cli.build_parser().parse_args(["--config", str(config), "list"]))
    assert cfg is not None
    snap = cli.build_top_snapshot(
        cfg, None, selector=DEFAULT_QUEUE, log_path=None, log_tail_lines=0, recent_limit=0
    )
    assert [q.label for q in snap.queue] == watch_order

    # `worc list --pending` now routes through the ranking too: same order, foreign queue absent,
    # each row carrying its rank position + the priority/queue it sorted on.
    code = cli.main(["--config", str(config), "list", "--pending", "--format", "json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert [e["task_id"] for e in data] == watch_order
    assert [e["rank"] for e in data] == ["1", "2", "3"]
    assert data[0]["priority"] == "high"
    assert data[0]["queue"] == "default"
    assert all(e["task_id"] != "other" for e in data)


def test_cmd_list_all_format_ids_unions_disk_and_db(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--all --format ids` is consistent with the table's `--all` scope (DB tasks); a plain
    # `--format ids` (no section) stays DB-derived as before (covered by the bare test above).
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    _seed_list_db(
        git_repo.clone,
        [
            TaskRow(task_id="task-a", title="A", status=Status.DONE),
            TaskRow(task_id="task-b", title="B", status=Status.RUNNING),
        ],
    )

    code = cli.main(["--config", str(config), "list", "--all", "--format", "ids"])

    assert code == 0
    assert set(capsys.readouterr().out.split()) == {"task-a", "task-b"}


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


def test_cmd_run_rejects_existing_branch_mode_with_missing_ref(git_repo, tmp_path: Path) -> None:
    # Fail-closed preflight: `existing` with a branch_ref that exists neither
    # locally nor on the remote is rejected before any slot/branch is taken (no auto-create).
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    task = project / "task-bm.md"
    task.write_text(
        '---\nid: task-bm\ntitle: "T"\nbranch_mode: existing\nbranch_ref: no-such-branch\n---\n\n'
        "## Description\n\nDo it.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    code = cli.main(["--config", str(config), "run", str(task)])
    assert code == 1  # failed at preflight
    report = git_repo.clone / ".worc" / "logs" / "task-bm" / "validation_report.json"
    assert json.loads(report.read_text(encoding="utf-8"))["reason"] == "invalid_branch_mode"
    assert (project / "rejected" / "task-bm.md").exists()  # quarantined, no branch created


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


def test_a_settled_tasks_own_file_survives_the_next_watch_tick(
    git_repo,
    fake_cli,
    git_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The full default-configuration chain that made a successful task report as failed.

    ``tasks/`` is tracked (that is what makes it the audit trail), so promote commits the file on
    base while the terminal move is committed on the task branch. Returning to base then restores
    the pending copy underneath the finished task, and the next poll tick finds it again. Nothing
    about that is exotic — every element of it is a default — so the second tick, and an explicit
    ``run`` on the same file, must both leave the operator's tree and ledger exactly as they were.
    """
    project = tmp_path / "project"
    project.mkdir()
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True)
    claude_cmd = fake_cli("success_edit", "claude")
    codex_cmd = fake_cli("success_edit", "codex")
    config = _write_cli_config(project, git_repo.clone, claude_cmd=claude_cmd, codex_cmd=codex_cmd)
    task_file = pending / "task-301.md"
    _complete_task_file(task_file, "task-301")
    # The promote commit: without it the file cannot come back, and the defect cannot reproduce.
    git_run(["add", "tasks/pending/task-301.md"], git_repo.clone)
    git_run(["commit", "-m", "promote task-301"], git_repo.clone)
    monkeypatch.chdir(project)

    assert cli.main(["--config", str(config), "watch"]) == 0
    ledger_path = git_repo.clone / ".worc" / "logs" / "completed.jsonl"
    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert [r["final_status"] for r in records] == ["done"]
    assert task_file.exists()  # restored by the base-branch checkout that ends the run

    assert cli.main(["--config", str(config), "watch"]) == 0

    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert [r["final_status"] for r in records] == ["done"]  # no second, contradicting record
    assert task_file.exists()
    assert not (project / "rejected").exists()  # the tracked file was never moved out of the tree
    assert git_run(["status", "--porcelain", "--", "tasks"], git_repo.clone) == ""

    # An explicit run has no scanner guard in front of it: it still answers loudly, but the reject
    # path must not touch the file, the ledger or the operator's notifications either.
    assert cli.main(["--config", str(config), "run", str(task_file)]) != 0
    assert "duplicate_task_id" in capsys.readouterr().err
    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert [r["final_status"] for r in records] == ["done"]
    assert task_file.exists()
    assert not (project / "rejected").exists()
