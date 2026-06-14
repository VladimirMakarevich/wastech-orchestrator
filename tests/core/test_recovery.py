"""Tests for restart recovery (§13): reconciliation decisions and resume behavior."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.core.recovery import RecoveryAction, RecoveryReconciler
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult, Notifier
from wastech_orchestrator.providers.base import AgentRunRequest, ProviderId, Stage
from wastech_orchestrator.state_store import CheckRunRow, StateStore, SubtaskRow, TaskRow


class FakeGit:
    """A stand-in exposing only ``commit_on_branch`` for reconciliation unit tests."""

    def __init__(self, *, on_branch: bool = True) -> None:
        self._on_branch = on_branch
        self.queries: list[tuple[str, str]] = []

    def commit_on_branch(self, sha: str, branch: str) -> bool:
        self.queries.append((sha, branch))
        return self._on_branch


class RecordingNotifier:
    """Record terminal notification calls made while recovery reconciles state."""

    def __init__(self, ask_result: AskResult | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.ask_result = ask_result
        self.started: list[str] = []
        self.waited: list[AskHandle] = []

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
        contacts: tuple[str, ...] = (),
    ) -> None:
        self.calls.append((task_id, final_status))

    def start_ask(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
        interaction_id: str,
        contacts: tuple[str, ...] = (),
    ) -> AskHandle:
        self.started.append(interaction_id)
        return AskHandle(
            interaction_id=interaction_id,
            kind=kind,
            expires_at=0,
            delivered=False,
        )

    def wait_for_answer(self, handle: AskHandle) -> AskResult:
        self.waited.append(handle)
        if self.ask_result is not None:
            return AskResult(
                answered=self.ask_result.answered,
                text=self.ask_result.text,
                approved=self.ask_result.approved,
                timed_out=self.ask_result.timed_out,
                failure=self.ask_result.failure,
                interaction_id=handle.interaction_id,
                message_id=handle.message_id,
            )
        return AskResult(answered=False, failure="transport_error")

    def ask_human(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
        interaction_id: str = "adhoc",
        contacts: tuple[str, ...] = (),
    ) -> AskResult:
        return self.wait_for_answer(
            self.start_ask(
                question=question,
                context=context,
                task_id=task_id,
                kind=kind,
                timeout_s=timeout_s,
                interaction_id=interaction_id,
                contacts=contacts,
            )
        )


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.db")


def _reconciler(store: StateStore, make_git_config, git_repo, *, on_branch: bool = True):
    config = make_git_config(git_repo.clone)
    return RecoveryReconciler(config, store, FakeGit(on_branch=on_branch))  # type: ignore[arg-type]


def test_no_active_task_is_none(store, make_git_config, git_repo) -> None:
    plan = _reconciler(store, make_git_config, git_repo).reconcile()
    assert plan.action is RecoveryAction.NONE


def test_one_active_task_resumes(store, make_git_config, git_repo) -> None:
    store.insert_task(TaskRow(task_id="t1", title="t", status=Status.IMPLEMENTING))
    plan = _reconciler(store, make_git_config, git_repo).reconcile()
    assert plan.action is RecoveryAction.RESUME
    assert plan.task_id == "t1"


def test_more_than_one_active_is_manual(store, make_git_config, git_repo) -> None:
    store.insert_task(TaskRow(task_id="a", title="a", status=Status.IMPLEMENTING))
    store.insert_task(TaskRow(task_id="b", title="b", status=Status.REVIEWING))
    plan = _reconciler(store, make_git_config, git_repo).reconcile()
    assert plan.action is RecoveryAction.MANUAL
    assert set(plan.manual_task_ids) == {"a", "b"}


def test_interrupted_cleanup_is_cleanup(store, make_git_config, git_repo) -> None:
    store.insert_task(TaskRow(task_id="t1", title="t", status=Status.DONE, branch="agent/t1-x"))
    plan = _reconciler(store, make_git_config, git_repo).reconcile()
    assert plan.action is RecoveryAction.CLEANUP
    assert plan.task_id == "t1"


def test_terminal_without_branch_is_not_cleanup(store, make_git_config, git_repo) -> None:
    # A gate-rejected task (terminal failed, no branch) needs no cleanup.
    store.insert_task(TaskRow(task_id="t1", title="t", status=Status.FAILED))
    plan = _reconciler(store, make_git_config, git_repo).reconcile()
    assert plan.action is RecoveryAction.NONE


def _decomposed(store: StateStore, *, completed: int, shas: dict[int, str]) -> None:
    store.insert_task(
        TaskRow(
            task_id="d",
            title="d",
            status=Status.IMPLEMENTING,
            branch="agent/d-x",
            decomposition_accepted=True,
            subtask_count=2,
            active_subtask=completed + 1,
            subtasks_completed=completed,
        )
    )
    rows = []
    for order in (1, 2):
        rows.append(
            SubtaskRow(
                task_id="d",
                order=order,
                slug=f"s{order}",
                title=f"S{order}",
                status="committed" if order in shas else "pending",
                depends_on=(),
                commit_sha=shas.get(order),
            )
        )
    store.insert_subtasks(rows)


def test_decomposed_resumes_at_next_subtask(store, make_git_config, git_repo) -> None:
    _decomposed(store, completed=1, shas={1: "sha1"})
    plan = _reconciler(store, make_git_config, git_repo, on_branch=True).reconcile()
    assert plan.action is RecoveryAction.RESUME
    assert plan.resume_subtask == 2


def test_decomposed_recorded_sha_absent_is_manual(store, make_git_config, git_repo) -> None:
    _decomposed(store, completed=1, shas={1: "sha1"})
    # The fake git reports the recorded commit is NOT on the branch → inconsistent.
    plan = _reconciler(store, make_git_config, git_repo, on_branch=False).reconcile()
    assert plan.action is RecoveryAction.MANUAL
    assert "absent" in (plan.manual_reason or "")


def test_decomposed_more_committed_than_recorded_is_manual(
    store, make_git_config, git_repo
) -> None:
    # Both subtasks have a commit on the branch, but only 1 is recorded as completed.
    _decomposed(store, completed=1, shas={1: "sha1", 2: "sha2"})
    plan = _reconciler(store, make_git_config, git_repo, on_branch=True).reconcile()
    assert plan.action is RecoveryAction.MANUAL
    assert "committed" in (plan.manual_reason or "")


# --- resume() integration ----------------------------------------------------------------


class _FakeProvider:
    """Minimal provider: writes a file on implementation so each commit is non-empty."""

    def __init__(self, provider_id: str, clone: Path) -> None:
        self.id = provider_id
        self._clone = clone
        self._n = 0
        self.requests: list[AgentRunRequest] = []

    def preflight(self):
        from wastech_orchestrator.providers.base import ProviderHealth

        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version="1",
            authenticated=True,
            supports_required_features=True,
            message="ok",
        )

    def run(self, request):
        from wastech_orchestrator.providers.base import AgentRunResult, RunStatus, Stage

        self.requests.append(request)
        if request.stage is Stage.IMPLEMENTATION:
            (self._clone / f"impl-{self._n}.py").write_text("x\n", encoding="utf-8")
            self._n += 1
        structured = None
        if request.stage is Stage.REFINEMENT:
            structured = {"content": "done", "human_input": None}
        elif request.stage is Stage.PLANNING:
            structured = {
                "content": "done",
                "human_input": None,
                "decompose": False,
                "subtasks": [],
                "skills": [],
            }
        return AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider=self.id,
            stage=request.stage,
            attempt=request.attempt,
            exit_code=0,
            started_at="t",
            finished_at="t",
            final_message="done",
            structured_output=structured,
        )


def _make_providers(git_repo):
    from wastech_orchestrator.providers.base import ProviderId

    return {
        ProviderId.CLAUDE: _FakeProvider("claude", git_repo.clone),
        ProviderId.CODEX: _FakeProvider("codex", git_repo.clone),
    }


def _build_orchestrator(
    git_repo,
    make_git_config,
    tmp_path: Path,
    providers,
    verdicts,
    notifier: Notifier | None = None,
):
    from wastech_orchestrator.check_runner import CheckRunner
    from wastech_orchestrator.core.loop_control import LoopController
    from wastech_orchestrator.core.orchestrator import Orchestrator
    from wastech_orchestrator.git_manager import GitManager
    from wastech_orchestrator.ledger import Ledger
    from wastech_orchestrator.providers.process import ProcessResult
    from wastech_orchestrator.routing.router import AgentRouter
    from wastech_orchestrator.task.validation_gate import ValidationGate

    art = tmp_path / "art"
    config = make_git_config(git_repo.clone, checks=["pytest"], decomposition=True)
    store = StateStore.open(art / "state.db")
    ledger = Ledger(art / "logs")

    def fake_proc(argv, *, cwd, env, timeout_seconds, stdout_path, stdin_text=None):
        Path(stdout_path).write_text("ok\n", encoding="utf-8")
        return ProcessResult(
            exit_code=verdicts[0],
            timed_out=False,
            launch_error=None,
            duration_seconds=0.0,
            stdout_path=str(stdout_path),
            stderr_text="",
        )

    def fake_gh(argv: Sequence[str]):
        from wastech_orchestrator.git_manager import GitResult

        return GitResult(
            exit_code=0,
            stdout="https://example/pr/9\n",
            stderr="",
            timed_out=False,
            launch_error=None,
        )

    git = GitManager(config, store=store, artifacts_root=str(art), gh_runner=fake_gh)
    orch = Orchestrator(
        config,
        router=AgentRouter(config, providers),
        git=git,
        checks=CheckRunner(config, run_process=fake_proc),
        store=store,
        ledger=ledger,
        loops=LoopController(config.agents),
        gate=ValidationGate(
            config, store_has_task_id=store.task_id_exists, ledger_has_task_id=ledger.has_task_id
        ),
        artifacts_root=str(art),
        notifier=notifier,
    )
    return orch, store, ledger, art, git


def test_resume_no_active_returns_none(git_repo, make_git_config, tmp_path: Path) -> None:
    orch, *_ = _build_orchestrator(
        git_repo, make_git_config, tmp_path, _make_providers(git_repo), [0]
    )
    assert orch.resume() is None  # nothing in flight → slot free


def test_resume_more_than_one_active_marks_manual(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    notifier = RecordingNotifier()
    orch, store, ledger, *_ = _build_orchestrator(
        git_repo,
        make_git_config,
        tmp_path,
        _make_providers(git_repo),
        [0],
        notifier=notifier,
    )
    store.insert_task(TaskRow(task_id="a", title="a", status=Status.IMPLEMENTING))
    store.insert_task(TaskRow(task_id="b", title="b", status=Status.REVIEWING))
    result = orch.resume()
    assert result is not None and result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert store.get_task("a").status is Status.MANUAL_ACTION_REQUIRED
    assert store.get_task("b").status is Status.MANUAL_ACTION_REQUIRED
    assert {r["id"] for r in ledger.records()} == {"a", "b"}
    assert set(notifier.calls) == {
        ("a", "manual_action_required"),
        ("b", "manual_action_required"),
    }


def test_resume_interrupted_cleanup_notifies_after_ledger(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    notifier = RecordingNotifier()
    orch, store, ledger, *_ = _build_orchestrator(
        git_repo,
        make_git_config,
        tmp_path,
        _make_providers(git_repo),
        [0],
        notifier=notifier,
    )
    branch = "agent/task-cleanup-x"
    git_run(["checkout", "-b", branch], git_repo.clone)
    store.insert_task(
        TaskRow(task_id="task-cleanup", title="cleanup", status=Status.DONE, branch=branch)
    )

    result = orch.resume()

    assert result is not None and result.final_status is Status.DONE
    assert ledger.records()[0]["id"] == "task-cleanup"
    assert notifier.calls == [("task-cleanup", "done")]


@pytest.mark.parametrize(
    ("status", "expected_provider", "expected_first_stage"),
    [
        (Status.VALIDATED, ProviderId.CLAUDE, Stage.REFINEMENT),
        (Status.PREPARING, ProviderId.CLAUDE, Stage.REFINEMENT),
        (Status.REFINING, ProviderId.CLAUDE, Stage.REFINEMENT),
        (Status.PLANNING, ProviderId.CLAUDE, Stage.PLANNING),
        (Status.IMPLEMENTING, ProviderId.CLAUDE, Stage.IMPLEMENTATION),
        (Status.TESTING, ProviderId.CODEX, Stage.REVIEW),
        (Status.REVIEWING, ProviderId.CODEX, Stage.REVIEW),
        (Status.FIXING, ProviderId.CLAUDE, Stage.FIXING),
    ],
)
def test_resume_continues_persisted_checkpoint(
    status: Status,
    expected_provider: ProviderId,
    expected_first_stage: Stage,
    git_repo,
    make_git_config,
    git_run,
    tmp_path: Path,
) -> None:
    from wastech_orchestrator.task.model import NormalizedTask
    from wastech_orchestrator.task.parser import slugify, write_normalized

    providers = _make_providers(git_repo)
    orch, store, _, art, _ = _build_orchestrator(
        git_repo, make_git_config, tmp_path, providers, [0]
    )
    task_id = f"resume-{status.value}"
    title = "Resume checkpoint"
    slug = slugify(title)
    branch = f"agent/{task_id}-{slug}"
    write_normalized(
        NormalizedTask(id=task_id, title=title, description="Implement the requested change."),
        str(art),
    )

    branch_prepared = status not in {Status.VALIDATED, Status.PREPARING}
    if branch_prepared:
        git_run(["checkout", "-b", branch], git_repo.clone)
    if status in {Status.TESTING, Status.REVIEWING, Status.FIXING}:
        (git_repo.clone / "feature.py").write_text("implemented = True\n", encoding="utf-8")

    store.insert_task(
        TaskRow(
            task_id=task_id,
            title=title,
            status=status,
            branch=branch if branch_prepared else None,
            slug=slug if branch_prepared else None,
            decomposition_accepted=False,
            test_fix_cycles=1 if status is Status.FIXING else 0,
            fix_iterations=1 if status is Status.FIXING else 0,
        )
    )
    failed_check = art / "logs" / task_id / "checks" / "001.log"
    if status is Status.FIXING:
        failed_check.parent.mkdir(parents=True, exist_ok=True)
        failed_check.write_text("failed assertion\n", encoding="utf-8")
        store.record_check_run(
            CheckRunRow(
                task_id=task_id,
                command="pytest",
                passed=False,
                exit_code=1,
                log_path=str(failed_check),
            )
        )

    result = orch.resume()

    assert result is not None and result.final_status is Status.DONE
    expected = providers[expected_provider]
    assert expected.requests[0].stage is expected_first_stage
    all_requests = [request for provider in providers.values() for request in provider.requests]
    if status in {Status.TESTING, Status.REVIEWING, Status.FIXING}:
        assert all(request.stage is not Stage.IMPLEMENTATION for request in all_requests)
    if status is Status.FIXING:
        assert expected.requests[0].check_artifacts_path == str(failed_check)
        row = store.get_task(task_id)
        assert row is not None and row.fix_iterations == 1


def test_resume_waits_on_persisted_planning_prompt_without_resending(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    from wastech_orchestrator.core.hitl import (
        HumanInputSignal,
        interaction_path,
        load_interaction,
        write_waiting_interaction,
    )
    from wastech_orchestrator.task.model import NormalizedTask
    from wastech_orchestrator.task.parser import slugify, write_normalized

    notifier = RecordingNotifier(AskResult(answered=True, text="Use PostgreSQL."))
    providers = _make_providers(git_repo)
    orch, store, _, art, _ = _build_orchestrator(
        git_repo,
        make_git_config,
        tmp_path,
        providers,
        [0],
        notifier=notifier,
    )
    task_id = "resume-planning-hitl"
    title = "Resume planning HITL"
    slug = slugify(title)
    branch = f"agent/{task_id}-{slug}"
    git_run(["checkout", "-b", branch], git_repo.clone)
    write_normalized(
        NormalizedTask(id=task_id, title=title, description="Implement the requested change."),
        str(art),
    )
    store.insert_task(
        TaskRow(
            task_id=task_id,
            title=title,
            status=Status.PLANNING,
            branch=branch,
            slug=slug,
        )
    )

    path = interaction_path(art, task_id, Stage.PLANNING)
    handle = AskHandle(
        interaction_id="h-persisted",
        kind="question",
        expires_at=time.time() + 60,
        message_id=123,
        update_offset=456,
    )
    write_waiting_interaction(
        path,
        task_id=task_id,
        stage=Stage.PLANNING,
        subtask=None,
        signal=HumanInputSignal(
            kind="question",
            question="Which database should be used?",
            context="The task does not select one.",
            risk="clarification",
            paths=(),
        ),
        handle=handle,
    )

    result = orch.resume()

    assert result is not None and result.final_status is Status.DONE
    assert notifier.started == []
    assert notifier.waited == [handle]
    planning_requests = providers[ProviderId.CLAUDE].requests
    assert planning_requests[0].stage is Stage.PLANNING
    assert planning_requests[0].human_input_path == str(path)
    persisted = load_interaction(path)
    assert persisted is not None
    assert persisted["status"] == "consumed"
    assert persisted["answer"] == "Use PostgreSQL."


def test_resume_decomposed_at_subtask_without_duplicate_commit(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    from wastech_orchestrator.core.decomposition import (
        DecompositionDecision,
        SubtaskSpec,
        write_subtask_artifacts,
    )
    from wastech_orchestrator.git_manager import KIND_SUBTASK_COMMIT
    from wastech_orchestrator.state_store import PublishOpRow
    from wastech_orchestrator.task.model import NormalizedTask
    from wastech_orchestrator.task.parser import write_normalized

    providers = _make_providers(git_repo)
    orch, store, ledger, art, git = _build_orchestrator(
        git_repo, make_git_config, tmp_path, providers, [0]
    )
    task_id = "task-d"
    slug = "add-a-thing"
    branch = f"agent/{task_id}-{slug}"

    # Simulate an interrupted run: subtask 1 was committed on the branch, subtask 2 is pending.
    git_run(["checkout", "-b", branch], git_repo.clone)
    (git_repo.clone / "sub1.py").write_text("a = 1\n", encoding="utf-8")
    git_run(["add", "sub1.py"], git_repo.clone)
    git_run(["commit", "-m", "subtask 1"], git_repo.clone)
    sha1 = git_run(["rev-parse", "HEAD"], git_repo.clone)
    git_run(["checkout", "main"], git_repo.clone)

    task = NormalizedTask(id=task_id, title="Add a thing", description="do it")
    write_normalized(task, str(art))
    decision = DecompositionDecision(
        accepted=True,
        reason="accepted",
        n=2,
        subtasks=(
            SubtaskSpec(1, "First", "s1", ("a",), ()),
            SubtaskSpec(2, "Second", "s2", ("b",), (1,)),
        ),
    )
    write_subtask_artifacts(decision, str(art), task_id)

    store.insert_task(
        TaskRow(
            task_id=task_id,
            title="Add a thing",
            status=Status.IMPLEMENTING,
            branch=branch,
            slug=slug,
            decomposition_accepted=True,
            subtask_count=2,
            active_subtask=2,
            subtasks_completed=1,
            refinement_ran=False,
        )
    )
    store.insert_subtasks(
        [
            SubtaskRow(task_id, 1, "s1", "First", "committed", (), commit_sha=sha1),
            SubtaskRow(task_id, 2, "s2", "Second", "pending", (1,)),
        ]
    )
    store.record_publish_op(
        PublishOpRow(
            task_id=task_id,
            kind=KIND_SUBTASK_COMMIT,
            subtask_order=1,
            fingerprint="fp1",
            status="completed",
            result_ref=sha1,
        )
    )

    result = orch.resume()
    assert result is not None and result.final_status is Status.DONE
    # Subtask 1's recorded commit is unchanged (never re-committed, §13).
    subs = {s.order: s for s in store.get_subtasks(task_id)}
    assert subs[1].commit_sha == sha1
    assert subs[2].commit_sha and subs[2].commit_sha != sha1
    # Exactly one commit for subtask 1 on the branch.
    log = git_run(["log", "--format=%s", f"main..{branch}"], git_repo.clone)
    assert log.count("subtask 1") == 1
