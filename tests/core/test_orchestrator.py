"""Integration tests for the Orchestrator Core pipeline (§5, §8).

These drive the real Router + Git Manager (temp repo) + Check Runner, with fake in-memory providers
and a fake check process, exercising the full state machine, loops, decomposition, summary fallback,
publishing, terminal cleanup, and the ledger.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.core.loop_control import LoopController
from wastech_orchestrator.core.orchestrator import Orchestrator, SlotBusyError
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import GitManager, GitResult
from wastech_orchestrator.ledger import Ledger
from wastech_orchestrator.notify import AskKind, AskResult, Notifier
from wastech_orchestrator.providers.artifacts import create_attempt_dir
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    ProviderError,
    ProviderHealth,
    ProviderId,
    RunStatus,
    Stage,
)
from wastech_orchestrator.state_store import StateStore, TaskRow
from wastech_orchestrator.task.validation_gate import ValidationGate


class FakeProvider:
    """An in-memory AgentProvider returning scripted results, used to drive the Core."""

    def __init__(
        self,
        provider_id: str,
        *,
        outputs: dict[Stage, tuple[str, dict | None]] | None = None,
        infra_fail: set[Stage] | None = None,
    ) -> None:
        self.id = provider_id
        self._outputs = outputs or {}
        self._infra_fail = infra_fail or set()

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version="1",
            authenticated=True,
            supports_required_features=True,
            message="ok",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        if request.stage in self._infra_fail:
            raise ProviderError(error_class=ErrorClass.TIMEOUT, message="infra fail")
        message, structured = self._outputs.get(request.stage, ("done", None))
        return AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider=self.id,
            stage=request.stage,
            attempt=request.attempt,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            final_message=message,
            structured_output=structured,
        )


class ArtifactWritingProvider(FakeProvider):
    """Provider fake that exercises the production artifact directory allocator."""

    def __init__(self, provider_id: str, artifacts_root: Path) -> None:
        super().__init__(provider_id)
        self._artifacts_root = artifacts_root

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        paths = create_attempt_dir(
            self._artifacts_root,
            request.task_id,
            request.stage,
            request.attempt,
            self.id,
            stage_run_id=request.stage_run_id,
        )
        result = super().run(request)
        Path(paths.stdout_path).write_text("provider output\n", encoding="utf-8")
        return replace(result, stdout_path=paths.stdout_path)


class RecordingNotifier:
    """Core-facing notifier fake that records terminal messages without network access."""

    def __init__(self, *, raise_on_send: bool = False) -> None:
        self.calls: list[dict[str, str | None]] = []
        self._raise_on_send = raise_on_send

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
    ) -> None:
        self.calls.append(
            {
                "task_id": task_id,
                "final_status": final_status,
                "pr_url": pr_url,
                "reason": reason,
            }
        )
        if self._raise_on_send:
            raise RuntimeError("notification failed")

    def ask_human(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
    ) -> AskResult:
        return AskResult(answered=False, timed_out=True)


def _fake_proc(verdicts: list[int]) -> Callable[..., object]:
    """A run_process stand-in for the Check Runner; pops one exit code per call (repeats last)."""
    from wastech_orchestrator.providers.process import ProcessResult

    state = {"i": 0}

    def run(argv: Sequence[str], *, cwd, env, timeout_seconds, stdout_path, stdin_text=None):
        Path(stdout_path).write_text("check\n", encoding="utf-8")
        idx = min(state["i"], len(verdicts) - 1)
        state["i"] += 1
        code = verdicts[idx]
        return ProcessResult(
            exit_code=code,
            timed_out=False,
            launch_error=None,
            duration_seconds=0.0,
            stdout_path=str(stdout_path),
            stderr_text="",
        )

    return run


def _fake_gh() -> Callable[[Sequence[str]], GitResult]:
    def gh(argv: Sequence[str]) -> GitResult:
        return GitResult(
            exit_code=0,
            stdout="https://example/pr/1\n",
            stderr="",
            timed_out=False,
            launch_error=None,
        )

    return gh


def _build(
    git_repo,
    make_git_config,
    tmp_path: Path,
    *,
    providers: dict[ProviderId, FakeProvider],
    check_verdicts: list[int],
    config_kwargs: dict | None = None,
    notifier: Notifier | None = None,
) -> tuple[Orchestrator, StateStore, Ledger, Path]:
    from wastech_orchestrator.routing.router import AgentRouter

    art = tmp_path / "art"
    config = make_git_config(git_repo.clone, checks=["pytest"], **(config_kwargs or {}))
    store = StateStore.open(art / "state.db")
    ledger = Ledger(art / "logs")
    router = AgentRouter(config, providers)  # type: ignore[arg-type]
    git = GitManager(config, store=store, artifacts_root=str(art), gh_runner=_fake_gh())
    checks = CheckRunner(config, run_process=_fake_proc(check_verdicts))  # type: ignore[arg-type]
    gate = ValidationGate(
        config,
        store_has_task_id=store.task_id_exists,
        ledger_has_task_id=ledger.has_task_id,
    )
    orch = Orchestrator(
        config,
        router=router,
        git=git,
        checks=checks,
        store=store,
        ledger=ledger,
        loops=LoopController(config.agents),
        gate=gate,
        artifacts_root=str(art),
        notifier=notifier,
    )
    return orch, store, ledger, art


def _complete_task(tmp_path: Path, task_id: str = "task-001") -> str:
    path = tmp_path / f"{task_id}.md"
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\nrefined: true\n---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    return str(path)


def _both(**kwargs) -> dict[ProviderId, FakeProvider]:
    return {
        ProviderId.CLAUDE: FakeProvider("claude", **kwargs),
        ProviderId.CODEX: FakeProvider("codex", **kwargs),
    }


def _impl_writes_file(provider_id: str) -> FakeProvider:
    # Implementation must actually change the working tree so there is something to commit.
    return FakeProvider(provider_id, outputs={Stage.IMPLEMENTATION: ("implemented", None)})


def test_happy_path_complete_task(git_repo, make_git_config, git_run, tmp_path: Path) -> None:
    providers = _both()
    notifier = RecordingNotifier()
    orch, store, ledger, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )
    task_file = _complete_task(tmp_path)
    # The implementation stage must leave a change to commit; simulate by writing in the clone.
    # We patch the provider run for implementation to create a file via a side effect.
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE
    assert result.pr_url == "https://example/pr/1"

    row = store.get_task("task-001")
    assert row is not None and row.status is Status.DONE
    assert row.refinement_ran is False  # refined: true → skipped
    # The task file was moved into its lifecycle folder (tasks/done, §20.2).
    assert (tmp_path / "done" / "task-001.md").exists()
    assert not (tmp_path / "task-001.md").exists()
    # Exactly one ledger record; back on the base branch.
    records = ledger.records()
    assert len(records) == 1 and records[0]["final_status"] == "done"
    assert notifier.calls == [
        {
            "task_id": "task-001",
            "final_status": "done",
            "pr_url": "https://example/pr/1",
            "reason": None,
        }
    ]
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # The commit landed on the task branch.
    branches = git_run(["branch", "--list", "agent/task-001-add-a-thing"], git_repo.clone)
    assert "agent/task-001-add-a-thing" in branches


def test_vague_task_runs_refinement(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    path = tmp_path / "task-002.md"
    path.write_text(
        '---\nid: task-002\ntitle: "Vague"\n---\n\n## Description\n\nMake it better.\n',
        encoding="utf-8",
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "f.py").write_text("y = 2\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(str(path))
    assert result.final_status is Status.DONE
    row = store.get_task("task-002")
    assert row is not None and row.refinement_ran is True
    assert (art / "logs" / "task-002" / "task.enriched.md").exists()


def test_failed_checks_then_fix_then_pass(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    # Checks fail once, then pass on the retry after fixing.
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[1, 0]
    )
    task_file = _complete_task(tmp_path, "task-003")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage in (Stage.IMPLEMENTATION, Stage.FIXING):
            (git_repo.clone / "f.py").write_text("z = 3\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE
    row = store.get_task("task-003")
    assert row is not None and row.fix_iterations == 1  # one fixing entry
    context = json.loads(
        (art / "logs" / "task-003" / "fixing-context.json").read_text(encoding="utf-8")
    )
    assert context["loop"] == "test"
    assert context["check_artifacts_path"]


def test_two_fix_cycles_use_distinct_stage_run_artifacts(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    artifacts_root = tmp_path / "art"
    providers: dict[ProviderId, FakeProvider] = {
        ProviderId.CLAUDE: ArtifactWritingProvider("claude", artifacts_root),
        ProviderId.CODEX: ArtifactWritingProvider("codex", artifacts_root),
    }
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[1, 1, 0],
        config_kwargs={"max_fix_cycles": 3, "max_total_fix_iterations": 10},
    )
    task_file = _complete_task(tmp_path, "task-two-fixes")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage in (Stage.IMPLEMENTATION, Stage.FIXING):
            (git_repo.clone / "f.py").write_text(
                f"stage_run_id = {request.stage_run_id}\n", encoding="utf-8"
            )
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)

    assert result.final_status is Status.DONE
    rows = store._conn.execute(  # noqa: SLF001 - cross-checking SQLite against artifact paths
        "SELECT id FROM stage_runs WHERE task_id = ? AND stage = ? ORDER BY id",
        ("task-two-fixes", Stage.FIXING.value),
    ).fetchall()
    assert len(rows) == 2
    expected = [
        art
        / "logs"
        / "task-two-fixes"
        / "stages"
        / "fixing"
        / f"run-{row['id']:06d}"
        / "1-claude"
        for row in rows
    ]
    assert all(path.is_dir() for path in expected)
    assert expected[0] != expected[1]


def test_fix_budget_exhausted_is_manual(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    notifier = RecordingNotifier()
    # Checks always fail → the test-driven fix loop hits max_fix_cycles.
    orch, store, ledger, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[1],
        config_kwargs={"max_fix_cycles": 2, "max_total_fix_iterations": 10},
        notifier=notifier,
    )
    task_file = _complete_task(tmp_path, "task-004")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage in (Stage.IMPLEMENTATION, Stage.FIXING):
            (git_repo.clone / "f.py").write_text("w = 4\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert (art / "logs" / "task-004" / "failure_report.json").exists()
    assert (art / "logs" / "task-004" / "stuck.md").exists()
    assert ledger.records()[0]["final_status"] == "manual_action_required"
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["final_status"] == "manual_action_required"


def test_review_blocking_then_fix(git_repo, make_git_config, tmp_path: Path) -> None:
    # Review returns a blocking finding the first time, none the second.
    review_outputs = [
        ("blocking found", {"findings": [{"title": "bug", "severity": "blocking"}]}),
        ("clean", {"findings": []}),
    ]
    state = {"i": 0}

    class ReviewProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.stage is Stage.IMPLEMENTATION:
                (git_repo.clone / "r.py").write_text("a = 1\n", encoding="utf-8")
            if request.stage is Stage.REVIEW:
                msg, structured = review_outputs[min(state["i"], 1)]
                state["i"] += 1
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    stage=request.stage,
                    attempt=request.attempt,
                    exit_code=0,
                    started_at="t",
                    finished_at="t",
                    final_message=msg,
                    structured_output=structured,
                )
            return super().run(request)

    providers = {
        ProviderId.CLAUDE: ReviewProvider("claude"),
        ProviderId.CODEX: ReviewProvider("codex"),
    }
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    result = orch.run_task(_complete_task(tmp_path, "task-005"))
    assert result.final_status is Status.DONE
    row = store.get_task("task-005")
    assert row is not None and row.fix_iterations == 1  # one review-driven fix
    context = json.loads(
        (art / "logs" / "task-005" / "fixing-context.json").read_text(encoding="utf-8")
    )
    assert context["loop"] == "review"
    assert context["review_artifacts_path"]


def test_summary_fallback_when_provider_fails(git_repo, make_git_config, tmp_path: Path) -> None:
    # Both providers fail the summary stage with an infra error → minimal summary, still DONE.
    providers = _both(infra_fail={Stage.SUMMARY})
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    task_file = _complete_task(tmp_path, "task-006")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "s.py").write_text("b = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE  # summary failure never blocks (§5.2)
    summary = (art / "logs" / "task-006" / "summary.md").read_text(encoding="utf-8")
    assert "## What" in summary


def test_decomposed_task_commits_each_subtask(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    subtasks = {
        "decompose": True,
        "subtasks": [
            {
                "order": 1,
                "title": "First",
                "slug": "first",
                "acceptance_criteria": ["a"],
                "depends_on": [],
            },
            {
                "order": 2,
                "title": "Second",
                "slug": "second",
                "acceptance_criteria": ["b"],
                "depends_on": [1],
            },
        ],
    }

    class DecompProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.stage is Stage.PLANNING:
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    stage=request.stage,
                    attempt=request.attempt,
                    exit_code=0,
                    started_at="t",
                    finished_at="t",
                    final_message="plan",
                    structured_output=subtasks,
                )
            if request.stage is Stage.IMPLEMENTATION:
                # Each subtask writes a distinct file so each commit is non-empty.
                marker = git_repo.clone / f"impl-{state['n']}.py"
                marker.write_text("x\n", encoding="utf-8")
                state["n"] += 1
            return super().run(request)

    state = {"n": 0}
    providers = {
        ProviderId.CLAUDE: DecompProvider("claude"),
        ProviderId.CODEX: DecompProvider("codex"),
    }
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"decomposition": True},
    )
    result = orch.run_task(_complete_task(tmp_path, "task-007"))
    assert result.final_status is Status.DONE
    row = store.get_task("task-007")
    assert row is not None
    assert row.decomposition_accepted is True
    assert row.subtask_count == 2
    assert row.subtasks_completed == 2
    # Two subtask commits + the final (empty) code commit guard; assert at least 2 new commits.
    branch = "agent/task-007-add-a-thing"
    count = git_run(["rev-list", "--count", f"main..{branch}"], git_repo.clone)
    assert int(count) >= 2
    subs = store.get_subtasks("task-007")
    assert all(s.commit_sha for s in subs)


def test_single_active_slot_blocks(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    # Pre-seed another active task occupying the slot.
    store.insert_task(TaskRow(task_id="other", title="o", status=Status.IMPLEMENTING))
    with pytest.raises(SlotBusyError):
        orch.run_task(_complete_task(tmp_path, "task-008"))


def test_rejected_task_no_branch(git_repo, make_git_config, git_run, tmp_path: Path) -> None:
    quarantine = tmp_path / "rejected"
    notifier = RecordingNotifier()
    orch, store, ledger, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        config_kwargs={"quarantine": str(quarantine)},
        notifier=notifier,
    )
    bad = tmp_path / "task-009.md"
    bad.write_text("no front matter at all\n", encoding="utf-8")
    result = orch.run_task(str(bad))
    assert result.final_status is Status.FAILED
    assert result.validation_reason == "frontmatter_missing"
    assert (art / "logs" / "task-009" / "validation_report.json").exists()
    # No branch was created and the file was quarantined.
    assert (quarantine / "task-009.md").exists()
    branches = git_run(["branch", "--list", "agent/*"], git_repo.clone)
    assert branches == ""
    assert ledger.records()[0]["validation_reason"] == "frontmatter_missing"
    assert notifier.calls == [
        {
            "task_id": "task-009",
            "final_status": "failed",
            "pr_url": None,
            "reason": "frontmatter_missing",
        }
    ]


def test_notifier_exception_does_not_change_terminal_outcome(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    notifier = RecordingNotifier(raise_on_send=True)
    orch, _, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        notifier=notifier,
    )
    bad = tmp_path / "task-notify-fail.md"
    bad.write_text("no front matter at all\n", encoding="utf-8")

    result = orch.run_task(str(bad))

    assert result.final_status is Status.FAILED
    assert ledger.records()[0]["final_status"] == "failed"
    assert len(notifier.calls) == 1
    assert (tmp_path / "rejected" / "task-notify-fail.md").exists()
    assert not (Path.cwd() / "tasks" / "rejected" / "task-notify-fail.md").exists()


# --- Phase 6: security & observability (spec §6.1/§6.5) -------------------------------------------


def test_strict_isolation_preflight_fails_without_branch(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # When strict_isolation cannot be guaranteed, the task fails BEFORE a branch is created (§12.8).
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    monkeypatch.setattr(
        "wastech_orchestrator.core.orchestrator.check_isolation",
        lambda _config: ["codex: sandbox 'danger-full-access' grants full filesystem access"],
    )
    result = orch.run_task(_complete_task(tmp_path, "task-iso"))

    assert result.final_status is Status.FAILED
    row = store.get_task("task-iso")
    assert row is not None and row.status is Status.FAILED
    assert git_run(["branch", "--list", "agent/*"], git_repo.clone) == ""  # no branch created
    assert ledger.records()[0]["final_status"] == "failed"


def test_failed_with_branch_commits_and_pushes_task_and_summary(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # Both providers fail (infra) at implementation, AFTER the branch exists → FAILED. The failed
    # attempt is finalized like a success: the task moves to tasks/failed/, its summary.md is
    # committed, and the branch is pushed — but no PR is opened for a failure (§6).
    providers = _both(infra_fail={Stage.IMPLEMENTATION})
    orch, store, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"location": "in_repo", "tracking": "commit"},
    )
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True)
    task_file = pending / "task-fail.md"
    task_body = (
        '---\nid: task-fail\ntitle: "Add a thing"\nrefined: true\n---\n\n## Description\n\nDo it.\n'
    )
    task_file.write_text(task_body, encoding="utf-8")

    result = orch.run_task(str(task_file))

    assert result.final_status is Status.FAILED
    assert result.pr_url is None  # no PR for a failed attempt
    branch = "agent/task-fail-add-a-thing"
    tracked = git_run(["ls-tree", "-r", "--name-only", branch], git_repo.clone)
    assert "tasks/failed/task-fail.md" in tracked  # task moved to failed/ and committed
    assert "tasks/failed/task-fail.summary.md" in tracked  # summary committed beside it
    assert "logs/" not in tracked  # working artifacts never enter git
    # The failed branch was pushed for inspection; the working copy is back on base.
    assert git_run(["ls-remote", "--heads", "origin", branch], git_repo.clone) != ""
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    assert ledger.records()[0]["final_status"] == "failed"


def test_artifacts_registered_with_checksums(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "feature.py").write_text("z = 9\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]
    orch.run_task(_complete_task(tmp_path, "task-art"))

    rows = store._conn.execute(  # noqa: SLF001
        "SELECT kind, checksum FROM artifacts WHERE task_id = ?", ("task-art",)
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert {"normalized", "validation_report", "plan", "diff", "summary_md"} <= kinds
    assert rows and all(len(r["checksum"]) == 64 for r in rows)  # sha256 hex digests


def test_decomposed_failure_report_has_subtask_fields(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A decomposed task that gets stuck in subtask 1 records its decomposition context (§10).
    subtasks = {
        "decompose": True,
        "subtasks": [
            {
                "order": 1,
                "title": "First",
                "slug": "first",
                "acceptance_criteria": ["a"],
                "depends_on": [],
            },
            {
                "order": 2,
                "title": "Second",
                "slug": "second",
                "acceptance_criteria": ["b"],
                "depends_on": [1],
            },
        ],
    }

    class DecompProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.stage is Stage.PLANNING:
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    stage=request.stage,
                    attempt=request.attempt,
                    exit_code=0,
                    started_at="t",
                    finished_at="t",
                    final_message="plan",
                    structured_output=subtasks,
                )
            if request.stage in (Stage.IMPLEMENTATION, Stage.FIXING):
                (git_repo.clone / "d.py").write_text("q = 1\n", encoding="utf-8")
            return super().run(request)

    providers = {
        ProviderId.CLAUDE: DecompProvider("claude"),
        ProviderId.CODEX: DecompProvider("codex"),
    }
    orch, _, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[1],
        config_kwargs={"decomposition": True, "max_fix_cycles": 2, "max_total_fix_iterations": 10},
    )
    result = orch.run_task(_complete_task(tmp_path, "task-dec"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    report = json.loads((art / "logs" / "task-dec" / "failure_report.json").read_text("utf-8"))
    assert report["decomposed"]["subtask_count"] == 2
    assert report["decomposed"]["failing_subtask"] == 1
    assert report["decomposed"]["subtasks_completed"] == 0
    assert report["decomposed"]["committed_shas"] == []
