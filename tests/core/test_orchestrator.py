"""Integration tests for the Orchestrator Core pipeline.

These drive the real Router + Git Manager (temp repo) + Check Runner, with fake in-memory providers
and a fake check process, exercising the full state machine, loops, decomposition, summary fallback,
publishing, terminal cleanup, and the ledger.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.core.orchestrator import Eligibility, Orchestrator, SlotBusyError
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    KIND_PR,
    KIND_PR_MERGE,
    GitCommandError,
    GitManager,
    GitResult,
)
from wastech_orchestrator.ledger import Ledger
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult, Notifier
from wastech_orchestrator.providers.artifacts import create_attempt_dir, task_artifact_dir
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    ProviderError,
    ProviderHealth,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow
from wastech_orchestrator.task.validation_gate import ValidationGate


class FakeProvider:
    """An in-memory AgentProvider returning scripted results, used to drive the Core."""

    def __init__(
        self,
        provider_id: str,
        *,
        outputs: dict[str, tuple[str, dict | None]] | None = None,
        infra_fail: set[str] | None = None,
    ) -> None:
        self.id = provider_id
        self._outputs = outputs or {}
        self._infra_fail = infra_fail or set()
        self.requests: list[AgentRunRequest] = []

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
        self.requests.append(request)
        if request.node_id in self._infra_fail:
            raise ProviderError(error_class=ErrorClass.TIMEOUT, message="infra fail")
        message, structured = self._outputs.get(request.node_id, ("done", None))
        if request.node_id == "refinement":
            structured = (
                structured
                if isinstance(structured, dict) and "content" in structured
                else {"content": message, "human_input": None}
            )
        elif request.node_id == "planning" and (
            not isinstance(structured, dict) or "content" not in structured
        ):
            planning = structured or {}
            structured = {
                "content": message,
                "human_input": None,
                "decompose": planning.get("decompose") is True,
                "subtasks": planning.get("subtasks") or [],
                "skills": planning.get("skills") or [],
            }
        return AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider=self.id,
            node_id=request.node_id,
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
            request.node_id,
            request.attempt,
            self.id,
            node_run_id=request.node_run_id,
        )
        result = super().run(request)
        Path(paths.stdout_path).write_text("provider output\n", encoding="utf-8")
        return replace(result, stdout_path=paths.stdout_path)


class RecordingNotifier:
    """Core-facing notifier fake that records terminal messages without network access."""

    def __init__(
        self,
        *,
        raise_on_send: bool = False,
        ask_results: list[AskResult] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.ask_calls: list[dict[str, object]] = []
        self._raise_on_send = raise_on_send
        self._ask_results = list(ask_results or [])

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
        contacts: tuple[str, ...] = (),
    ) -> None:
        self.calls.append(
            {
                "task_id": task_id,
                "final_status": final_status,
                "pr_url": pr_url,
                "reason": reason,
                "contacts": contacts,
            }
        )
        if self._raise_on_send:
            raise RuntimeError("notification failed")

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
        self.ask_calls.append(
            {
                "question": question,
                "context": context,
                "task_id": task_id,
                "kind": kind,
                "timeout_s": timeout_s,
                "interaction_id": interaction_id,
                "contacts": contacts,
            }
        )
        return AskHandle(
            interaction_id=interaction_id,
            kind=kind,
            expires_at=9999999999.0,
            message_id=len(self.ask_calls),
            update_offset=1,
        )

    def wait_for_answer(self, handle: AskHandle) -> AskResult:
        if self._ask_results:
            return replace(
                self._ask_results.pop(0),
                interaction_id=handle.interaction_id,
                message_id=handle.message_id,
            )
        return AskResult(
            answered=False,
            timed_out=True,
            failure="timeout",
            interaction_id=handle.interaction_id,
            message_id=handle.message_id,
        )

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
    gh: Callable[[Sequence[str]], GitResult] | None = None,
) -> tuple[Orchestrator, StateStore, Ledger, Path]:
    from wastech_orchestrator.checks.resolver import CheckResolver
    from wastech_orchestrator.routing.router import AgentRouter

    art = tmp_path / "art"
    config = make_git_config(git_repo.clone, checks=["pytest"], **(config_kwargs or {}))
    store = StateStore.open(art / "state.db")
    ledger = Ledger(art / "logs")
    router = AgentRouter(config, providers)  # type: ignore[arg-type]
    git = GitManager(config, store=store, artifacts_root=str(art), gh_runner=gh or _fake_gh())
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
        gate=gate,
        artifacts_root=str(art),
        notifier=notifier,
        resolver=CheckResolver(config),  # normalize checks.command_sets (production wires this)
    )
    return orch, store, ledger, art


def _complete_task(tmp_path: Path, task_id: str = "task-001") -> str:
    path = tmp_path / f"{task_id}.md"
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\n---\n\n'
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
    return FakeProvider(provider_id, outputs={"implementation": ("implemented", None)})


def test_happy_path_complete_task(git_repo, make_git_config, git_run, tmp_path: Path) -> None:
    providers = _both()
    notifier = RecordingNotifier()
    orch, store, ledger, _ = _build(
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
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE
    assert result.pr_url == "https://example/pr/1"

    row = store.get_task("task-001")
    assert row is not None and row.status is Status.DONE
    assert "refinement" in _skipped_nodes(store)  # complete task → refinement node skipped
    # The task file was moved into its lifecycle folder (tasks/done).
    assert (tmp_path / "done" / "task-001.md").exists()
    assert not (tmp_path / "task-001.md").exists()
    # A done task has no resume position: the flow checkpoint is cleared (no stale node= in status),
    # while node_runs remain for the audit trail (Secondary obs 1).
    assert store.get_flow_checkpoint("task-001") == (None, None, None)
    assert store.get_node_runs("task-001")  # the per-node audit trail is retained
    # Exactly one ledger record; back on the base branch.
    records = ledger.records()
    assert len(records) == 1 and records[0]["final_status"] == "done"
    assert notifier.calls == [
        {
            "task_id": "task-001",
            "final_status": "done",
            "pr_url": "https://example/pr/1",
            "reason": None,
            "contacts": (),
        }
    ]
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # The commit landed on the task branch (epoch-prefixed; read the actual name from the store).
    assert row.branch is not None
    branches = git_run(["branch", "--list", row.branch], git_repo.clone)
    assert row.branch in branches


def test_task_branch_name_override_controls_published_head(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    providers = _both()
    gh_calls: list[list[str]] = []

    def gh(argv: Sequence[str]) -> GitResult:
        gh_calls.append(list(argv))
        return GitResult(
            exit_code=0,
            stdout="https://example/pr/1\n",
            stderr="",
            timed_out=False,
            launch_error=None,
        )

    orch, store, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        gh=gh,
    )
    task = tmp_path / "task-custom-branch.md"
    task.write_text(
        '---\nid: task-custom-branch\ntitle: "Add a thing"\n'
        'branch_name: "feature/ABC-123-customer-branch"\n---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(str(task))
    assert result.final_status is Status.DONE
    row = store.get_task("task-custom-branch")
    assert row is not None and row.branch == "feature/ABC-123-customer-branch"
    assert ledger.records()[0]["branch"] == "feature/ABC-123-customer-branch"
    assert "feature/ABC-123-customer-branch" in git_run(
        ["branch", "--list", "feature/ABC-123-customer-branch"], git_repo.clone
    )
    assert gh_calls and "--head" in gh_calls[0]
    assert gh_calls[0][gh_calls[0].index("--head") + 1] == "feature/ABC-123-customer-branch"


def test_documentation_node_edit_is_committed(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # docs/backlog/documentation-node.md: the packaged implementation flow runs the documentation
    # node after review accepts; its workspace-write edit (here a docs file) joins the same diff the
    # orchestrator commits/publishes, using the same prompt/lineage/commit machinery as the others.
    providers = _both()
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        if request.node_id == "documentation":
            (git_repo.clone / "README.md").write_text("# project\n\nNow does a thing.\n", "utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-doc"))
    assert result.final_status is Status.DONE
    # The documentation node executed (after review, before publish), not skipped.
    ran = _ran_nodes(store, "task-doc")
    assert "documentation" in ran
    assert ran.index("review") < ran.index("documentation") < ran.index("publish")
    # Its docs edit is part of the committed change on the task branch (one shared diff).
    row = store.get_task("task-doc")
    assert row is not None and row.branch is not None
    committed = git_run(["show", "--name-only", "--format=", row.branch], git_repo.clone)
    assert "README.md" in committed
    assert "feature.py" in committed


def _evaluations(store: StateStore, task_id: str) -> list:
    return store._conn.execute(  # noqa: SLF001
        "SELECT node_id, kind, verdict FROM evaluations WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()


def test_supervisor_layer_observes_each_step_and_writes_one_summary(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # P2.1: the constant supervisor layer runs above any flow — it observes every executed
    # (non-publish) node read-only (advisory), and synthesizes the summary once at whole-task close.
    providers = _both()
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-sup"))
    assert result.final_status is Status.DONE

    rows = _evaluations(store, "task-sup")
    supervisor_rows = [r for r in rows if r["kind"].startswith("supervisor_")]
    # Every supervisor record is advisory and carries no node_id (it is a layer, not a node).
    assert {r["verdict"] for r in supervisor_rows} == {"advisory"}
    assert all(r["node_id"] is None for r in supervisor_rows)
    steps = [r for r in rows if r["kind"] == "supervisor_step"]
    finals = [r for r in rows if r["kind"] == "supervisor_final"]
    # One observation per executed non-publish node (planning, implementation, testing, review,
    # documentation).
    assert len(steps) >= 4
    assert len(finals) == 1  # the summary synthesis is once per whole task
    # The in-flow review evaluator also recorded an immutable verdict (a separate kind).
    assert any(r["kind"] == "in_flow_verdict" and r["node_id"] == "review" for r in rows)
    # The summary is always written (no config.summary_enabled gate) and committed as the PR body.
    assert (task_artifact_dir(art, "task-sup") / "summary.md").exists()
    # There is no summary graph node anymore — the layer owns it.
    assert "summary" not in _ran_nodes(store, "task-sup")


def test_finalize_tail_is_logged(git_repo, make_git_config, tmp_path: Path) -> None:
    # P2.1: the end-of-run tail (whole-task summary → publish prep) emits transition log lines so a
    # long silent window (context assembly + the summary LLM call) is observable, not a hang.
    # Capture on the ``wastech_orchestrator`` logger directly — once any run configures runtime
    # logging it sets ``propagate = False``, so a root-attached caplog would miss these records.
    providers = _both()
    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect(level=logging.INFO)
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        result = orch.run_task(_complete_task(tmp_path, "task-tail"))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
    assert result.final_status is Status.DONE
    assert any("task finalize: starting" in m for m in messages)
    assert any("task finalize: supervisor summary written" in m for m in messages)
    assert any("task finalize: publish prep" in m for m in messages)


def test_supervisor_summary_once_per_whole_task_not_subtask(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # P2.1: with decomposition the summary synthesis is once at whole-task close — not per subtask.
    subtasks = {
        "decompose": True,
        "skills": [],
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
    state = {"n": 0}

    class DecompProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.node_id == "planning":
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    node_id=request.node_id,
                    attempt=request.attempt,
                    exit_code=0,
                    started_at="t",
                    finished_at="t",
                    final_message="plan",
                    structured_output={"content": "plan", "human_input": None, **subtasks},
                )
            if request.node_id == "implementation":
                (git_repo.clone / f"impl-{state['n']}.py").write_text("x\n", encoding="utf-8")
                state["n"] += 1
            return super().run(request)

    providers = {
        ProviderId.CLAUDE: DecompProvider("claude"),
        ProviderId.CODEX: DecompProvider("codex"),
    }
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"decomposition": True},
    )
    result = orch.run_task(_complete_task(tmp_path, "task-sup-dec"))
    assert result.final_status is Status.DONE

    rows = _evaluations(store, "task-sup-dec")
    finals = [r for r in rows if r["kind"] == "supervisor_final"]
    assert len(finals) == 1  # exactly one, despite two subtasks


def test_live_route_defaults_to_global_primary(git_repo, make_git_config, tmp_path: Path) -> None:
    # Routing is node-based now (PRE.1): the packaged implementation flow declares no per-node
    # `provider`, so every node resolves to the config's global primary (claude) on the live engine
    # path, tagged RouteSource.CONFIG. A task can no longer repoint a stage's provider.
    providers = _both()
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-001"))
    assert result.final_status is Status.DONE

    runs = {r.node_id: r for r in store.get_node_runs("task-001")}
    impl = runs["implementation"]
    assert impl.route_primary == "claude"  # the global primary
    assert impl.route_source == "config"


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
        if request.node_id == "implementation":
            (git_repo.clone / "f.py").write_text("y = 2\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(str(path))
    assert result.final_status is Status.DONE
    # No acceptance criteria → needs_enrichment → refinement node ran (deterministic, PRE.3).
    assert "refinement" in _ran_nodes(store, "task-002")
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
        if request.node_id in ("implementation", "fixing"):
            (git_repo.clone / "f.py").write_text("z = 3\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE
    # The test-fix loop ran exactly one fixing node (checks failed once, then passed on retry).
    assert _ran_nodes(store, "task-003").count("fixing") == 1


def test_fix_iterations_synced_to_operator_surfaces(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # F6: the engine owns loop counting in FlowRunState; the operator-facing tasks.fix_iterations
    # (CLI status / get_counters) and the ledger must reflect it, not stay at the stale 0 they read
    # before the cutover synced them. One test-fix cycle => fix_iterations == 1 on both surfaces.
    providers = _both()
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[1, 0]
    )
    task_file = _complete_task(tmp_path, "task-009")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id in ("implementation", "fixing"):
            (git_repo.clone / "f.py").write_text("z = 3\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE
    assert _ran_nodes(store, "task-009").count("fixing") == 1
    row = store.get_task("task-009")
    assert row is not None and row.fix_iterations == 1  # synced from the engine, not stale 0
    assert ledger.records()[0]["fix_iterations"] == 1  # ledger reflects the engine's count


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
        if request.node_id in ("implementation", "fixing"):
            (git_repo.clone / "f.py").write_text(
                f"node_run_id = {request.node_run_id}\n", encoding="utf-8"
            )
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)

    assert result.final_status is Status.DONE
    rows = store._conn.execute(  # noqa: SLF001 - cross-checking SQLite against artifact paths
        "SELECT id FROM node_runs WHERE task_id = ? AND node_id = ? AND skipped = 0 ORDER BY id",
        ("task-two-fixes", "fixing"),
    ).fetchall()
    assert len(rows) == 2
    expected = [
        art / "logs" / "task-two-fixes" / "stages" / "fixing" / f"run-{row['id']:06d}" / "1-claude"
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
        if request.node_id in ("implementation", "fixing"):
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
            if request.node_id == "implementation":
                (git_repo.clone / "r.py").write_text("a = 1\n", encoding="utf-8")
            if request.node_id == "review":
                msg, structured = review_outputs[min(state["i"], 1)]
                state["i"] += 1
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    node_id=request.node_id,
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
    # Review blocked once → one review-driven fixing node ran, then review passed.
    assert _ran_nodes(store, "task-005").count("fixing") == 1


def test_review_infra_failure_degrades_to_manual_not_failed(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # An evaluator (review) that cannot RUN (both providers infra-fail) must not discard the green
    # diff: the task degrades to manual_action_required (branch preserved for the operator) and a
    # failure_report.json / stuck.md is written — never a silent terminal `failed`.
    providers = _both(infra_fail={"review"})
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-rev-infra"))
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert (art / "logs" / "task-rev-infra" / "failure_report.json").exists()
    assert (art / "logs" / "task-rev-infra" / "stuck.md").exists()
    assert ledger.records()[0]["final_status"] == "manual_action_required"
    # Review was reached; publish never ran (the diff is preserved on the branch, not published).
    ran = _ran_nodes(store, "task-rev-infra")
    assert "review" in ran and "publish" not in ran


_MINIMAL_FLOW = """
flow:
  name: implementation
  task_type: implementation
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: implementation
      kind: agent
      role_file: roles/implementation.md
      session_scope: editing_lineage
      permission_profile: workspace-write
    - id: publish
      kind: publish
      policy: pull_request
  edges:
    - { from: implementation, to: publish }
