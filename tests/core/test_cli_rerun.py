"""Tests for the ``rerun`` CLI command — fresh re-attempt and ``--continue``."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from tests.conftest import seed_builtin_flows

from wastech_orchestrator import cli
from wastech_orchestrator.core.orchestrator import Orchestrator, PipelineResult
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.ledger import Ledger, LedgerRecord
from wastech_orchestrator.state_store import StateStore, TaskRow

_ENV = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]


def _write_config(
    project: Path, clone: Path, *, claude: str, codex: str, create_pr: bool = False
) -> Path:
    env_lines = "\n".join(f"    - {e}" for e in _ENV)
    config = project / "config.yaml"
    config.write_text(
        f"""
orchestrator:
  auto_mode:
    enabled: false
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
      command: {claude!r}
      primary: true
    codex:
      command: {codex!r}
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\n---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )


def _ledger_records(clone: Path) -> list[dict]:
    path = clone / ".worc" / "logs" / "completed.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# --- fresh re-attempt: failed -> rerun -> done -------------------------------------------


def test_real_failure_persists_flow_checkpoint(git_repo, fake_cli, tmp_path: Path) -> None:
    """A real terminal failure leaves a flow checkpoint (current_node) for ``--continue``."""
    project = tmp_path / "project"
    project.mkdir()
    config = _write_config(
        project,
        git_repo.clone,
        claude=fake_cli("process_crashed", "claude"),
        codex=fake_cli("process_crashed", "codex"),
    )
    task_file = project / "task-700.md"
    _complete_task_file(task_file, "task-700")

    code = cli.main(["--config", str(config), "--heartbeat-seconds", "0", "run", str(task_file)])
    assert code == 1  # failed

    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    row = store.get_task("task-700")
    current_node, _counters, _fingerprint = store.get_flow_checkpoint("task-700")
    store.close()
    assert row is not None and row.status is Status.FAILED
    assert current_node is not None  # the flow checkpoint records where --continue re-enters


def test_rerun_fresh_failed_to_done(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    """A failed attempt with a local stale branch (no remote/PR — the common pre-publish failure) is
    re-attempted fresh from base to done; the ledger links the attempts and prior artifacts are
    archived."""
    project = tmp_path / "project"
    project.mkdir()
    config = _write_config(
        project,
        git_repo.clone,
        claude=fake_cli("success_edit", "claude"),
        codex=fake_cli("success_edit", "codex"),
    )
    external = git_repo.clone / ".worc"

    # Seed the bookkeeping of a prior pre-publish failure: a FAILED row + ledger line, a stale
    # *local* branch (never pushed), and a stale artifact under logs/<id>/.
    source = project / "failed" / "task-700.md"
    _complete_task_file(source, "task-700")
    db = external / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(
        TaskRow(
            task_id="task-700",
            title="Add a thing",
            status=Status.FAILED,
            source_path=str(source),
            branch="worc/task-700-add-a-thing",
            slug="add-a-thing",
            cleanup_completed=True,
        )
    )
    store.close()
    Ledger(external / "logs").append(
        LedgerRecord(id="task-700", title="Add a thing", final_status="failed", finished_at="t1")
    )
    stale = external / "logs" / "task-700" / "plan.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale plan from the failed attempt\n", encoding="utf-8")
    git_run(["branch", "worc/task-700-add-a-thing"], git_repo.clone)  # stale local branch

    code = cli.main(
        ["--config", str(config), "--heartbeat-seconds", "0", "rerun", "task-700", "--yes"]
    )
    assert code == 0  # done

    # Two linked ledger records: the prior failure preserved, the rerun marked attempt 2.
    records = _ledger_records(git_repo.clone)
    assert len(records) == 2
    assert records[0]["final_status"] == "failed"
    assert records[1]["final_status"] == "done"
    assert records[1]["attempt"] == 2 and records[1]["rerun_of"] == "task-700"
    # The change is committed on a branch rebuilt from the current base; HEAD back on main.
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # The stale branch was deleted and rebuilt under a fresh epoch; resolve the new name.
    branch = git_run(
        ["branch", "--list", "--format=%(refname:short)", "worc/*-task-700-add-a-thing"],
        git_repo.clone,
    )
    assert branch
    committed = git_run(["show", "--name-only", "--format=", branch], git_repo.clone)
    assert "agent_change.py" in committed
    # Prior artifacts archived; the fresh attempt's summary written at the top level.
    assert (external / "logs" / "task-700" / "attempt-1" / "plan.md").exists()
    assert (external / "logs" / "task-700" / "summary.md").exists()
    # The task file ended in done/, and the final state row is done.
    assert (project / "done" / "task-700.md").exists()
    store = StateStore.open_readonly(db)
    final_row = store.get_task("task-700")
    store.close()
    assert final_row is not None and final_row.status is Status.DONE


