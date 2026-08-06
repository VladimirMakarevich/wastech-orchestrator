"""Tests for restart recovery: reconciliation decisions and resume behavior."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.core.recovery import RecoveryAction, RecoveryReconciler
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult, Notifier
from wastech_orchestrator.providers.artifacts import exchange_task_dir
from wastech_orchestrator.providers.base import AgentRunRequest, ProviderId
from wastech_orchestrator.state_store import (
    CheckRunRow,
    NodeRunRow,
    StateStore,
    SubtaskRow,
    TaskRow,
)

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow


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
        governance_changed: tuple[str, ...] = (),
        details: object = None,
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
    store.insert_task(TaskRow(task_id="t1", title="t", status=Status.RUNNING))
    plan = _reconciler(store, make_git_config, git_repo).reconcile()
    assert plan.action is RecoveryAction.RESUME
    assert plan.task_id == "t1"


def test_more_than_one_active_is_manual(store, make_git_config, git_repo) -> None:
    store.insert_task(TaskRow(task_id="a", title="a", status=Status.RUNNING))
    store.insert_task(TaskRow(task_id="b", title="b", status=Status.RUNNING))
    plan = _reconciler(store, make_git_config, git_repo).reconcile()
    assert plan.action is RecoveryAction.MANUAL
    assert set(plan.manual_task_ids) == {"a", "b"}


def test_interrupted_cleanup_is_cleanup(store, make_git_config, git_repo) -> None:
    store.insert_task(TaskRow(task_id="t1", title="t", status=Status.DONE, branch="worc/t1-x"))
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
            status=Status.RUNNING,
            branch="worc/d-x",
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
            supports_required_features=True,
            message="ok",
        )

    def run(self, request):
        from wastech_orchestrator.providers.base import AgentRunResult, RunStatus

        self.requests.append(request)
        if request.node_id == "implementation":
            (self._clone / f"impl-{self._n}.py").write_text("x\n", encoding="utf-8")
            self._n += 1
        structured = None
        if request.node_id == "refinement":
            structured = {"content": "done", "human_input": None}
        elif request.node_id == "planning":
            structured = {
                "content": "done",
                "human_input": None,
                "decompose": False,
                "subtasks": [],
            }
        elif request.node_id == "review":
            # The review evaluator requires a well-formed findings array; a well-formed empty
            # one is a clean, accepting verdict.
            structured = {"findings": []}
        return AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider=self.id,
            node_id=request.node_id,
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
    from tests.conftest import seed_builtin_flows

    from wastech_orchestrator.check_runner import CheckRunner
    from wastech_orchestrator.core.orchestrator import Orchestrator
    from wastech_orchestrator.git_manager import GitManager
    from wastech_orchestrator.ledger import Ledger
    from wastech_orchestrator.providers.process import ProcessResult
    from wastech_orchestrator.routing.router import AgentRouter
    from wastech_orchestrator.runtime_layout import RuntimeLayout
    from wastech_orchestrator.task.validation_gate import ValidationGate

    art = tmp_path / "art"
    config = make_git_config(git_repo.clone, checks=["pytest"], decomposition=True)
    seed_builtin_flows(git_repo.clone)  # deliver the built-in flows as `worc install` would
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
        gate=ValidationGate(
            config, store_has_task_id=store.task_id_exists, ledger_has_task_id=ledger.has_task_id
        ),
        layout=RuntimeLayout(
            repo_root=Path(config.repo.local_path),
            control_home=Path(config.repo.local_path) / ".worc",
            private_home=art,
            exchange_root=Path(config.repo.local_path) / ".worc-io",
        ),
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
    store.insert_task(TaskRow(task_id="a", title="a", status=Status.RUNNING))
    store.insert_task(TaskRow(task_id="b", title="b", status=Status.RUNNING))
    result = orch.resume()
    assert result is not None and result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert store.get_task("a").status is Status.MANUAL_ACTION_REQUIRED
    assert store.get_task("b").status is Status.MANUAL_ACTION_REQUIRED
    assert {r["id"] for r in ledger.records()} == {"a", "b"}
    assert set(notifier.calls) == {
        ("a", "manual_action_required"),
        ("b", "manual_action_required"),
    }


def test_resume_corrupt_normalized_artifact_marks_manual(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A corrupt/truncated task.normalized.json must fail closed to manual on resume, not crash out
    # of resume() with an uncaught JSONDecodeError (fail-closed).
    from wastech_orchestrator.providers.artifacts import task_artifact_dir

    notifier = RecordingNotifier()
    orch, store, ledger, art, _ = _build_orchestrator(
        git_repo,
        make_git_config,
        tmp_path,
        _make_providers(git_repo),
        [0],
        notifier=notifier,
    )
    task_id = "corrupt"
    store.insert_task(TaskRow(task_id=task_id, title="Corrupt manifest", status=Status.RUNNING))
    task_dir = task_artifact_dir(str(art), task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.normalized.json").write_text('{"id": "corrupt", "ti', encoding="utf-8")

    result = orch.resume()

    assert result is not None and result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert store.get_task(task_id).status is Status.MANUAL_ACTION_REQUIRED
    assert ledger.records()[0]["id"] == task_id
    assert notifier.calls == [(task_id, "manual_action_required")]


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
    branch = "worc/task-cleanup-x"
    git_run(["checkout", "-b", branch], git_repo.clone)
    store.insert_task(
        TaskRow(task_id="task-cleanup", title="cleanup", status=Status.DONE, branch=branch)
    )

    result = orch.resume()

    assert result is not None and result.final_status is Status.DONE
    assert ledger.records()[0]["id"] == "task-cleanup"
    assert notifier.calls == [("task-cleanup", "done")]


def test_resume_blocked_cleanup_returns_none_and_keeps_queue_scanning(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A terminal manual_action_required task whose cleanup cannot complete (a dirty tree it
    # does not own) must NOT freeze the queue. resume() returns None — the task is terminal and owns
    # no slot — so watch_once falls through to scan pending/. The cleanup stays re-elected each tick
    # (self-heals once the operator clears the tree), so a second tick still returns None rather
    # than a blocking manual_action_required result. Without the fix the retry returns MAR forever
    # and watch_once returns before the pending scan — the permanent stall.
    notifier = RecordingNotifier()
    orch, store, ledger, *_ = _build_orchestrator(
        git_repo, make_git_config, tmp_path, _make_providers(git_repo), [0], notifier=notifier
    )
    branch = "worc/task-blocked-x"
    git_run(["checkout", "-b", branch], git_repo.clone)
    store.insert_task(
        TaskRow(
            task_id="task-blocked",
            title="blocked",
            status=Status.MANUAL_ACTION_REQUIRED,
            branch=branch,
        )
    )
    # A dirty, foreign working tree the task never produced (no evaluator/checks/publish node ran →
    # _worktree_is_task_output is False → preserve_own_wip is False), so new-mode terminal cleanup
    # fail-closes on "unaccounted changes".
    (git_repo.clone / "foreign.txt").write_text("dirty\n", encoding="utf-8")

    first = orch.resume()
    second = orch.resume()

    assert first is None and second is None  # a blocked cleanup never blocks the queue
    row = store.get_task("task-blocked")
    assert row is not None and not row.cleanup_completed  # still blocked, re-elected each tick
    assert row.cleanup_last_error and "unaccounted changes" in row.cleanup_last_error
    # Ledger + notify fire exactly once (the first tick), marked blocked rather than completed.
    assert notifier.calls == [("task-blocked", "manual_action_required")]
    assert ledger.records()[0]["terminal_cleanup"] == "blocked"


def test_resume_cleanup_preserves_own_wip_on_manual_park(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A resumable manual_action_required park carrying the task's OWN uncommitted work
    # (a publish node ran, so the dirty tree is the task's output — its --continue resume input) is
    # preserved on the resume path exactly as the primary terminal path does: cleanup leaves HEAD on
    # the branch and reports safe instead of fail-closing on "unaccounted changes". Without the fix
    # the retry drops preserve_own_wip and stalls on the very tree _go_terminal kept.
    notifier = RecordingNotifier()
    orch, store, ledger, *_ = _build_orchestrator(
        git_repo, make_git_config, tmp_path, _make_providers(git_repo), [0], notifier=notifier
    )
    branch = "worc/task-wip-x"
    git_run(["checkout", "-b", branch], git_repo.clone)
    store.insert_task(
        TaskRow(
            task_id="task-wip",
            title="wip",
            status=Status.MANUAL_ACTION_REQUIRED,
            branch=branch,
        )
    )
    store.record_node_run(NodeRunRow(task_id="task-wip", node_id="publish", node_kind="publish"))
    (git_repo.clone / "own-wip.txt").write_text("task work\n", encoding="utf-8")

    result = orch.resume()

    assert result is not None and result.final_status is Status.MANUAL_ACTION_REQUIRED
    row = store.get_task("task-wip")
    assert row is not None and row.cleanup_completed  # preserved WIP → cleanup reports safe
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone).strip() == branch
    assert (git_repo.clone / "own-wip.txt").exists()  # the task's WIP is untouched
    assert ledger.records()[0]["terminal_cleanup"] == "completed"


def _impl_fingerprint() -> str:
    from tests.conftest import BUILTIN_FLOWS_DIR

    from wastech_orchestrator.core.flow.registry import FlowRegistry

    registry = FlowRegistry(operator_flows_dir=BUILTIN_FLOWS_DIR)
    return registry.resolve("implementation").flow_fingerprint


def _seed_control_bundle(
    orch,
    store: StateStore,
    task_id: str,
    *,
    skill_packages: tuple[tuple[str, str, list[str]], ...] = (),
) -> None:
    """Seed the control + instruction bundles an interrupted task left on disk.

    A resume verifies both frozen bundles against their persisted digests before reusing them; these
    recovery tests seed the checkpoint directly (bypassing the fresh run that freezes), so they must
    also freeze the implementation-flow control bundle the checkpoint fingerprints AND a minimal
    instruction bundle (task packet + any tracked root repo instructions + any selected skill
    packages), recording both digests. ``skill_packages`` is ``(folder, skill_md_rel, files_rel)``
    per selected skill (matching what a fresh run would freeze for a resumed skill map).
    """
    from wastech_orchestrator.core.flow.control_bundle import freeze_control_bundle
    from wastech_orchestrator.core.flow.instruction_bundle import (
        REPO_INSTRUCTION_NAMES,
        discover_repository_instructions,
        freeze_repository_instructions,
        freeze_skill_package,
        freeze_task_packet,
        write_instruction_manifest,
    )

    snapshot = orch._flow_registry.resolve("implementation")
    assert snapshot.source_path is not None
    bundle_dir = orch._control_bundle_dir(task_id)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle = freeze_control_bundle(
        bundle_dir, snapshot, snapshot.source_path.parent, orch._tool_registry
    )
    store.update_task(task_id, control_bundle_digest=bundle.bundle_digest)

    ib_dir = orch._instruction_bundle_dir(task_id)
    ib_dir.mkdir(parents=True, exist_ok=True)
    src = ib_dir.parent / f"{task_id}.seed-task.md"
    src.write_text("# seeded task\n", encoding="utf-8")
    _, task_entry = freeze_task_packet(ib_dir, src)
    repo_root = Path(orch._config.repo.local_path)
    tracked = frozenset(orch._git.list_tracked_files(*REPO_INSTRUCTION_NAMES))
    repo_entries = freeze_repository_instructions(
        ib_dir, discover_repository_instructions(repo_root, tracked)
    )
    entries = [task_entry, *repo_entries]
    for folder, skill_md_rel, files_rel in skill_packages:
        package = freeze_skill_package(ib_dir, folder, skill_md_rel, files_rel, repo_root)
        entries.extend(package.entries)
    digest = write_instruction_manifest(
        ib_dir, entries=entries, control_digest=bundle.bundle_digest
    )
    store.update_task(task_id, instruction_manifest_digest=digest)


@pytest.mark.parametrize(
    ("current_node", "expected_provider", "expected_first_stage"),
    [
        # No checkpoint (interrupted before the engine) → restart from refinement.
        (None, ProviderId.CLAUDE, "refinement"),
        ("refinement", ProviderId.CLAUDE, "refinement"),
        ("planning", ProviderId.CLAUDE, "planning"),
        ("implementation", ProviderId.CLAUDE, "implementation"),
        # Every node defaults to the global primary (claude) now — routing is node-based (PRE.1).
        ("testing", ProviderId.CLAUDE, "review"),  # checks node → first agent node is review
        ("review", ProviderId.CLAUDE, "review"),
        ("fixing", ProviderId.CLAUDE, "fixing"),
    ],
)
def test_resume_continues_persisted_checkpoint(
    current_node: str | None,
    expected_provider: ProviderId,
    expected_first_stage: str,
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
    task_id = f"resume-{current_node or 'fresh'}"
    title = "Resume checkpoint"
    slug = slugify(title)
    branch = f"worc/{task_id}-{slug}"
    write_normalized(
        NormalizedTask(id=task_id, title=title, description="Implement the requested change."),
        str(art),
    )

    # An interrupted engine task: RUNNING + a flow checkpoint at current_node. No checkpoint
    # (current_node=None) models an interruption before the engine wrote one → a fresh restart.
    branch_prepared = current_node is not None
    if branch_prepared:
        git_run(["checkout", "-b", branch], git_repo.clone)
    if current_node in {"testing", "review", "fixing"}:
        (git_repo.clone / "feature.py").write_text("implemented = True\n", encoding="utf-8")

    store.insert_task(
        TaskRow(
            task_id=task_id,
            title=title,
            status=Status.RUNNING if current_node else Status.VALIDATED,
            branch=branch if branch_prepared else None,
            slug=slug if branch_prepared else None,
            decomposition_accepted=False,
        )
    )
    if current_node is not None:
        store.save_flow_checkpoint(
            task_id,
            current_node=current_node,
            counters_json="{}",
            flow_fingerprint=_impl_fingerprint(),
            fix_iterations=0,
        )
        _seed_control_bundle(orch, store, task_id)
    failed_check = art / "logs" / task_id / "checks" / "001.log"
    if current_node == "fixing":
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
    # Ignore the constant supervisor layer's per-step observations (read-only, its own "supervisor"
    # node id, on the global primary) — they interleave with node requests but are not graph nodes.
    node_requests = [r for r in expected.requests if r.node_id != "supervisor"]
    assert node_requests[0].node_id == expected_first_stage
    all_requests = [
        request
        for provider in providers.values()
        for request in provider.requests
        if request.node_id != "supervisor"
    ]
    if current_node in {"testing", "review", "fixing"}:
        assert all(request.node_id != "implementation" for request in all_requests)
    if current_node == "fixing":
        # Recovery re-publishes the failed check log into the exchange and points {checks_path}
        # there; the private log stays the audit record.
        expected_checks = (
            exchange_task_dir(orch._exchange_root, task_id) / "checks" / "001.log"
        ).as_posix()
        assert node_requests[0].check_artifacts_path == expected_checks
        assert failed_check.exists()


def test_resume_restores_skill_map_without_re_proposing(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # skills-selection-rework: the effective per-node skill map was resolved + persisted to
    # skill_map.json before the interruption. A resume restores it (and does NOT re-run the
    # supervisor proposal), so the resumed implementation node still receives the skill path.
    import json

    from wastech_orchestrator.task.model import NormalizedTask
    from wastech_orchestrator.task.parser import slugify, write_normalized

    providers = _make_providers(git_repo)
    orch, store, _, art, _ = _build_orchestrator(
        git_repo, make_git_config, tmp_path, providers, [0]
    )
    task_id = "resume-skills"
    title = "Resume checkpoint"
    slug = slugify(title)
    branch = f"worc/{task_id}-{slug}"
    write_normalized(
        NormalizedTask(id=task_id, title=title, description="Implement the requested change."),
        str(art),
    )
    git_run(["checkout", "-b", branch], git_repo.clone)
    store.insert_task(
        TaskRow(
            task_id=task_id,
            title=title,
            status=Status.RUNNING,
            branch=branch,
            slug=slug,
            decomposition_accepted=False,
        )
    )
    store.save_flow_checkpoint(
        task_id,
        current_node="implementation",
        counters_json="{}",
        flow_fingerprint=_impl_fingerprint(),
        fix_iterations=0,
    )
    # The skill exists in the clone; the per-node map was persisted before the interruption.
    # Identity is the repo-relative POSIX path (joined onto the clone when surfaced to a provider).
    skill_md = git_repo.clone / ".claude" / "skills" / "safe-change" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text("---\nname: safe-change\ndescription: d\n---\n# Body\n", "utf-8")
    # A fresh run would have frozen the selected skill's package into the instruction bundle; seed
    # it so resume reconstructs the same frozen exchange path (not a re-read of the live SKILL.md).
    _seed_control_bundle(
        orch,
        store,
        task_id,
        skill_packages=(
            (
                "safe-change",
                ".claude/skills/safe-change/SKILL.md",
                [".claude/skills/safe-change/SKILL.md"],
            ),
        ),
    )
    skill_map = art / "logs" / task_id / "skill_map.json"
    skill_map.parent.mkdir(parents=True, exist_ok=True)
    skill_map.write_text(
        json.dumps(
            {
                "implementation": [
                    {
                        "name": "safe-change",
                        "description": "d",
                        "path": ".claude/skills/safe-change/SKILL.md",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = orch.resume()
    assert result is not None and result.final_status is Status.DONE

    impl = next(r for p in providers.values() for r in p.requests if r.node_id == "implementation")
    assert any(path.endswith("safe-change/SKILL.md") for path in impl.skill_reference_paths)


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
    branch = f"worc/{task_id}-{slug}"
    git_run(["checkout", "-b", branch], git_repo.clone)
    write_normalized(
        NormalizedTask(id=task_id, title=title, description="Implement the requested change."),
        str(art),
    )
    store.insert_task(
        TaskRow(
            task_id=task_id,
            title=title,
            status=Status.RUNNING,
            branch=branch,
            slug=slug,
            decomposition_accepted=False,
        )
    )
    store.save_flow_checkpoint(
        task_id,
        current_node="planning",
        counters_json="{}",
        flow_fingerprint=_impl_fingerprint(),
        fix_iterations=0,
    )
    _seed_control_bundle(orch, store, task_id)

    path = interaction_path(art, task_id, "planning")
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
        node_id="planning",
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
    assert planning_requests[0].node_id == "planning"
    # The provider receives only the sanitized answer-only exchange packet, never the durable record
    # human_input_path points under the exchange, not at the durable interaction file.
    # (The active exchange is torn down at the terminal outcome; the packet's answer-only shape is
    # unit-tested in test_hitl.py::test_sanitized_answer_packet_is_answer_only.)
    exchange_hitl = (
        exchange_task_dir(orch._exchange_root, task_id) / "hitl" / "planning.answer.json"
    ).as_posix()
    assert planning_requests[0].human_input_path == exchange_hitl
    assert planning_requests[0].human_input_path != str(path)
    # The full durable interaction record stays private and unchanged.
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
    branch = f"worc/{task_id}-{slug}"

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
            status=Status.RUNNING,
            branch=branch,
            slug=slug,
            decomposition_accepted=True,
            subtask_count=2,
            active_subtask=2,
            subtasks_completed=1,
        )
    )
    store.save_flow_checkpoint(
        task_id,
        current_node="implementation",
        counters_json="{}",
        flow_fingerprint=_impl_fingerprint(),
        fix_iterations=0,
    )
    _seed_control_bundle(orch, store, task_id)
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
    # Subtask 1's recorded commit is unchanged (never re-committed).
    subs = {s.order: s for s in store.get_subtasks(task_id)}
    assert subs[1].commit_sha == sha1
    assert subs[2].commit_sha and subs[2].commit_sha != sha1
    # Exactly one commit for subtask 1 on the branch.
    log = git_run(["log", "--format=%s", f"main..{branch}"], git_repo.clone)
    assert log.count("subtask 1") == 1