"""


def test_minimal_flow_implement_only(git_repo, make_git_config, tmp_path: Path) -> None:
    # P2.5: a degenerate flow (implementation → publish, no checks / review / fixing) is a valid
    # graph shape and executes; the constant supervisor layer still writes the summary. A flow
    # without a checks node simply has no mutation guard (optional via graph shape).
    from wastech_orchestrator.core.flow.registry import FlowRegistry

    flows = tmp_path / "flows"
    (flows / "roles").mkdir(parents=True)
    (flows / "roles" / "implementation.md").write_text("Implement {task_path}.", "utf-8")
    (flows / "implementation.yaml").write_text(_MINIMAL_FLOW, "utf-8")
    providers = _both()
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)  # noqa: SLF001
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-min"))
    assert result.final_status is Status.DONE
    ran = _ran_nodes(store, "task-min")
    assert set(ran) == {"implementation", "publish"}  # only the two declared nodes ran
    assert "testing" not in ran and "review" not in ran  # degenerate: no checks / review nodes
    assert (task_artifact_dir(art, "task-min") / "summary.md").exists()  # supervisor still wrote it


def test_summary_fallback_when_provider_fails(git_repo, make_git_config, tmp_path: Path) -> None:
    # Both providers fail the supervisor's summary synthesis with an infra error → minimal summary,
    # still DONE. The supervisor layer runs under its own "supervisor" node id (not a stage).
    providers = _both(infra_fail={"supervisor"})
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    task_file = _complete_task(tmp_path, "task-006")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "s.py").write_text("b = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE  # summary failure never blocks
    summary = (art / "logs" / "task-006" / "summary.md").read_text(encoding="utf-8")
    assert "## What" in summary


def test_decomposed_task_commits_each_subtask(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    subtasks = {
        "decompose": True,
        "skills": [],
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
            if request.node_id == "planning":
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    node_id=request.node_id,
                    attempt=request.attempt,
                    exit_code=0,
                    started_at="t",
                    finished_at="t",
                    final_message="plan",
                    structured_output={
                        "content": "plan",
                        "human_input": None,
                        **subtasks,
                    },
                )
            if request.node_id == "implementation":
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
    assert row.branch is not None
    count = git_run(["rev-list", "--count", f"main..{row.branch}"], git_repo.clone)
    assert int(count) >= 2
    subs = store.get_subtasks("task-007")
    assert all(s.commit_sha for s in subs)


def test_decomposed_subtask_spec_path_reaches_implementation_prompt(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # F5 / MC3: in decomposition mode each subtask's edit nodes must be scoped to that subtask — the
    # active immutable spec path (and "subtask N of M") is rendered into the implementation prompt,
    # so subtask 1 sees 01-first.md and subtask 2 sees 02-second.md.
    subtasks = {
        "decompose": True,
        "skills": [],
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
            if request.node_id == "planning":
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    node_id=request.node_id,
                    attempt=request.attempt,
                    exit_code=0,
                    started_at="t",
                    finished_at="t",
                    final_message="plan",
                    structured_output={"content": "plan", "human_input": None, **subtasks},
                )
            if request.node_id == "implementation":
                (git_repo.clone / f"impl-{state['n']}.py").write_text("x\n", encoding="utf-8")
                state["n"] += 1
            return super().run(request)

    state = {"n": 0}
    providers = {
        ProviderId.CLAUDE: DecompProvider("claude"),
        ProviderId.CODEX: DecompProvider("codex"),
    }
    orch, _store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"decomposition": True},
    )
    result = orch.run_task(_complete_task(tmp_path, "task-007"))
    assert result.final_status is Status.DONE

    impl_prompts = [
        r.prompt for r in providers[ProviderId.CLAUDE].requests if r.node_id == "implementation"
    ]
    assert len(impl_prompts) == 2  # one implementation run per subtask, each subtask-scoped
    assert "01-first.md" in impl_prompts[0] and "subtask 1 of 2" in impl_prompts[0].lower()
    assert "02-second.md" in impl_prompts[1] and "subtask 2 of 2" in impl_prompts[1].lower()


# --- operator-authored decomposition (``subtasks:`` manifest) ------------------------------


def _operator_root(tmp_path: Path, root_id: str, refs: list[str], *, front_extra: str = "") -> str:
    refs_yaml = "[" + ", ".join(f'"{r}"' for r in refs) + "]"
    path = tmp_path / f"{root_id}.md"
    path.write_text(
        f'---\nid: {root_id}\ntitle: "Epic"\nsubtasks: {refs_yaml}\n{front_extra}---\n\n'
        "## Description\n\nShared context for the whole change.\n",
        encoding="utf-8",
    )
    return str(path)


def _write_subtask(
    tmp_path: Path,
    rel: str,
    *,
    title: str,
    depends_on: tuple[str, ...] = (),
    body: str | None = None,
) -> None:
    dep_yaml = "[" + ", ".join(f'"{d}"' for d in depends_on) + "]"
    spec = tmp_path / rel
    spec.parent.mkdir(parents=True, exist_ok=True)
    text = body if body is not None else "## Acceptance criteria\n\n- it works\n"
    spec.write_text(
        f'---\ntitle: "{title}"\ndepends_on: {dep_yaml}\n---\n\n{text}', encoding="utf-8"
    )


class _OpImplProvider(FakeProvider):
    """Writes a distinct file per ``implementation`` run so each subtask commit is non-empty."""

    def __init__(self, provider_id: str, *, clone: Path, state: dict[str, int]) -> None:
        super().__init__(provider_id)
        self._clone = clone
        self._state = state

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (self._clone / f"impl-{self._state['n']}.py").write_text("x\n", encoding="utf-8")
            self._state["n"] += 1
        return super().run(request)


def test_operator_decomposition_runs_each_subtask_one_pr(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # Independent of ``agents.decomposition.enabled`` (the agent-proposal gate): the operator
    # manifest is authoritative. Three subtasks → one branch, one commit each, in order.
    _write_subtask(tmp_path, "subtasks/01-first.md", title="First")
    _write_subtask(tmp_path, "subtasks/02-second.md", title="Second", depends_on=("first",))
    _write_subtask(tmp_path, "subtasks/03-third.md", title="Third", depends_on=("second",))
    root = _operator_root(
        tmp_path,
        "epic-001",
        ["subtasks/01-first.md", "subtasks/02-second.md", "subtasks/03-third.md"],
    )
    state = {"n": 0}
    providers = {
        ProviderId.CLAUDE: _OpImplProvider("claude", clone=git_repo.clone, state=state),
        ProviderId.CODEX: _OpImplProvider("codex", clone=git_repo.clone, state=state),
    }
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    result = orch.run_task(root)
    assert result.final_status is Status.DONE
    row = store.get_task("epic-001")
    assert row is not None
    assert row.decomposition_accepted is True
    assert row.decomposition_reason == "operator_authored"
    assert row.subtask_count == 3
    subs = store.get_subtasks("epic-001")
    assert [s.slug for s in subs] == ["first", "second", "third"]
    assert [s.depends_on for s in subs] == [(), (1,), (2,)]
    assert all(s.commit_sha for s in subs)
    # The immutable spec carries the operator's verbatim body.
    spec = art / "logs" / "epic-001" / "subtasks" / "01-first.md"
    assert "## Acceptance criteria" in spec.read_text(encoding="utf-8")
    assert row.branch is not None
    count = git_run(["rev-list", "--count", f"main..{row.branch}"], git_repo.clone)
    assert int(count) >= 3


def test_operator_decomposition_when_planning_disabled(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # Disabling planning skips the proposed_by node (its post-hook never fires); the operator
    # decision is materialized at preflight regardless, so the split still runs.
    _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
    _write_subtask(tmp_path, "subtasks/02-b.md", title="B", depends_on=("a",))
    root = _operator_root(
        tmp_path,
        "epic-002",
        ["subtasks/01-a.md", "subtasks/02-b.md"],
        front_extra="nodes:\n  planning:\n    enabled: false\n",
    )
    state = {"n": 0}
    providers = {
        ProviderId.CLAUDE: _OpImplProvider("claude", clone=git_repo.clone, state=state),
        ProviderId.CODEX: _OpImplProvider("codex", clone=git_repo.clone, state=state),
    }
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    result = orch.run_task(root)
    assert result.final_status is Status.DONE
    row = store.get_task("epic-002")
    assert row is not None and row.decomposition_accepted is True and row.subtask_count == 2


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("count", "subtask_count_out_of_range"),
        ("forward", "subtask_depends_forward"),
        ("unknown_dep", "subtask_depends_forward"),
        ("traversal", "invalid_subtask_path"),
        ("beside_root", "invalid_subtask_path"),
        ("missing", "subtask_file_missing"),
        ("malformed", "subtask_malformed"),
    ],
)
def test_operator_decomposition_bad_manifest_rejected_before_branch(
    git_repo, make_git_config, git_run, tmp_path: Path, setup: str, reason: str
) -> None:
    if setup == "count":
        _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
        refs = ["subtasks/01-a.md"]  # < 2 units
    elif setup == "forward":
        _write_subtask(tmp_path, "subtasks/01-a.md", title="A", depends_on=("b",))
        _write_subtask(tmp_path, "subtasks/02-b.md", title="B")
        refs = ["subtasks/01-a.md", "subtasks/02-b.md"]
    elif setup == "unknown_dep":
        _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
        _write_subtask(tmp_path, "subtasks/02-b.md", title="B", depends_on=("ghost",))
        refs = ["subtasks/01-a.md", "subtasks/02-b.md"]
    elif setup == "traversal":
        _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
        _write_subtask(tmp_path, "subtasks/02-b.md", title="B")
        refs = ["subtasks/01-a.md", "../02-b.md"]
    elif setup == "beside_root":
        _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
        _write_subtask(tmp_path, "beside.md", title="B")
        refs = ["subtasks/01-a.md", "beside.md"]
    elif setup == "missing":
        _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
        refs = ["subtasks/01-a.md", "subtasks/02-nope.md"]
    else:  # malformed
        _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
        (tmp_path / "subtasks" / "02-bad.md").write_text("no front matter\n", encoding="utf-8")
        refs = ["subtasks/01-a.md", "subtasks/02-bad.md"]

    root = _operator_root(tmp_path, "epic-bad", refs)
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    result = orch.run_task(root)
    assert result.final_status is Status.FAILED
    assert result.validation_reason == reason
    # Quarantined with a report, and no branch was created.
    report = art / "logs" / "epic-bad" / "validation_report.json"
    assert json.loads(report.read_text(encoding="utf-8"))["reason"] == reason
    branches = git_run(["branch", "--list", "worc/*"], git_repo.clone)
    assert branches.strip() == ""


def test_operator_decomposition_flow_without_block_rejected(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A flow that declares no ``decomposition:`` block cannot host an operator split.
    _write_subtask(tmp_path, "subtasks/01-a.md", title="A")
    _write_subtask(tmp_path, "subtasks/02-b.md", title="B")
    root = _operator_root(
        tmp_path,
        "epic-flow",
        ["subtasks/01-a.md", "subtasks/02-b.md"],
        front_extra="task_type: deep_research\n",
    )
    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    result = orch.run_task(root)
    assert result.final_status is Status.FAILED
    assert result.validation_reason == "flow_cannot_decompose"


def test_single_active_slot_blocks(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    # Pre-seed another active task occupying the slot.
    store.insert_task(TaskRow(task_id="other", title="o", status=Status.RUNNING))
    with pytest.raises(SlotBusyError):
        orch.run_task(_complete_task(tmp_path, "task-008"))


def test_unknown_task_type_fails_before_branch(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A structurally-valid task whose ``task_type`` maps to no flow fails before any side effect:
    # the flow is resolved before branch prep, so an unknown type → terminal failed, no branch.
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    task = tmp_path / "task-ut.md"
    task.write_text(
        '---\nid: task-ut\ntitle: "X"\ntask_type: no_such_flow\n---\n\n## Description\n\nDo it.\n',
        encoding="utf-8",
    )
    result = orch.run_task(str(task))
    assert result.final_status is Status.FAILED
    branches = git_run(["branch", "--list", "worc/*"], git_repo.clone)
    assert branches == ""


def test_disable_unknown_node_fails_before_branch(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A task disabling a node id absent from its resolved flow fails before any side effect: the
    # disabled-node set is validated at flow resolution (before branch prep), so an unknown id →
    # terminal failed with no branch. (Shape passes the gate; existence is the resolution tier.)
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    block = "nodes:\n  no_such_node:\n    enabled: false\n"
    result = orch.run_task(_task_with_nodes(tmp_path, block))
    assert result.final_status is Status.FAILED
    branches = git_run(["branch", "--list", "worc/*"], git_repo.clone)
    assert branches == ""


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
    branches = git_run(["branch", "--list", "worc/*"], git_repo.clone)
    assert branches == ""
    assert ledger.records()[0]["validation_reason"] == "frontmatter_missing"
    assert notifier.calls == [
        {
            "task_id": "task-009",
            "final_status": "failed",
            "pr_url": None,
            "reason": "frontmatter_missing",
            "contacts": (),
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


# --- Phase 6: security & observability -------------------------------------------


def test_check_launch_failure_is_manual_not_a_fix_cycle(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A required check whose executable cannot be launched is an incomplete gate → manual hand-off
    # (the agent cannot install host toolchains) — never entering fixing or spending budget.
    from wastech_orchestrator.providers.process import ProcessResult

    providers = _both()
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )

    def launch_fail(argv, *, cwd, env, timeout_seconds, stdout_path, stdin_text=None):
        Path(stdout_path).write_text("", encoding="utf-8")
        return ProcessResult(
            exit_code=None,
            timed_out=False,
            launch_error="could not launch 'pytest': No such file or directory",
            duration_seconds=0.0,
            stdout_path=str(stdout_path),
            stderr_text="",
        )

    orch._checks = CheckRunner(orch._config, run_process=launch_fail)  # type: ignore[attr-defined]
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "f.py").write_text("a = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-launch"))
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    row = store.get_task("task-launch")
    assert row is not None and row.status is Status.MANUAL_ACTION_REQUIRED
    assert row.fix_iterations == 0  # a launch failure never consumed a fix iteration


def test_strict_isolation_preflight_fails_without_branch(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # When strict_isolation cannot be guaranteed, the task fails BEFORE a branch is created.
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
    assert git_run(["branch", "--list", "worc/*"], git_repo.clone) == ""  # no branch created
    assert ledger.records()[0]["final_status"] == "failed"


def test_failed_with_branch_commits_and_pushes_task_and_summary(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # Both providers fail (infra) at implementation, AFTER the branch exists → FAILED. The failed
    # attempt is finalized like a success: the task moves to tasks/failed/, its summary.md is
    # committed, and the branch is pushed — but no PR is opened for a failure.
    providers = _both(infra_fail={"implementation"})
    orch, store, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
    )
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True)
    task_file = pending / "task-fail.md"
    task_body = (
        '---\nid: task-fail\ntitle: "Add a thing"\n---\n\n'
        "## Description\n\nDo it.\n\n## Acceptance criteria\n\n- works\n"
    )
    task_file.write_text(task_body, encoding="utf-8")

    result = orch.run_task(str(task_file))

    assert result.final_status is Status.FAILED
    assert result.pr_url is None  # no PR for a failed attempt
    row = store.get_task("task-fail")
    assert row is not None and row.branch is not None
    branch = row.branch
    tracked = git_run(["ls-tree", "-r", "--name-only", branch], git_repo.clone)
    assert "tasks/failed/task-fail.md" in tracked  # task moved to failed/ and committed
    assert "tasks/failed/task-fail.summary.md" in tracked  # summary committed beside it
    assert ".worc/" not in tracked  # working artifacts never enter git
    # The failed branch was pushed for inspection; the working copy is back on base.
    assert git_run(["ls-remote", "--heads", "origin", branch], git_repo.clone) != ""
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    assert ledger.records()[0]["final_status"] == "failed"


def test_publish_failure_after_finalize_is_manual_not_stranded_done(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A git failure during publishing AFTER finalize has moved the task file to tasks/done/ and
    # committed the audit trail must not mislabel the work: the deliverable is committed, only the
    # push/PR did not finish → resumable MANUAL_ACTION_REQUIRED, with the task file consistently in
    # done/ (never stranded as a failed/ artifact, never marked FAILED). (F1 / MC2.)
    providers = _both()
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    task_file = _complete_task(tmp_path)
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    def boom(*args: object, **kwargs: object) -> str:
        raise GitCommandError("simulated PR creation failure")

    orch._git.create_pr = boom  # type: ignore[method-assign]

    result = orch.run_task(task_file)

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED  # not FAILED, not DONE
    assert result.pr_url is None
    row = store.get_task("task-001")
    assert row is not None and row.status is Status.MANUAL_ACTION_REQUIRED
    # The task file stays in its done/ lifecycle folder — never stranded as failed/, never left in
    # the queue root.
    assert (tmp_path / "done" / "task-001.md").exists()
    assert not (tmp_path / "failed" / "task-001.md").exists()
    assert not (tmp_path / "task-001.md").exists()
    assert ledger.records()[0]["final_status"] == "manual_action_required"


def test_artifacts_registered_with_checksums(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
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
    # A decomposed task that gets stuck in subtask 1 records its decomposition context.
    subtasks = {
        "decompose": True,
        "skills": [],
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
            if request.node_id == "planning":
                return AgentRunResult(
                    status=RunStatus.SUCCEEDED,
                    provider=self.id,
                    node_id=request.node_id,
                    attempt=request.attempt,
                    exit_code=0,
                    started_at="t",
                    finished_at="t",
                    final_message="plan",
                    structured_output={
                        "content": "plan",
                        "human_input": None,
                        **subtasks,
                    },
                )
            if request.node_id in ("implementation", "fixing"):
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


def _incomplete_task(tmp_path: Path, task_id: str) -> str:
    path = tmp_path / f"{task_id}.md"
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Choose behavior"\n---\n\n'
        "## Description\n\nThe behavior is intentionally ambiguous.\n",
        encoding="utf-8",
    )
    return str(path)


def _stage_result(
    provider: FakeProvider,
    request: AgentRunRequest,
    structured: dict[str, object],
) -> AgentRunResult:
    # Planning output requires the `skills` key; default it so test fixtures stay terse.
    if "decompose" in structured and "skills" not in structured:
        structured = {**structured, "skills": []}
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider=provider.id,
        node_id=request.node_id,
        attempt=request.attempt,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message=str(structured.get("content", "")),
        structured_output=structured,
    )


def test_refinement_question_is_answered_and_reinjected(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    class HitlProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.node_id == "refinement":
                self.requests.append(request)
                if request.human_input_path is None:
                    return _stage_result(
                        self,
                        request,
                        {
                            "content": "",
                            "human_input": {
                                "kind": "question",
                                "question": "Which behavior?",
                                "context": "The task permits A or B.",
                                "risk": "clarification",
                                "paths": [],
                            },
                        },
                    )
                return _stage_result(
                    self,
                    request,
                    {"content": "Use behavior B.", "human_input": None},
                )
            if request.node_id == "implementation":
                (git_repo.clone / "feature.py").write_text("behavior = 'B'\n", encoding="utf-8")
            return super().run(request)

    claude = HitlProvider("claude")
    providers = {ProviderId.CLAUDE: claude, ProviderId.CODEX: FakeProvider("codex")}
    notifier = RecordingNotifier(
        ask_results=[AskResult(answered=True, text="Use B", approved=None)]
    )
    orch, _, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_incomplete_task(tmp_path, "task-hitl-question"))

    assert result.final_status is Status.DONE
    refinement_requests = [r for r in claude.requests if r.node_id == "refinement"]
    assert len(refinement_requests) == 2
    assert refinement_requests[1].human_input_path is not None
    interaction = json.loads(
        (art / "logs" / "task-hitl-question" / "hitl" / "refinement.json").read_text()
    )
    assert interaction["status"] == "consumed"
    assert interaction["answer"] == "Use B"


def test_refinement_timeout_is_manual_action_required(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    signal = {
        "content": "",
        "human_input": {
            "kind": "question",
            "question": "Which behavior?",
            "context": "",
            "risk": "clarification",
            "paths": [],
        },
    }
    providers = _both(outputs={"refinement": ("", signal)})
    notifier = RecordingNotifier(
        ask_results=[AskResult(answered=False, timed_out=True, failure="timeout")]
    )
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_incomplete_task(tmp_path, "task-hitl-timeout"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED


def test_ambiguous_stage_approval_is_manual_action_required(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    signal = {
        "content": "",
        "human_input": {
            "kind": "approval",
            "question": "Proceed?",
            "context": "",
            "risk": "other",
            "paths": [],
        },
    }
    providers = _both(outputs={"refinement": ("", signal)})
    notifier = RecordingNotifier(
        ask_results=[AskResult(answered=True, text="maybe", approved=None)]
    )
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_incomplete_task(tmp_path, "task-hitl-ambiguous"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED


def test_repeated_stage_question_is_manual_action_required(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    signal = {
        "content": "",
        "human_input": {
            "kind": "question",
            "question": "Still unclear?",
            "context": "",
            "risk": "clarification",
            "paths": [],
        },
    }
    providers = _both(outputs={"refinement": ("", signal)})
    notifier = RecordingNotifier(ask_results=[AskResult(answered=True, text="Use B")])
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_incomplete_task(tmp_path, "task-hitl-repeat"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED


@pytest.mark.parametrize("danger", ["dependency", "deletion"])
def test_dangerous_diff_requires_approval(
    danger: str, git_repo, make_git_config, tmp_path: Path
) -> None:
    class DangerousProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.node_id == "implementation":
                if danger == "dependency":
                    (git_repo.clone / "pyproject.toml").write_text(
                        "[project]\nname='x'\n", encoding="utf-8"
                    )
                else:
                    (git_repo.clone / "README.md").unlink()
            return super().run(request)

    providers = {
        ProviderId.CLAUDE: DangerousProvider("claude"),
        ProviderId.CODEX: DangerousProvider("codex"),
    }
    notifier = RecordingNotifier(
        ask_results=[AskResult(answered=True, text="approved", approved=True)]
    )
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_complete_task(tmp_path, f"task-{danger}-approval"))

    assert result.final_status is Status.DONE
    # Exactly one approval despite TWO workspace-write nodes (implementation + documentation) seeing
    # the same uncommitted dangerous diff: the guard honors the prior in-task approval and the
    # documentation node does not re-prompt for the already-cleared change.
    assert len(notifier.ask_calls) == 1
    assert notifier.ask_calls[0]["kind"] == "approval"
    assert "documentation" in _ran_nodes(store, f"task-{danger}-approval")


def test_denied_dependency_change_gets_one_safe_reconsideration(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    class ReconsideringProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.node_id == "implementation":
                dependency = git_repo.clone / "pyproject.toml"
                if request.human_input_path is None:
                    dependency.write_text("[project]\nname='x'\n", encoding="utf-8")
                elif dependency.exists():
                    dependency.unlink()
                (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
            return super().run(request)

    claude = ReconsideringProvider("claude")
    providers = {ProviderId.CLAUDE: claude, ProviderId.CODEX: FakeProvider("codex")}
    notifier = RecordingNotifier(
        ask_results=[AskResult(answered=True, text="denied", approved=False)]
    )
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_complete_task(tmp_path, "task-denied-dependency"))

    assert result.final_status is Status.DONE
    implementation = [r for r in claude.requests if r.node_id == "implementation"]
    assert len(implementation) == 2
    assert implementation[1].human_input_path is not None


def test_denied_dangerous_change_that_remains_requires_manual_action(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    class PersistentDangerProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.node_id == "implementation":
                (git_repo.clone / "pyproject.toml").write_text(
                    "[project]\nname='x'\n", encoding="utf-8"
                )
            return super().run(request)

    providers = {
        ProviderId.CLAUDE: PersistentDangerProvider("claude"),
        ProviderId.CODEX: FakeProvider("codex"),
    }
    notifier = RecordingNotifier(
        ask_results=[AskResult(answered=True, text="denied", approved=False)]
    )
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_complete_task(tmp_path, "task-denied-risk-remains"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert len(notifier.ask_calls) == 1


def test_planning_approval_is_reused_for_exact_dependency_diff(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    class PlanningApprovalProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.node_id == "planning":
                self.requests.append(request)
                if request.human_input_path is None:
                    return _stage_result(
                        self,
                        request,
                        {
                            "content": "",
                            "human_input": {
                                "kind": "approval",
                                "question": "Approve adding Python dependencies?",
                                "context": "The plan changes pyproject.toml.",
                                "risk": "dependency",
                                "paths": ["pyproject.toml"],
                            },
                            "decompose": False,
                            "subtasks": [],
                        },
                    )
                return _stage_result(
                    self,
                    request,
                    {
                        "content": "Update pyproject.toml.",
                        "human_input": None,
                        "decompose": False,
                        "subtasks": [],
                    },
                )
            if request.node_id == "implementation":
                (git_repo.clone / "pyproject.toml").write_text(
                    "[project]\nname='x'\n", encoding="utf-8"
                )
            return super().run(request)

    claude = PlanningApprovalProvider("claude")
    providers = {ProviderId.CLAUDE: claude, ProviderId.CODEX: FakeProvider("codex")}
    notifier = RecordingNotifier(
        ask_results=[AskResult(answered=True, text="approved", approved=True)]
    )
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_complete_task(tmp_path, "task-planning-approval"))

    assert result.final_status is Status.DONE
    assert len(notifier.ask_calls) == 1


def test_expanded_diff_requires_separate_approval_after_planning(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    class ExpandedDiffProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.node_id == "planning":
                self.requests.append(request)
                if request.human_input_path is None:
                    return _stage_result(
                        self,
                        request,
                        {
                            "content": "",
                            "human_input": {
                                "kind": "approval",
                                "question": "Approve changing pyproject.toml?",
                                "context": "",
                                "risk": "dependency",
                                "paths": ["pyproject.toml"],
                            },
                            "decompose": False,
                            "subtasks": [],
                        },
                    )
                return _stage_result(
                    self,
                    request,
                    {
                        "content": "Update the Python manifest.",
                        "human_input": None,
                        "decompose": False,
                        "subtasks": [],
                    },
                )
            if request.node_id == "implementation":
                (git_repo.clone / "pyproject.toml").write_text(
                    "[project]\nname='x'\n", encoding="utf-8"
                )
                (git_repo.clone / "package-lock.json").write_text("{}\n", encoding="utf-8")
            return super().run(request)

    claude = ExpandedDiffProvider("claude")
    providers = {ProviderId.CLAUDE: claude, ProviderId.CODEX: FakeProvider("codex")}
    notifier = RecordingNotifier(
        ask_results=[
            AskResult(answered=True, text="approved", approved=True),
            AskResult(answered=True, text="approved", approved=True),
        ]
    )
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_complete_task(tmp_path, "task-expanded-approval"))

    assert result.final_status is Status.DONE
    assert len(notifier.ask_calls) == 2


# --- auto-merge bypass (git.auto_merge*) ------------------------------------------------


def _merge_gh(
    calls: list[list[str]], *, merge_exit: int = 0, merge_stderr: str = ""
) -> Callable[[Sequence[str]], GitResult]:
    """Fake `gh` that records argv and handles pr create / merge / view for auto-merge tests."""

    def gh(argv: Sequence[str]) -> GitResult:
        calls.append(list(argv))
        head = list(argv[:2])
        if head == ["pr", "view"]:
            return GitResult(0, "deadbeef\n", "", False, None)
        if head == ["pr", "merge"]:
            return GitResult(merge_exit, "", merge_stderr, False, None)
        return GitResult(
            0, "https://example/pr/1\n", "", False, None
        )  # pr create (+ anything else)

    return gh


def _patch_impl_edit(providers: dict[ProviderId, FakeProvider], git_repo) -> None:
    """Make the implementation stage leave a change to commit (so publish has a real diff)."""
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]


def _task_with_auto_merge(tmp_path: Path, value: bool, task_id: str = "task-001") -> str:
    path = tmp_path / f"{task_id}.md"
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\n'
        f"auto_merge: {str(value).lower()}\n---\n\n"
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    return str(path)


def _merge_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["pr", "merge"]]


def test_auto_merge_resolution_matrix(git_repo, make_git_config, tmp_path: Path) -> None:
    from wastech_orchestrator.task.model import NormalizedTask

    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    base_git = orch._config.git

    def eff(task_am: bool | None, cfg_am: bool) -> bool:
        orch._config = replace(orch._config, git=replace(base_git, auto_merge=cfg_am))
        task = NormalizedTask(id="t", title="T", description="d", auto_merge=task_am)
        return orch._auto_merge_on(task)

    # The per-task value wins outright (PRE.2), in every config combination.
    for cfg_am in (True, False):
        assert eff(False, cfg_am) is False
        assert eff(True, cfg_am) is True
    # Absent (None) defers to the global flag.
    assert eff(None, True) is True
    assert eff(None, False) is False


def test_global_auto_merge_merges_pr(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    calls: list[list[str]] = []
    orch, store, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": True},
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_complete_task(tmp_path))
    assert result.final_status is Status.DONE
    assert _merge_calls(calls) == [["pr", "merge", "https://example/pr/1", "--squash"]]
    rec = ledger.records()[0]
    assert rec["auto_merged"] is True and rec["merge_outcome"] == "deadbeef"
    op = store.get_publish_op("task-001", "pr_merge")
    assert op is not None and op.status == "completed"


def test_no_auto_merge_leaves_pr_open(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    calls: list[list[str]] = []
    orch, _, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_complete_task(tmp_path))
    assert result.final_status is Status.DONE
    assert _merge_calls(calls) == []  # never merged
    assert ledger.records()[0]["auto_merged"] is False


def test_per_task_true_wins_over_global_false(git_repo, make_git_config, tmp_path: Path) -> None:
    # PRE.2: a per-task ``auto_merge: true`` wins outright over the instance default ``false``;
    # there is no operator gate. The task author owns the decision (see docs/operations.md).
    providers = _both()
    calls: list[list[str]] = []
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": False},
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_task_with_auto_merge(tmp_path, True))
    assert result.final_status is Status.DONE
    assert len(_merge_calls(calls)) == 1


def test_per_task_false_opts_out_under_global(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    calls: list[list[str]] = []
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": True},
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_task_with_auto_merge(tmp_path, False))
    assert result.final_status is Status.DONE
    assert _merge_calls(calls) == []  # explicit per-task opt-out wins over the global flag


def test_auto_merge_blocked_goes_manual(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    calls: list[list[str]] = []
    orch, store, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": True},
        gh=_merge_gh(calls, merge_exit=1, merge_stderr="required status checks are pending"),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_complete_task(tmp_path))
    # A blocked merge is non-fatal: not FAILED, not a silent DONE — the PR is left open for a human.
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert len(_merge_calls(calls)) == 1  # attempted exactly once, no retry storm
    op = store.get_publish_op("task-001", "pr_merge")
    assert op is not None and op.status != "completed"  # resume can retry
    assert ledger.records()[0]["final_status"] == "manual_action_required"


def test_auto_merge_wait_for_checks_arms_native_auto(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    providers = _both()
    calls: list[list[str]] = []
    orch, _, ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": True, "auto_merge_wait_for_checks": True},
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_complete_task(tmp_path))
    assert result.final_status is Status.DONE
    assert _merge_calls(calls) == [["pr", "merge", "https://example/pr/1", "--squash", "--auto"]]
    assert ledger.records()[0]["merge_outcome"] == "armed"


def test_auto_merge_does_not_fire_when_quality_gate_fails(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # auto_merge affects only the publish step: a task that never reaches publish is never merged.
    providers = _both()
    calls: list[list[str]] = []
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[1] * 20,  # checks always fail → fix loop exhausts, never publishes
        config_kwargs={"auto_merge": True},
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_complete_task(tmp_path))
    assert result.final_status is not Status.DONE
    assert _merge_calls(calls) == []


# --- node-disable control (per-task nodes.<node-id>.enabled: false) ---------------------


def _task_with_nodes(tmp_path: Path, nodes_block: str, task_id: str = "task-001") -> str:
    path = tmp_path / f"{task_id}.md"
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\n{nodes_block}---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    return str(path)


def _skipped_nodes(store: StateStore, task_id: str = "task-001") -> list[str]:
    # The flow records a skipped node (deterministic when=false skip) as a node_runs row; node ids
    # are stage-aligned in the packaged flow, so the skipped node id is the skipped stage name.
    return sorted(r.node_id for r in store.get_node_runs(task_id) if r.skipped)


def _ran_nodes(store: StateStore, task_id: str = "task-001") -> list[str]:
    """Node ids that actually executed (not skipped), in order — the node_runs audit trail."""
    return [r.node_id for r in store.get_node_runs(task_id) if not r.skipped]


def _summary_text(art: Path, task_id: str = "task-001") -> str:
    return (task_artifact_dir(art, task_id) / "summary.md").read_text(encoding="utf-8")


def test_skip_planning_writes_stub_and_runs(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)
    block = "nodes:\n  planning:\n    enabled: false\n"
    result = orch.run_task(_task_with_nodes(tmp_path, block))
    assert result.final_status is Status.DONE
    # The planning agent was never invoked; the planning node was deterministically skipped.
    assert all(r.node_id != "planning" for r in providers[ProviderId.CLAUDE].requests)
    # Flow semantics: a skipped node writes no artifact (no legacy stub plan); the task still runs.
    assert not (task_artifact_dir(art, "task-001") / "plan.md").exists()
    assert "planning" in _skipped_nodes(store)


def test_skip_testing_bypasses_checks(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    # check_verdicts would FAIL if the runner ran — proving testing is bypassed, not passed.
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[1] * 20,
    )
    _patch_impl_edit(providers, git_repo)
    block = "nodes:\n  testing:\n    enabled: false\n"
    result = orch.run_task(_task_with_nodes(tmp_path, block))
    assert result.final_status is Status.DONE
    n_checks = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM check_runs"
    ).fetchone()["n"]
    assert n_checks == 0  # the check runner never ran
    assert "testing" in _skipped_nodes(store)


def test_skip_review_commits_without_review(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
    )
    _patch_impl_edit(providers, git_repo)
    block = "nodes:\n  review:\n    enabled: false\n"
    result = orch.run_task(_task_with_nodes(tmp_path, block))
    assert result.final_status is Status.DONE
    # Review is routed to codex (primary); it must never be invoked for the review node.
    assert all(r.node_id != "review" for r in providers[ProviderId.CODEX].requests)
    assert "review" in _skipped_nodes(store)


def test_skip_fixing_routes_to_manual_on_failure(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[1] * 20,  # first check fails
    )
    _patch_impl_edit(providers, git_repo)
    block = "nodes:\n  fixing:\n    enabled: false\n"
    result = orch.run_task(_task_with_nodes(tmp_path, block))
    # Fixing disabled → the failure still ends at manual review (the preserved capability). The
    # engine is domain-agnostic, so it does NOT special-case "no fixing → straight to manual": it
    # runs the declared test-fix loop (the skipped fixing node is a no-op each cycle) until the cap,
    # then exhausts → manual. So fix_iterations honestly reflects the bounded loop, not 0 — F6 made
    # this visible (it was masked by the stale-0 counter before); see Verification Additional #7.
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert store.get_counters("task-001").fix_iterations == orch._config.agents.max_fix_cycles
    assert "fixing" in _skipped_nodes(store)


def test_skipped_nodes_listed_in_summary(git_repo, make_git_config, tmp_path: Path) -> None:
    providers = _both()
    orch, _, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
    )
    _patch_impl_edit(providers, git_repo)
    block = "nodes:\n  planning:\n    enabled: false\n  testing:\n    enabled: false\n"
    result = orch.run_task(_task_with_nodes(tmp_path, block))
    assert result.final_status is Status.DONE
    summary = _summary_text(art)
    assert "## Pipeline nodes skipped" in summary
    assert "`planning`" in summary and "`testing`" in summary


def test_review_disabled_with_auto_merge_still_merges(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Disabling ``review`` + ``auto_merge`` merges without any review gate. There is no longer a
    # ``review``-special-case warning (the operator owns which nodes are safe to disable); the
    # generic auto-merge behaviour is unchanged.
    providers = _both()
    calls: list[list[str]] = []
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": True},
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    block = "nodes:\n  review:\n    enabled: false\n"
    result = orch.run_task(_task_with_nodes(tmp_path, block))
    assert result.final_status is Status.DONE
    assert "review" in _skipped_nodes(store)
    assert len(_merge_calls(calls)) == 1  # it really did merge without a review gate


def test_planning_selected_skills_reach_downstream_stages(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # Seed a target-repo skill (committed, so it is a tracked repo file, not an agent change);
    # planning picks it (plus an unknown name that must be dropped).
    skill_dir = git_repo.clone / ".claude" / "skills" / "safe-change"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: safe-change\ndescription: review your change\n---\n\n# Body\nguidance\n",
        encoding="utf-8",
    )
    git_run(["add", ".claude/skills/safe-change/SKILL.md"], git_repo.clone)
    git_run(["commit", "-m", "add safe-change skill"], git_repo.clone)
    providers = {
        ProviderId.CLAUDE: FakeProvider(
            "claude", outputs={"planning": ("plan", {"skills": ["safe-change", "ghost"]})}
        ),
        ProviderId.CODEX: FakeProvider("codex"),
    }
    orch, _, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )

    result = orch.run_task(_complete_task(tmp_path, "task-skills"))
    assert result.final_status is Status.DONE

    # plan.md records the selection and the dropped unknown name (auditable).
    plan = (art / "logs" / "task-skills" / "plan.md").read_text(encoding="utf-8")
    assert "Skills (planning-selected" in plan
    assert "safe-change" in plan and "ghost" in plan

    # The chosen SKILL.md reaches a downstream stage as a read-only reference path, not its body.
    downstream = [
        r
        for p in providers.values()
        for r in p.requests
        if r.node_id in ("implementation", "fixing", "review")
    ]
    assert downstream, "a downstream stage ran"
    impl = next(r for r in downstream if r.node_id == "implementation")
    assert any(path.endswith("safe-change/SKILL.md") for path in impl.skill_reference_paths)
    assert "ghost" not in str(impl.skill_reference_paths)  # unknown name never surfaced
    assert "# Body" not in impl.prompt  # the skill body is never inlined into the prompt


# --- prompt audit (who+prompt per step) -----------------------------------------------------


def test_prompt_audit_resolution_matrix(git_repo, make_git_config, tmp_path: Path) -> None:
    """The per-task value always overrides the global; absent (None) defers to the global flag."""
    from wastech_orchestrator.task.model import NormalizedTask

    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )

    def eff(task_pa: bool | None, cfg_pa: bool) -> bool:
        orch._config = replace(orch._config, prompt_audit=cfg_pa)
        task = NormalizedTask(id="t", title="T", description="d", prompt_audit=task_pa)
        return orch._prompt_audit_on(task)

    # Task True/False win in every global combination (no operator gate).
    assert eff(True, False) is True
    assert eff(True, True) is True
    assert eff(False, True) is False
    assert eff(False, False) is False
    # Absent (None) defers to the global flag.
    assert eff(None, True) is True
    assert eff(None, False) is False


def test_decomposition_gate_resolution_matrix(git_repo, make_git_config, tmp_path: Path) -> None:
    """Per-task ``decomposition`` overrides the global gate; absent (None) defers to the global."""
    from wastech_orchestrator.task.model import NormalizedTask

    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )

    def eff(task_dc: bool | None, cfg_enabled: bool) -> bool:
        agents = orch._config.agents
        deco = replace(agents.decomposition, enabled=cfg_enabled)
        orch._config = replace(orch._config, agents=replace(agents, decomposition=deco))
        task = NormalizedTask(id="t", title="T", description="d", decomposition=task_dc)
        return orch._decomposition_gate_on(task)

    # Task True/False win in every global combination (no operator gate).
    assert eff(True, False) is True
    assert eff(True, True) is True
    assert eff(False, True) is False
    assert eff(False, False) is False
    # Absent (None) defers to the global flag.
    assert eff(None, True) is True
    assert eff(None, False) is False


def _audit_dir(art: Path, task_id: str) -> Path:
    return task_artifact_dir(art, task_id) / "prompt-audit"


def test_prompt_audit_records_steps_in_order(git_repo, make_git_config, tmp_path: Path) -> None:
    """With the global flag on, each stage run is recorded as a self-contained, chronological file
    plus a combined timeline; the records carry the who-metadata and the prompt."""
    providers = _both()
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"prompt_audit": True},
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_complete_task(tmp_path))
    assert result.final_status is Status.DONE

    audit_dir = _audit_dir(art, "task-001")
    step_files = sorted(audit_dir.glob("*.json"))
    assert step_files, "per-step audit files were written"
    # Filenames are zero-padded node_run_id → lexical sort is chronological.
    ids = [int(p.name.split("-")[0]) for p in step_files]
    assert ids == sorted(ids)
    # complete task → refinement skipped; planning/implementation/review/documentation run agents.
    # The summary is written by the supervisor layer now (not a graph node), so no summary step is
    # audited.
    stages = [json.loads(p.read_text())["node_id"] for p in step_files]
    assert stages == ["planning", "implementation", "review", "documentation"]

    # The combined timeline has one line per step, in the same chronological order.
    lines = (audit_dir / "timeline.jsonl").read_text().splitlines()
    assert len(lines) == len(step_files)
    timeline = [json.loads(line) for line in lines]
    assert [r["node_run_id"] for r in timeline] == sorted(r["node_run_id"] for r in timeline)
    for rec in timeline:
        assert rec["prompt"]
        assert rec["agents"] and rec["agents"][0]["status"] == "succeeded"
        assert "route_primary" in rec and "provider_used" in rec

    # Who-metadata is correct: every node defaults to the global primary (claude) now (PRE.1).
    review = next(r for r in timeline if r["node_id"] == "review")
    assert review["provider_used"] == "claude"
    assert review["agents"][0]["provider"] == "claude"
    assert review["agents"][0]["is_fallback"] is False

    # Both artifact kinds are registered in SQLite.
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT kind FROM artifacts WHERE task_id = ?", ("task-001",)
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert {"prompt_audit", "prompt_audit_timeline"} <= kinds


# NOTE: the live-path "primary fails → fallback runs" prompt-audit scenario no longer exists for the
# packaged flow: every node defaults to the global primary, whose fallback target is itself (none).
# Fallback fires only for a node pinned to a non-primary provider. The fallback who-metadata
# (is_fallback across primary+fallback attempts) is unit-covered in test_flow_observability.py.


def test_prompt_audit_absent_when_disabled(git_repo, make_git_config, tmp_path: Path) -> None:
    """Global off and no per-task flag → no audit directory, no audit artifacts."""
    providers = _both()
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_complete_task(tmp_path))
    assert result.final_status is Status.DONE

    assert not _audit_dir(art, "task-001").exists()
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT kind FROM artifacts WHERE task_id = ?", ("task-001",)
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert "prompt_audit" not in kinds and "prompt_audit_timeline" not in kinds


def test_prompt_audit_task_overrides_global_off(git_repo, make_git_config, tmp_path: Path) -> None:
    """Global off but a per-task ``prompt_audit: true`` → only that task is audited."""
    providers = _both()
    orch, _, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)
    path = tmp_path / "task-001.md"
    path.write_text(
        '---\nid: task-001\ntitle: "Add a thing"\nprompt_audit: true\n---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    result = orch.run_task(str(path))
    assert result.final_status is Status.DONE

    audit_dir = _audit_dir(art, "task-001")
    assert audit_dir.exists()
    assert (audit_dir / "timeline.jsonl").exists()
    assert sorted(audit_dir.glob("*.json"))


# --- task dependencies (``depends_on`` merge-gated scheduling) -----------------------------

_DEP_PR = "https://example/pr/dep"


def _merge_state_gh(state: str, sha: str | None = None) -> Callable[[Sequence[str]], GitResult]:
    """A gh fake that answers the readiness probe with a fixed PR state (and SHA when merged)."""

    def gh(argv: Sequence[str]) -> GitResult:
        if list(argv[:2]) == ["pr", "view"]:
            payload = {"state": state, "mergeCommit": {"oid": sha} if sha else None}
            return GitResult(
                exit_code=0,
                stdout=json.dumps(payload),
                stderr="",
                timed_out=False,
                launch_error=None,
            )
        return GitResult(
            exit_code=0,
            stdout="https://example/pr/1\n",
            stderr="",
            timed_out=False,
            launch_error=None,
        )

    return gh


def _seed_task(store: StateStore, task_id: str, status: Status) -> None:
    store.insert_task(TaskRow(task_id=task_id, title=task_id, status=status))


def _seed_pr(
    store: StateStore, task_id: str, *, pr_url: str = _DEP_PR, merge: str | None = None
) -> None:
    store.record_publish_op(
        PublishOpRow(
            task_id=task_id, kind=KIND_PR, fingerprint=pr_url, status="completed", result_ref=pr_url
        )
    )
    if merge is not None:
        store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=KIND_PR_MERGE,
                fingerprint=pr_url,
                status="completed",
                result_ref=merge,
            )
        )


def test_dependency_eligibility_dep_merged_is_eligible_and_backfills_sha(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        gh=_merge_state_gh("MERGED", sha="realsha"),
    )
    _seed_task(store, "dep", Status.DONE)
    _seed_pr(store, "dep", merge="armed")  # auto-merge armed, real SHA not yet captured
    verdict = orch.dependency_eligibility("task-001", ("dep",), pending={})
    assert verdict.state is Eligibility.ELIGIBLE
    # The readiness probe backfilled the armed merge op with the real SHA (SQLite only).
    op = store.get_publish_op("dep", KIND_PR_MERGE)
    assert op is not None and op.result_ref == "realsha"


def test_dependency_eligibility_dep_open_pr_waits(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        gh=_merge_state_gh("OPEN"),
    )
    _seed_task(store, "dep", Status.DONE)
    _seed_pr(store, "dep", merge="armed")
    assert (
        orch.dependency_eligibility("task-001", ("dep",), pending={}).state is Eligibility.WAITING
    )


def test_dependency_eligibility_dep_failed_waits_forever(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    _seed_task(store, "dep", Status.FAILED)
    assert (
        orch.dependency_eligibility("task-001", ("dep",), pending={}).state is Eligibility.WAITING
    )


def test_dependency_eligibility_local_commit_done_is_eligible(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    _seed_task(store, "dep", Status.DONE)  # no PR recorded → local-commit mode
    assert (
        orch.dependency_eligibility("task-001", ("dep",), pending={}).state is Eligibility.ELIGIBLE
    )


def test_dependency_eligibility_all_deps_must_be_merged(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        gh=_merge_state_gh("MERGED", sha="s"),
    )
    _seed_task(store, "a", Status.DONE)
    _seed_pr(store, "a", pr_url="https://example/pr/a", merge="armed")
    _seed_task(store, "b", Status.RUNNING)  # still in flight
    assert (
        orch.dependency_eligibility("task-001", ("a", "b"), pending={}).state is Eligibility.WAITING
    )
    store.update_task("b", status=Status.DONE)  # b finishes, no PR → local-commit eligible
    assert (
        orch.dependency_eligibility("task-001", ("a", "b"), pending={}).state
        is Eligibility.ELIGIBLE
    )


def test_dependency_eligibility_pending_dep_waits(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    pending = {"task-001": ("dep",), "dep": ()}
    assert (
        orch.dependency_eligibility("task-001", ("dep",), pending=pending).state
        is Eligibility.WAITING
    )


def test_dependency_eligibility_unknown_ref_is_broken(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    verdict = orch.dependency_eligibility("task-001", ("ghost",), pending={"task-001": ("ghost",)})
    assert verdict.state is Eligibility.BROKEN
    assert "ghost" in verdict.detail


def test_dependency_eligibility_cycle_is_broken(git_repo, make_git_config, tmp_path: Path) -> None:
    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    pending = {"task-001": ("task-002",), "task-002": ("task-001",)}
    assert (
        orch.dependency_eligibility("task-001", ("task-002",), pending=pending).state
        is Eligibility.BROKEN
    )
    assert (
        orch.dependency_eligibility("task-002", ("task-001",), pending=pending).state
        is Eligibility.BROKEN
    )


def test_dependency_eligibility_empty_is_eligible(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, _, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    assert orch.dependency_eligibility("task-001", (), pending={}).state is Eligibility.ELIGIBLE


def test_reject_dependency_quarantines_and_records_failed(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    path = _complete_task(tmp_path, "task-001")
    result = orch.reject_dependency(path, "depends on unknown task 'ghost'")
    assert result.final_status is Status.FAILED
    assert not Path(path).exists()  # quarantined out of the source folder
    record = ledger.records()[-1]
    assert record["id"] == "task-001"
    assert record["validation_reason"] == "invalid_depends_on"