# --- guards / refusals (no pipeline run) -------------------------------------------------


def _seed(
    project: Path,
    clone: Path,
    row: TaskRow,
    *,
    checkpoint_node: str | None = None,
    node_run: tuple[str, str] | None = None,
    counters_json: str = "{}",
    flow_fingerprint: str | None = None,
) -> Path:
    config = _write_config(project, clone, claude="claude", codex="codex")
    db = clone / ".worc" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(row)
    if node_run is not None:  # (node_id, node_kind) for the interrupted node — F14 reads node_kind
        from wastech_orchestrator.state_store import NodeRunRow

        node_id, node_kind = node_run
        store.record_node_run(
            NodeRunRow(
                task_id=row.task_id,
                node_id=node_id,
                node_kind=node_kind,
                subtask_order=None,
                status="failed",
                started_at="2026-01-01T00:00:00+00:00",
            )
        )
    if checkpoint_node is not None:  # an interrupted engine task: a flow checkpoint to resume from
        from tests.conftest import BUILTIN_FLOWS_DIR

        from wastech_orchestrator.core.flow.registry import FlowRegistry

        registry = FlowRegistry(operator_flows_dir=BUILTIN_FLOWS_DIR)
        fingerprint = flow_fingerprint or registry.resolve("implementation").flow_fingerprint
        store.save_flow_checkpoint(
            row.task_id,
            current_node=checkpoint_node,
            counters_json=counters_json,
            flow_fingerprint=fingerprint,
            fix_iterations=row.fix_iterations,  # checkpoint mirrors the seeded task's fix counter
        )
    store.close()
    return config


def _seed_manifest(clone: Path, task_id: str, *, task_type: str | None = None) -> None:
    """Write the persisted normalized manifest a ``--from`` rerun reads to resolve the flow."""
    from wastech_orchestrator.task.model import NormalizedTask
    from wastech_orchestrator.task.parser import write_normalized

    write_normalized(
        NormalizedTask(id=task_id, title="T", description="x", task_type=task_type),
        clone / ".worc",
    )


def test_rerun_refuses_unknown_id(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED))
    code = cli.main(["--config", str(config), "rerun", "task-missing"])
    assert code == 1
    assert "unknown task id" in capsys.readouterr().out


def test_rerun_refuses_non_terminal_task(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.RUNNING))
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "is running" in capsys.readouterr().out


def test_rerun_refuses_when_daemon_running(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED))
    monkeypatch.setattr(cli.process_control, "running_daemon_pid", lambda _p: 4321)
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "watch daemon is running" in capsys.readouterr().out


def test_rerun_resolves_task_file_moved_between_lifecycle_folders(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The stored source_path points at tasks/failed/, but the file now lives in tasks/pending/
    # (a manual/external move). The resolver finds it by id across lifecycle folders instead of
    # refusing — the main "failed → rerun" barrier.
    project = tmp_path / "project"
    project.mkdir()
    actual = project / "pending" / "task-1.md"
    _complete_task_file(actual, "task-1")
    stale = project / "failed" / "task-1.md"  # recorded path, but no file there
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(stale))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "would re-attempt task-1" in out
    assert "source file is missing" not in out


def test_rerun_refuses_when_task_file_ambiguous(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two files match the id across lifecycle folders → never guessed; the rerun refuses and names
    # both so the operator leaves exactly one.
    project = tmp_path / "project"
    project.mkdir()
    _complete_task_file(project / "pending" / "task-1.md", "task-1")
    _complete_task_file(project / "done" / "task-1.md", "task-1")
    stale = project / "failed" / "task-1.md"
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(stale))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "ambiguous" in capsys.readouterr().out


def test_rerun_refuses_when_task_file_truly_missing(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No file matches the id in any lifecycle folder → the original "missing" refusal.
    project = tmp_path / "project"
    project.mkdir()
    stale = project / "failed" / "task-1.md"  # never created anywhere
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(stale))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "source file is missing" in capsys.readouterr().out


def test_rerun_refuses_fresh_in_operator_owned_branch_mode(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # branch-mode ADR: a fresh rerun resets the branch to base — forbidden in existing/current mode,
    # where the branch is the operator's. The refusal directs them to `rerun --continue` instead.
    from wastech_orchestrator.config.schema import BranchMode
    from wastech_orchestrator.task.model import NormalizedTask
    from wastech_orchestrator.task.parser import write_normalized

    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="feature/keep"),
    )
    # The rerun guard reads the effective branch mode from the persisted normalized manifest.
    write_normalized(
        NormalizedTask(
            id="task-1",
            title="T",
            description="x",
            branch_mode=BranchMode.EXISTING,
            branch_ref="feature/keep",
        ),
        git_repo.clone / ".worc",
    )
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    out = capsys.readouterr().out
    assert code == 1
    assert "branch_mode 'existing'" in out and "operator-owned" in out
    assert "rerun --continue" in out


def test_rerun_dry_run_writes_nothing(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source)),
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    # Nothing changed: still failed, no ledger record appended.
    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    row = store.get_task("task-1")
    store.close()
    assert row is not None and row.status is Status.FAILED
    assert _ledger_records(git_repo.clone) == []


# --- continue mode -----------------------------------------------------------------------


def test_rerun_continue_refuses_without_recoverable_stage(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    # No flow checkpoint recorded -> --continue cannot know which node to re-enter.
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(source))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue"])
    assert code == 1
    assert "no recoverable node" in capsys.readouterr().out


def test_rerun_continue_in_publish_allows_uncommitted_code(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F14: when ``commit_code`` failed inside the publish node, the agent's code is left
    uncommitted in the working tree. ``rerun --continue`` re-enters publish (commit_code is
    idempotent and docommits it), so that dirty state must NOT be refused as "unaccounted changes".
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.MANUAL_ACTION_REQUIRED,
            source_path=str(source),
            branch="worc/task-1-t",
            finished_at="2026-01-01T00:00:00+00:00",
        ),
        checkpoint_node="publish",
        node_run=("publish", "publish"),
    )
    # Leave uncommitted code in the working tree (the failed publish's staged-but-uncommitted work).
    (git_repo.clone / "feature.py").write_text("print('shipped')\n", encoding="utf-8")

    def fake_resume(self: Orchestrator) -> PipelineResult:
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 0  # not refused for a dirty tree; --continue proceeds into publish


def test_rerun_continue_at_review_tolerates_task_wip(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Capability #3: re-entering review/fixing on ``--continue`` tolerates the task's own
    uncommitted work — the staged implementation is the legitimate input to those nodes (an
    evaluator has already run, so a dirty tree is the task's output, not foreign). It is not
    refused, and the operator is warned that the WIP will be committed into the task.
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.FAILED,
            source_path=str(source),
            branch="worc/task-1-t",
        ),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
    )
    (git_repo.clone / "feature.py").write_text("print('wip')\n", encoding="utf-8")

    def fake_resume(self: Orchestrator) -> PipelineResult:
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    out = capsys.readouterr().out
    assert code == 0  # not refused for a dirty tree; --continue proceeds into review
    assert "will be committed into the task" in out


def test_rerun_continue_pre_edit_node_still_refuses_dirty_tree(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: before the task has produced code (only a pre-edit agent node ran; no
    evaluator/checks/publish), a dirty tree is almost certainly foreign and is still refused — the
    WIP tolerance does not blanket-accept every ``--continue``.
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.FAILED,
            source_path=str(source),
            branch="worc/task-1-t",
        ),
        checkpoint_node="planning",
        node_run=("planning", "agent"),
    )
    (git_repo.clone / "stray.py").write_text("x = 1\n", encoding="utf-8")
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 1
    assert "unaccounted changes" in capsys.readouterr().out


def test_rerun_continue_revives_then_delegates_to_resume(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.FAILED,
            source_path=str(source),
            branch="worc/task-1-t",
            fix_iterations=2,
            finished_at="2026-01-01T00:00:00+00:00",
            cleanup_completed=True,
        ),
        checkpoint_node="review",  # the engine checkpoint --continue re-enters at
    )

    calls = {"resume": 0}

    def fake_resume(self: Orchestrator) -> PipelineResult:
        calls["resume"] += 1
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 0  # the stubbed resume returns done
    assert calls["resume"] == 1  # continue delegated to the resume engine

    # The row was revived to the failed stage with the terminal markers cleared, work preserved.
    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    row = store.get_task("task-1")
    store.close()
    assert row is not None
    assert row.status is Status.RUNNING  # revived as active; resume re-enters at the checkpoint
    assert row.finished_at is None and row.cleanup_completed is None
    assert row.branch == "worc/task-1-t" and row.fix_iterations == 2


# --- Phase 0 #2: fresh fix budget on continue (--reset-fix-budget) -----------------------


def test_reset_fix_budget_requires_continue(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--reset-fix-budget is a continue-only control; on a fresh rerun it is refused."""
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="worc/task-1-t"),
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--reset-fix-budget", "--yes"])
    assert code == 1
    assert "--reset-fix-budget requires --continue" in capsys.readouterr().out


def test_reset_fix_budget_resets_consecutive_keeps_global(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A max_fix_cycles-saturated task gets fresh consecutive budget: the bare loop counter is
    dropped, but the global fix counter and the cumulative total are preserved so the
    max_total_fix_iterations backstop is never weakened.
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    counters = json.dumps(
        {"review_fix": 15, "total_fix:review_fix": 15, "global_fix_iterations": 15}
    )
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.MANUAL_ACTION_REQUIRED,
            source_path=str(source),
            branch="worc/task-1-t",
            fix_iterations=15,
            review_fix_cycles=15,
        ),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
        counters_json=counters,
    )

    def fake_resume(self: Orchestrator) -> PipelineResult:  # isolate the checkpoint transformation
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(
        ["--config", str(config), "rerun", "task-1", "--continue", "--reset-fix-budget", "--yes"]
    )
    assert code == 0

    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    _node, counters_json, _fp = store.get_flow_checkpoint("task-1")
    persisted = json.loads(counters_json or "{}")
    row = store.get_task("task-1")
    store.close()
    assert "review_fix" not in persisted  # consecutive counter reset (below the cap again)
    assert persisted["total_fix:review_fix"] == 15  # cumulative audit total preserved
    assert persisted["global_fix_iterations"] == 15  # global backstop untouched
    assert row is not None
    assert row.fix_iterations == 15  # mirror keeps the global; only *_cycles zeroed
    assert row.review_fix_cycles == 0


def test_reset_fix_budget_warns_when_global_backstop_exhausted(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the global max_total_fix_iterations backstop is already spent, the dry-run warns rather
    than silently running one cycle and re-failing.
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    counters = json.dumps({"review_fix": 30, "global_fix_iterations": 30})
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.MANUAL_ACTION_REQUIRED,
            source_path=str(source),
            branch="worc/task-1-t",
            fix_iterations=30,
        ),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
        counters_json=counters,
    )
    _seed_manifest(git_repo.clone, "task-1")
    code = cli.main(
        [
            "--config",
            str(config),
            "rerun",
            "task-1",
            "--continue",
            "--reset-fix-budget",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0  # dry-run, no refusal
    assert "the global max_total_fix_iterations backstop is exhausted" in out


# --- Phase 1 #4: re-enter at a chosen node (--from <node>) --------------------------------


def test_rerun_from_requires_continue(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from is a continue-only control; on a fresh rerun it is refused."""
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="worc/task-1-t"),
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--from", "review", "--yes"])
    assert code == 1
    assert "--from requires --continue" in capsys.readouterr().out


def test_rerun_from_unknown_node_refuses(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from must name a node in the checkpoint's flow; an unknown node is refused w/ the list."""
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="worc/task-1-t"),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
    )
    _seed_manifest(git_repo.clone, "task-1")
    code = cli.main(
        [
            "--config",
            str(config),
            "rerun",
            "task-1",
            "--continue",
            "--from",
            "nonesuch",
            "--dry-run",
        ]
    )
    assert code == 1
    assert "is not in the current flow" in capsys.readouterr().out


def test_rerun_from_drift_note_surfaced_in_dry_run(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from proceeds through drift as long as the target node still exists — no refusal, just a
    transparency note (the node existing is the only thing --from checks; the operator invoking it
    is trusted to know what they changed).
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="worc/task-1-t"),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
        flow_fingerprint="stale-fingerprint",
    )
    _seed_manifest(git_repo.clone, "task-1")
    code = cli.main(
        [
            "--config",
            str(config),
            "rerun",
            "task-1",
            "--continue",
            "--from",
            "implementation",
            "--dry-run",
        ]
    )
    assert code == 0  # no refusal — the node exists; drift alone never blocks --from
    out = capsys.readouterr().out
    assert "the flow changed since the checkpoint" in out
    assert "will resume using the current on-disk flow" in out


def test_rerun_from_drift_actually_resumes_at_node_and_rebaselines_fingerprint(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the fingerprint-rebaseline fix: --from under drift must actually resume
    at the target node instead of the engine's own resume gate silently top-restarting (which would
    happen if the checkpoint's stale fingerprint were left untouched), and the persisted fingerprint
    is updated to the live flow's.
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="worc/task-1-t"),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
        flow_fingerprint="stale-fingerprint",
    )
    _seed_manifest(git_repo.clone, "task-1")

    def fake_resume(self: Orchestrator) -> PipelineResult:
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(
        [
            "--config",
            str(config),
            "rerun",
            "task-1",
            "--continue",
            "--from",
            "implementation",
            "--yes",
        ]
    )
    assert code == 0

    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    current_node, _counters, fingerprint = store.get_flow_checkpoint("task-1")
    store.close()
    assert current_node == "implementation"  # checkpoint re-pointed at the --from node
    assert fingerprint != "stale-fingerprint"  # rebaselined, not round-tripped

    from tests.conftest import BUILTIN_FLOWS_DIR

    from wastech_orchestrator.core.flow.registry import FlowRegistry

    registry = FlowRegistry(operator_flows_dir=BUILTIN_FLOWS_DIR)
    assert fingerprint == registry.resolve("implementation").flow_fingerprint


def test_rerun_from_valid_node_overrides_checkpoint(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--from a valid node rewrites the persisted checkpoint so resume re-enters there."""
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="worc/task-1-t"),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
    )
    _seed_manifest(git_repo.clone, "task-1")

    def fake_resume(self: Orchestrator) -> PipelineResult:
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(
        [
            "--config",
            str(config),
            "rerun",
            "task-1",
            "--continue",
            "--from",
            "implementation",
            "--yes",
        ]
    )
    assert code == 0

    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    current_node, _counters, _fp = store.get_flow_checkpoint("task-1")
    store.close()
    assert current_node == "implementation"  # checkpoint re-pointed at the --from node


# --- exhausted fix-budget interactive confirmation ----------------------------------------


def _seed_exhausted_task(
    project: Path, clone: Path, *, review_fix: int = 15, global_fix: int = 0
) -> Path:
    """A manual_action_required task parked at 'review' with the given loop/global counters."""
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    counters = json.dumps({"review_fix": review_fix, "global_fix_iterations": global_fix})
    config = _seed(
        project,
        clone,
        TaskRow(
            "task-1",
            "T",
            Status.MANUAL_ACTION_REQUIRED,
            source_path=str(source),
            branch="worc/task-1-t",
            fix_iterations=global_fix,
            review_fix_cycles=review_fix,
        ),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
        counters_json=counters,
    )
    _seed_manifest(clone, "task-1")
    return config


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _forbid_continue_task(*_args: object, **_kwargs: object) -> PipelineResult:
    raise AssertionError("continue_task should not be called when the budget decision refuses")


def test_exhausted_budget_prompt_yes_resets_and_resumes(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming into an exhausted review_fix loop prompts; answering yes resets it and resumes."""
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(cli, "_confirm", lambda _prompt: True)

    def fake_resume(self: Orchestrator) -> PipelineResult:
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 0

    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    _node, counters_json, _fp = store.get_flow_checkpoint("task-1")
    store.close()
    persisted = json.loads(counters_json or "{}")
    assert "review_fix" not in persisted  # the exhausted loop's consecutive counter was reset


def test_exhausted_budget_prompt_no_refuses(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining the prompt refuses cleanly, naming the exhausted budget, before any resume."""
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(cli, "_confirm", lambda _prompt: False)
    monkeypatch.setattr(Orchestrator, "continue_task", _forbid_continue_task)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 1
    assert "review_fix" in capsys.readouterr().out


def test_exhausted_budget_non_interactive_without_flag_refuses(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-TTY stdin with neither flag fails closed, naming the flags to pass instead of hanging
    on an interactive prompt that could never be answered."""
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # not a TTY

    def _forbid_confirm(_prompt: str) -> bool:
        raise AssertionError("_confirm should not be called on a non-interactive stdin")

    monkeypatch.setattr(cli, "_confirm", _forbid_confirm)
    monkeypatch.setattr(Orchestrator, "continue_task", _forbid_continue_task)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 1
    out = capsys.readouterr().out
    assert "non-interactive shell" in out
    assert "--reset-fix-budget" in out and "--no-reset-fix-budget" in out


def test_reset_fix_budget_flag_skips_prompt_when_exhausted(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--reset-fix-budget answers the exhausted-budget question non-interactively; no prompt."""
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)

    def _forbid_confirm(_prompt: str) -> bool:
        raise AssertionError("--reset-fix-budget must not prompt")

    monkeypatch.setattr(cli, "_confirm", _forbid_confirm)

    def fake_resume(self: Orchestrator) -> PipelineResult:
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(
        ["--config", str(config), "rerun", "task-1", "--continue", "--reset-fix-budget", "--yes"]
    )
    assert code == 0


def test_no_reset_fix_budget_flag_refuses_without_prompt(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-reset-fix-budget answers the question non-interactively too: refuse, no prompt."""
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)

    def _forbid_confirm(_prompt: str) -> bool:
        raise AssertionError("--no-reset-fix-budget must not prompt")

    monkeypatch.setattr(cli, "_confirm", _forbid_confirm)
    monkeypatch.setattr(Orchestrator, "continue_task", _forbid_continue_task)
    code = cli.main(
        [
            "--config",
            str(config),
            "rerun",
            "task-1",
            "--continue",
            "--no-reset-fix-budget",
            "--yes",
        ]
    )
    assert code == 1
    assert "review_fix" in capsys.readouterr().out


def test_yes_flag_does_not_satisfy_budget_prompt(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--yes alone (no reset flag) never substitutes for the budget decision — a non-interactive
    shell with only --yes still refuses and asks for an explicit flag."""
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # not a TTY
    monkeypatch.setattr(Orchestrator, "continue_task", _forbid_continue_task)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 1
    assert "non-interactive shell" in capsys.readouterr().out


def test_futile_reset_when_global_backstop_exhausted_errors(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reset that would not actually unblock the task (the global backstop is itself exhausted)
    surfaces its own clear error instead of silently resetting and re-parking a second time — even
    when no *named* loop is individually over cap.
    """
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=3, global_fix=30)
    monkeypatch.setattr(Orchestrator, "continue_task", _forbid_continue_task)
    code = cli.main(
        ["--config", str(config), "rerun", "task-1", "--continue", "--reset-fix-budget", "--yes"]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "global max_total_fix_iterations backstop is a hard ceiling" in out


def test_reset_and_no_reset_flags_mutually_exclusive(git_repo, tmp_path: Path) -> None:
    """--reset-fix-budget and --no-reset-fix-budget together is an argparse usage error."""
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "--config",
                str(config),
                "rerun",
                "task-1",
                "--continue",
                "--reset-fix-budget",
                "--no-reset-fix-budget",
                "--yes",
            ]
        )
    assert exc.value.code == 2


def test_exhausted_loop_downstream_of_from_target_is_caught(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from an upstream node still surfaces a loop already exhausted further down the resume
    path (the broad forward-reachable scope), not just the target node's own edge.
    """
    project = tmp_path / "project"
    project.mkdir()
    config = _seed_exhausted_task(project, git_repo.clone, review_fix=15)
    code = cli.main(
        [
            "--config",
            str(config),
            "rerun",
            "task-1",
            "--continue",
            "--from",
            "implementation",
            "--dry-run",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "review_fix" in out and "exhausted" in out
