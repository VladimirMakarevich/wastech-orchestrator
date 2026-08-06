"""Integration tests for the Orchestrator Core pipeline.

These drive the real Router + Git Manager (temp repo) + Check Runner, with fake in-memory providers
and a fake check process, exercising the full state machine, loops, decomposition, summary fallback,
publishing, terminal cleanup, and the ledger.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.core.flow.exchange_seal import (
    exchange_quarantine_root,
    exchange_seal_root,
)
from wastech_orchestrator.core.flow.nodes.exchange_publish import ExchangeMutationManual
from wastech_orchestrator.core.orchestrator import Eligibility, Orchestrator, SlotBusyError
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    KIND_PR,
    KIND_PR_MERGE,
    GitCommandError,
    GitManager,
    GitResult,
)
from wastech_orchestrator.ledger import Ledger, LedgerRecord
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult, Notifier
from wastech_orchestrator.providers.artifacts import (
    create_attempt_dir,
    exchange_node_run_dir,
    exchange_task_dir,
    node_run_dir,
    task_artifact_dir,
)
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    ProviderError,
    ProviderHealth,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.providers.exchange import build_exchange_manifest
from wastech_orchestrator.runtime_layout import RUNS_DIRNAME, RuntimeLayout
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow
from wastech_orchestrator.task.validation_gate import ValidationGate

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow

# The shipped default evicts a successful task's own `runs/` subtree at its terminal transition. A
# test that inspects a finished task's frozen bundles or sealed exchange must therefore switch that
# off — the same switch an operator flips to analyze runs.
_KEEP_RUN_ARTIFACTS = {"clean_runs_on_success": False}


class FakeProvider:
    """An in-memory AgentProvider returning scripted results, used to drive the Core."""

    def __init__(
        self,
        provider_id: str,
        *,
        outputs: dict[str, tuple[str, dict | None]] | None = None,
        infra_fail: set[str] | None = None,
        infra_error_class: ErrorClass = ErrorClass.TIMEOUT,
    ) -> None:
        self.id = provider_id
        self._outputs = outputs or {}
        self._infra_fail = infra_fail or set()
        self._infra_error_class = infra_error_class
        self._healed = False
        self.requests: list[AgentRunRequest] = []

    def heal(self) -> None:
        """Stop infra-failing — simulate the provider's outage clearing (B-lite resume tests)."""
        self._healed = True

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
        if request.node_id in self._infra_fail and not self._healed:
            raise ProviderError(error_class=self._infra_error_class, message="infra fail")
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
            }
        elif request.node_id == "review" and (
            not isinstance(structured, dict) or "findings" not in structured
        ):
            # The review evaluator requires a well-formed findings array; a well-formed empty
            # one is a clean, accepting verdict — the default when a test doesn't override it.
            structured = {"findings": []}
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
        self.trace_calls: list[dict[str, object]] = []
        self.ask_calls: list[dict[str, object]] = []
        self._raise_on_send = raise_on_send
        self._ask_results = list(ask_results or [])

    def send_trace(self, *, task_id: str, node_id: str, outcome: str) -> None:
        self.trace_calls.append({"task_id": task_id, "node_id": node_id, "outcome": outcome})
        if self._raise_on_send:
            raise RuntimeError("trace failed")

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
        self.calls.append(
            {
                "task_id": task_id,
                "final_status": final_status,
                "pr_url": pr_url,
                "reason": reason,
                "contacts": contacts,
                "governance_changed": governance_changed,
                "details": details,
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
    clock: Callable[[], str] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[Orchestrator, StateStore, Ledger, Path]:
    from tests.conftest import seed_builtin_flows

    from wastech_orchestrator.checks.resolver import CheckResolver
    from wastech_orchestrator.routing.router import AgentRouter

    art = tmp_path / "art"
    config = make_git_config(git_repo.clone, checks=["pytest"], **(config_kwargs or {}))
    seed_builtin_flows(git_repo.clone)  # deliver the built-in flows as `worc install` would
    store = StateStore.open(art / "state.db")
    ledger = Ledger(art / "logs")
    # A no-op sleep so the Router's transient backoff never actually waits in tests.
    cancel_kwargs = {"is_cancelled": is_cancelled} if is_cancelled is not None else {}
    router = AgentRouter(config, providers, sleep=lambda _d: None, **cancel_kwargs)  # type: ignore[arg-type]
    git = GitManager(config, store=store, artifacts_root=str(art), gh_runner=gh or _fake_gh())
    checks = CheckRunner(config, run_process=_fake_proc(check_verdicts))  # type: ignore[arg-type]
    gate = ValidationGate(
        config,
        store_has_task_id=store.task_id_exists,
        ledger_has_task_id=ledger.has_task_id,
    )
    extra: dict[str, object] = {}
    if clock is not None:
        extra["clock"] = clock
    if is_cancelled is not None:
        extra["is_cancelled"] = is_cancelled
    orch = Orchestrator(
        config,
        router=router,
        git=git,
        checks=checks,
        store=store,
        ledger=ledger,
        gate=gate,
        layout=RuntimeLayout(
            repo_root=Path(config.repo.local_path),
            control_home=Path(config.repo.local_path) / ".worc",
            private_home=art,
            exchange_root=Path(config.repo.local_path) / ".worc-io",
        ),
        notifier=notifier,
        resolver=CheckResolver(config),  # normalize checks.command_sets (production wires this)
        **extra,
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


def _both(*, claude=None, codex=None, **kwargs) -> dict[ProviderId, FakeProvider]:
    """Both fakes from one shared kwarg set, with optional per-provider overrides.

    A MIXED pair — each provider failing with its own error class — is what reproduces a broken
    fallback masking a park-eligible primary; one shared kwarg set cannot express it. The shared
    path is unchanged, so every existing call site keeps working untouched.
    """
    return {
        ProviderId.CLAUDE: FakeProvider("claude", **{**kwargs, **(claude or {})}),
        ProviderId.CODEX: FakeProvider("codex", **{**kwargs, **(codex or {})}),
    }


def _impl_writes_file(provider_id: str) -> FakeProvider:
    # Implementation must actually change the working tree so there is something to commit.
    return FakeProvider(provider_id, outputs={"implementation": ("implemented", None)})


def test_notify_terminal_enriches_manual_from_failure_report(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # _notify_terminal assembles TerminalDetails from the TaskRow + on-disk failure report
    # for a needs-attention terminal — stop node, loop, the most-severe blocking finding, and the
    # stuck.md report path — all keyed by task_id, so every call site enriches without extra
    # plumbing. The most-severe finding wins over a low nit in the same report.
    from wastech_orchestrator.notify import TerminalDetails

    notifier = RecordingNotifier()
    orch, store, _ledger, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        notifier=notifier,
    )
    task_dir = task_artifact_dir(art, "task-mar")
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "failure_report.json").write_text(
        json.dumps(
            {
                "loop": "review_fix",
                "last_review_findings": [
                    {"severity": "low", "reason": "a nit", "paths": ["b.py"]},
                    {
                        "severity": "high",
                        "reason": "AGENTS.md was never modified",
                        "paths": ["AGENTS.md"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    store.insert_task(
        TaskRow(
            task_id="task-mar",
            title="Fix governance docs",
            status=Status.MANUAL_ACTION_REQUIRED,
            branch="feat/x",
            fix_iterations=2,
            failure_report_path=str(task_dir / "failure_report.json"),
        )
    )
    store.update_task("task-mar", current_node="review")

    orch._notify_terminal(
        task_id="task-mar",
        final_status=Status.MANUAL_ACTION_REQUIRED,
        pr_url=None,
        reason="no_file_change",
    )

    details = notifier.calls[0]["details"]
    assert isinstance(details, TerminalDetails)
    assert details.title == "Fix governance docs"
    assert details.stop_node == "review" and details.loop == "review_fix"
    assert details.fix_rounds == 2
    assert details.finding is not None
    assert details.finding.severity == "high"  # most-severe finding wins over the low nit
    assert details.finding.paths == ("AGENTS.md",)
    assert details.report_path is not None and details.report_path.endswith("stuck.md")


def test_notify_terminal_done_passes_no_details(git_repo, make_git_config, tmp_path: Path) -> None:
    # A clean done carries no enrichment (stays terse) — details is None.
    notifier = RecordingNotifier()
    orch, store, _ledger, _art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        notifier=notifier,
    )
    store.insert_task(TaskRow(task_id="task-done", title="t", status=Status.DONE, branch="feat/x"))

    orch._notify_terminal(
        task_id="task-done", final_status=Status.DONE, pr_url="https://example/pr/2", reason=None
    )

    assert notifier.calls[0]["details"] is None


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
            "governance_changed": (),  # ordinary task touched no governance files
            "details": None,  # a clean done stays terse (no enrichment)
        }
    ]
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # The commit landed on the task branch (epoch-prefixed; read the actual name from the store).
    assert row.branch is not None
    branches = git_run(["branch", "--list", row.branch], git_repo.clone)
    assert row.branch in branches


def _run_happy_task_with_trace(
    git_repo, make_git_config, tmp_path: Path, *, telegram_trace: bool
) -> RecordingNotifier:
    """Drive one complete happy-path task, returning the notifier that recorded its step traces."""
    providers = _both()
    notifier = RecordingNotifier()
    orch, _store, _ledger, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
        config_kwargs={"telegram_trace": telegram_trace},
    )
    task_file = _complete_task(tmp_path)
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]
    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE
    return notifier


def test_step_trace_emits_one_message_per_executed_node(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    notifier = _run_happy_task_with_trace(git_repo, make_git_config, tmp_path, telegram_trace=True)
    traced = {c["node_id"] for c in notifier.trace_calls}
    # One trace per executed node finish; the skipped refinement node emits nothing.
    assert {"planning", "implementation", "testing", "review", "publish"} <= traced
    assert "refinement" not in traced
    # Every call carries the task id + the node's edge-selecting outcome (no secrets, no payload).
    impl = [c for c in notifier.trace_calls if c["node_id"] == "implementation"]
    assert impl == [{"task_id": "task-001", "node_id": "implementation", "outcome": "done"}]
    assert all(c["task_id"] == "task-001" for c in notifier.trace_calls)


def test_step_trace_off_by_default_emits_nothing(git_repo, make_git_config, tmp_path: Path) -> None:
    notifier = _run_happy_task_with_trace(git_repo, make_git_config, tmp_path, telegram_trace=False)
    assert notifier.trace_calls == []


# A flow whose review is a NON-blocking evaluator with a tiny rework budget: it reworks once, then
# (budget spent, finding still open) accepts and continues → exercises the rework-exhausted signal.
_NON_BLOCKING_REVIEW_FLOW = """
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
    - id: review
      kind: evaluator
      role: review
      role_file: roles/review.md
      blocking: false
      max_rework_per_stage: 1
    - id: fixing
      kind: agent
      role_file: roles/fixing.md
      session_scope: editing_lineage
      permission_profile: workspace-write
    - id: publish
      kind: publish
      policy: pull_request
  edges:
    - { from: implementation, to: review }
    - { from: review, to: fixing, outcome: rework, loop: review_fix }
    - { from: fixing, to: review }
    - { from: review, to: publish, outcome: accept }
  budgets:
    global_fix_iterations: 30
    review_fix: 5
"""


def test_rework_budget_exhausted_warns_operator_and_marks_trace(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # When a non-blocking evaluator spends its whole max_rework_per_stage budget and accepts with a
    # gating finding still open, the flow continues (DONE) but the orchestrator warns the operator
    # (console) and marks the live Telegram trace with the ⚠️ rework-exhausted label so a human
    # knows
    # the stage may need follow-up.
    from wastech_orchestrator.core.flow.registry import FlowRegistry
    from wastech_orchestrator.notify import TRACE_REWORK_EXHAUSTED

    flows = tmp_path / "flows"
    (flows / "roles").mkdir(parents=True)
    (flows / "roles" / "implementation.md").write_text("Implement {task_path}.", "utf-8")
    (flows / "roles" / "review.md").write_text("Review the change.", "utf-8")
    (flows / "roles" / "fixing.md").write_text("Fix the issue.", "utf-8")
    (flows / "implementation.yaml").write_text(_NON_BLOCKING_REVIEW_FLOW, "utf-8")

    gating = {"findings": [{"severity": "high", "path": None, "what": "boom", "fix": None}]}
    providers = _both(outputs={"review": ("needs work", gating)})
    notifier = RecordingNotifier()
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
        config_kwargs={"telegram_trace": True},
    )
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)

    orig = providers[ProviderId.CLAUDE].run
    writes = {"n": 0}

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id in ("implementation", "fixing"):
            writes["n"] += 1
            (git_repo.clone / "feature.py").write_text(f"x = {writes['n']}\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        result = orch.run_task(_complete_task(tmp_path, "task-rbx"))
    finally:
        logger.removeHandler(handler)

    assert result.final_status is Status.DONE  # exhaustion accepts and continues, never manual
    review_traces = [c["outcome"] for c in notifier.trace_calls if c["node_id"] == "review"]
    # review ran twice: pass 1 reworked (budget remaining), pass 2 exhausted → the ⚠️ label.
    assert "rework" in review_traces
    assert TRACE_REWORK_EXHAUSTED in review_traces
    assert any("exhausting its rework budget" in m for m in messages)

    # The finding gated on every pass, so the evaluator recorded it as gating — and because the
    # terminal verdict is an `accept`, the follow-up derivation keeps it (losing a finding that is
    # still open above the gate is the worst outcome) and labels it apart from an ordinary
    # sub-threshold nit. End to end: the flag the evaluator wrote is the flag the wording keys on.
    rows = [r for r in store.get_evaluations("task-rbx") if r.kind == "in_flow_verdict"]
    assert [json.loads(r.findings_json)[0]["gating"] for r in rows] == [True, True]
    assert rows[-1].verdict == "accept"
    follow_ups = json.loads(
        (task_artifact_dir(art, "task-rbx") / "summary.json").read_text("utf-8")
    )["follow_ups"]
    assert [fu["title"] for fu in follow_ups] == ["boom"]
    assert follow_ups[0]["evidence"] == [
        "review evaluator finding still open — rework budget exhausted"
    ]


def _run_complete_task_store_dir(
    git_repo, make_git_config, tmp_path: Path, *, memory_enabled: bool
) -> Path:
    """Drive one complete happy-path task; return the private-home ``memory`` store dir.

    The memory store lives under ``layout.private_home`` — here the injected ``art`` dir,
    which coincides with ``<repo>/.worc`` in production. The dir may not exist (memory disabled).
    """
    providers = _both()
    orch, _store, _ledger, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"memory_enabled": memory_enabled},
    )
    task_file = _complete_task(tmp_path)
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]
    assert orch.run_task(task_file).final_status is Status.DONE
    return art / "memory"


def test_memory_disabled_run_writes_no_store(git_repo, make_git_config, tmp_path: Path) -> None:
    # The default (disabled) run is byte-for-byte the pre-memory behavior — the same task
    # completes DONE and NO `.worc/memory` store is written at all.
    store_dir = _run_complete_task_store_dir(
        git_repo, make_git_config, tmp_path, memory_enabled=False
    )
    assert not store_dir.exists()


def test_memory_enabled_run_writes_store(git_repo, make_git_config, tmp_path: Path) -> None:
    # The contrast: the same happy-path run with memory enabled writes the store (a short-term
    # episode at minimum) — confirming the disabled run's emptiness above is the toggle's doing.
    store_dir = _run_complete_task_store_dir(
        git_repo, make_git_config, tmp_path, memory_enabled=True
    )
    assert store_dir.is_dir()
    assert list(store_dir.rglob("recent.jsonl")), "expected a short-term episode to be written"


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
    # The packaged implementation flow runs the documentation node after review accepts; its
    # workspace-write edit (here a docs file) joins the same diff the orchestrator commits/
    # publishes, using the same prompt/lineage/commit machinery as the others.
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
    return store._conn.execute(
        "SELECT node_id, kind, verdict FROM evaluations WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()


def _observed_nodes(store: StateStore, task_id: str) -> list[str]:
    """Node ids the supervisor layer actually observed (the payload of its supervisor_step rows)."""
    return [
        json.loads(row.findings_json)["node"]
        for row in store.get_evaluations(task_id)
        if row.kind == "supervisor_step"
    ]


def _supervisor_attempts(store: StateStore, task_id: str) -> list:
    """The supervisor layer's own provider calls — its rows are the ``node_run_id IS NULL`` ones."""
    return store._conn.execute(
        "SELECT id FROM provider_attempts WHERE task_id = ? AND node_run_id IS NULL ORDER BY id",
        (task_id,),
    ).fetchall()


def test_supervisor_layer_costs_one_call_on_a_clean_run_and_still_writes_the_summary(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The headline saving. The packaged `implementation` flow ships `observe.mode: events`, so a run
    # where nothing deviated — no rework, no failed step, no provider fallback — spends the layer's
    # ONLY call on the whole-task finalize. The summary is unaffected because that turn is seeded by
    # the deterministic packet (node_runs + each node's own output file), never by observations, so
    # switching the notes off cannot cost the operator their PR body.
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
    assert _observed_nodes(store, "task-sup") == []  # no deviation → no observation
    assert len([r for r in rows if r["kind"] == "supervisor_final"]) == 1
    # The structural invariant behind the saving: exactly one provider call, the finalize. Under the
    # pre-P1 cadence this was 1 + one per executed observable node.
    assert len(_supervisor_attempts(store, "task-sup")) == 1
    # The in-flow review evaluator also recorded an immutable verdict (a separate kind).
    assert any(r["kind"] == "in_flow_verdict" and r["node_id"] == "review" for r in rows)
    # The summary is always written (no config.summary_enabled gate) and committed as the PR body.
    assert (task_artifact_dir(art, "task-sup") / "summary.md").exists()
    # There is no summary graph node anymore — the layer owns it.
    assert "summary" not in _ran_nodes(store, "task-sup")
    # That one call is labelled with the job it did, so the layer's spend is readable per phase and
    # not as one lump; a graph node's attempts stay unlabelled and out of the layer's report.
    labels = store._conn.execute(
        "SELECT supervisor_function AS fn, node_run_id FROM provider_attempts "
        "WHERE task_id = ? ORDER BY id",
        ("task-sup",),
    ).fetchall()
    assert [r["fn"] for r in labels if r["node_run_id"] is None] == ["finalize"]
    assert {r["fn"] for r in labels if r["node_run_id"] is not None} == {None}


def test_packaged_flow_cadence_narrows_a_broader_global_mode(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A flow's own cadence wins over a broader global one: `implementation.yaml` declares `events`,
    # so an operator running with the debugging-wide `all` still gets deviations-only for this flow.
    # The flow is the narrower authority, and the engine reaches it as data — never by flow name.
    providers = _both()
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"supervisor_observe": "all"},
    )
    _patch_impl_edit(providers, git_repo)

    assert orch.run_task(_complete_task(tmp_path, "task-narrow")).final_status is Status.DONE
    assert _observed_nodes(store, "task-narrow") == []
    assert len(_supervisor_attempts(store, "task-narrow")) == 1  # finalize only


# A flow with no `supervisor:` block at all: it inherits the operator's global cadence, which is
# what
# makes it the vehicle for driving each mode end to end (a packaged flow's own mode would narrow
# it).
_NO_SUPERVISOR_BLOCK_FLOW = """
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
    - id: review
      kind: evaluator
      role: review
      role_file: roles/review.md
    - id: publish
      kind: publish
      policy: pull_request
  edges:
    - { from: implementation, to: review }
    - { from: review, to: publish, outcome: accept }
"""


def _run_with_cadence(
    git_repo,
    make_git_config,
    tmp_path: Path,
    task_id: str,
    *,
    flow_text: str = _NO_SUPERVISOR_BLOCK_FLOW,
    outputs: dict | None = None,
    **config_kwargs: object,
) -> StateStore:
    """Run one task on an operator flow under an explicit global cadence; return the store."""
    from wastech_orchestrator.core.flow.registry import FlowRegistry

    flows = tmp_path / "flows"
    (flows / "roles").mkdir(parents=True, exist_ok=True)
    (flows / "roles" / "implementation.md").write_text("Implement {task_path}.", "utf-8")
    (flows / "roles" / "review.md").write_text("Review the change.", "utf-8")
    (flows / "roles" / "fixing.md").write_text("Fix the issue.", "utf-8")
    (flows / "implementation.yaml").write_text(flow_text, "utf-8")

    providers = _both(outputs=outputs) if outputs else _both()
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs=config_kwargs,
    )
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)
    _patch_impl_edit(providers, git_repo)
    assert orch.run_task(_complete_task(tmp_path, task_id)).final_status is Status.DONE
    return store


def test_observe_mode_all_observes_every_executed_observable_step(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # `all` is the pre-P1 behavior, kept as an explicit debugging choice: one observation per
    # executed
    # node whose kind is observable, plus the finalize.
    store = _run_with_cadence(
        git_repo, make_git_config, tmp_path, "task-all", supervisor_observe="all"
    )
    observed = _observed_nodes(store, "task-all")
    assert observed == ["implementation", "review"]  # `publish` is never observable
    assert len(_supervisor_attempts(store, "task-all")) == len(observed) + 1


def test_observe_mode_none_observes_nothing_but_keeps_the_summary(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    store = _run_with_cadence(
        git_repo, make_git_config, tmp_path, "task-none", supervisor_observe="none"
    )
    assert _observed_nodes(store, "task-none") == []
    assert len(_supervisor_attempts(store, "task-none")) == 1  # the finalize turn survives
    finals = [r for r in _evaluations(store, "task-none") if r["kind"] == "supervisor_final"]
    assert len(finals) == 1


def test_observe_mode_selected_observes_exactly_the_listed_nodes(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    store = _run_with_cadence(
        git_repo,
        make_git_config,
        tmp_path,
        "task-sel",
        supervisor_observe="selected",
        # `documentation` is not in this flow at all — a listed id that never runs is simply not
        # observed, it is not an error.
        supervisor_include_nodes=["review", "documentation"],
    )
    assert _observed_nodes(store, "task-sel") == ["review"]


def test_observe_mode_events_observes_a_rework_deviation_only(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The `events` cadence in action: the review evaluator spends its rework budget and then accepts
    # with the finding still open (`rework_exhausted`), which is the `rework` trigger — so `review`
    # is
    # observed while `implementation` and `fixing`, which did nothing unusual, are not.
    gating = {"findings": [{"severity": "high", "path": None, "what": "boom", "fix": None}]}
    store = _run_with_cadence(
        git_repo,
        make_git_config,
        tmp_path,
        "task-ev",
        flow_text=_NON_BLOCKING_REVIEW_FLOW,
        outputs={"review": ("needs work", gating)},
        supervisor_observe="events",
    )
    # review ran twice: pass 1 reworked, pass 2 accepted with the budget spent — both are
    # deviations.
    assert set(_observed_nodes(store, "task-ev")) == {"review"}
    assert "implementation" not in _observed_nodes(store, "task-ev")
    assert "fixing" not in _observed_nodes(store, "task-ev")


def test_accepted_evaluator_findings_reach_the_pr_body(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # End to end: `review` accepts (the finding is below its `high` gate) but still recorded a
    # finding. Before this, that finding existed only in findings.json and the evaluations table,
    # and
    # the PR body told the operator the gate simply passed. The finalize turn is handed every
    # evaluator's recorded verdict, so the accepted finding reaches the summary — and it does so
    # from
    # durable state, independently of whether the step was ever observed (this run's cadence is the
    # packaged `events`, and a plain accept is not a deviation, so no observation runs at all).
    finding_text = "docstring drift in the new helper"
    providers = _both(
        outputs={
            "review": (
                "I found one advisory issue",
                {"findings": [{"severity": "low", "what": finding_text}]},
            ),
            # The finalize turn is structured here (memory is on), so script a summary: without one
            # it degrades to the deterministic fallback, which carries no follow-ups section.
            "supervisor": ("noted", {"summary": "Added the helper and its tests."}),
        }
    )
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-findings"))
    assert result.final_status is Status.DONE
    assert _observed_nodes(store, "task-findings") == []  # accept is not a deviation

    # The operator surface: the accepted finding lands in the summary that becomes the PR body.
    summary = (task_artifact_dir(art, "task-findings") / "summary.md").read_text("utf-8")
    assert "## Technical debt / follow-ups" in summary
    assert finding_text in summary


def test_an_observed_step_carries_the_evaluator_findings_not_just_the_label(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # When an observation *does* run, it gets the step's substance. Here review exhausts its rework
    # budget and accepts with the finding still open — an accept that is also a deviation, so it is
    # observed. Passing only the outcome label had the observer acknowledge `accept` for a node that
    # had filed a substantive finding, and then describe the gate as having passed.
    finding_text = "docstring drift in the new helper"
    gating = {"findings": [{"severity": "high", "path": None, "what": finding_text, "fix": None}]}
    store = _run_with_cadence(
        git_repo,
        make_git_config,
        tmp_path,
        "task-obs-findings",
        flow_text=_NON_BLOCKING_REVIEW_FLOW,
        outputs={"review": ("I found one advisory issue", gating)},
        supervisor_observe="events",
    )
    assert set(_observed_nodes(store, "task-obs-findings")) == {"review"}
    supervisor_dir = task_artifact_dir(tmp_path / "art", "task-obs-findings") / "stages/supervisor"
    prompts = [path.read_text("utf-8") for path in supervisor_dir.glob("run-*/rendered-prompt.md")]
    review_observation = [p for p in prompts if "Node: review" in p]
    assert review_observation, "the deviating review step was observed"
    assert any(f"- [high] {finding_text}" in p for p in review_observation)
    # The provider's own prose reached the observer too, not only the typed findings.
    assert any("I found one advisory issue" in p for p in review_observation)


def test_supervisor_turns_write_rendered_prompt_and_prompt_audit(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    """The supervisor's own turns are now part of the audit trail: previously rendered-prompt.md
    and the prompt-audit JSON/timeline were never written for observe/finalize turns at all."""
    providers = _both()
    orch, _, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"prompt_audit": True},
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-sup-audit"))
    assert result.final_status is Status.DONE

    supervisor_runs = list(
        (task_artifact_dir(art, "task-sup-audit") / "stages" / "supervisor").glob("run-*")
    )
    assert supervisor_runs, "supervisor turns now have their own stages/supervisor/run-* dirs"
    assert all((run_dir / "rendered-prompt.md").exists() for run_dir in supervisor_runs)

    timeline = (_audit_dir(art, "task-sup-audit") / "timeline.jsonl").read_text().splitlines()
    supervisor_entries = [json.loads(line) for line in timeline if '"supervisor"' in line]
    assert supervisor_entries
    assert all(r["node_id"] == "supervisor" and r["prompt"] for r in supervisor_entries)


def test_finalize_tail_is_logged(git_repo, make_git_config, tmp_path: Path) -> None:
    # The end-of-run tail (whole-task summary → publish prep) emits transition log lines so a
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
    # With decomposition the summary synthesis is once at whole-task close — not per subtask.
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
    # The engine owns loop counting in FlowRunState; the operator-facing tasks.fix_iterations
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
    rows = store._conn.execute(
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
    # preserve-node-run-artifact-history: the per-node history.jsonl indexes every run of a
    # re-running node, one line per pass, in order — so an operator reads the sequence without
    # listing run-*/ dirs. fixing ran twice, so its index has both runs.
    history = art / "logs" / "task-two-fixes" / "stages" / "fixing" / "history.jsonl"
    entries = [json.loads(line) for line in history.read_text("utf-8").splitlines()]
    assert [e["run_id"] for e in entries] == [row["id"] for row in rows]
    assert all(e["node_id"] == "fixing" and e["kind"] == "agent" for e in entries)


def test_resume_restores_review_path_from_latest_verdict_run(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # preserve-node-run-artifact-history: on resume, review_path is rebuilt from the store's latest
    # in_flow_verdict run dir — NOT the last evaluations row, which is a supervisor_step carrying no
    # node_id/run_id (that would yield a bogus stages/None/ path and silently lose the input).
    from types import SimpleNamespace

    from wastech_orchestrator.core.flow.nodes import NodeInputs
    from wastech_orchestrator.state_store import EvaluationRow

    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    tid = "task-resume-review"
    store.insert_task(TaskRow(task_id=tid, title="t", status=Status.RUNNING))
    store.record_evaluation(
        EvaluationRow(
            task_id=tid,
            kind="in_flow_verdict",
            verdict="rework",
            node_id="review",
            source_node_run_id=5,
        )
    )
    store.record_evaluation(
        EvaluationRow(
            task_id=tid,
            kind="in_flow_verdict",
            verdict="accept",
            node_id="review",
            source_node_run_id=9,
        )
    )  # newest verdict
    store.record_evaluation(EvaluationRow(task_id=tid, kind="supervisor_step", verdict="advisory"))
    findings = node_run_dir(art, tid, "review", 9) / "findings.json"
    findings.parent.mkdir(parents=True, exist_ok=True)
    findings.write_text('{"findings": []}', encoding="utf-8")

    inputs = NodeInputs(flow_dir=str(tmp_path))
    orch._restore_engine_inputs(SimpleNamespace(task=SimpleNamespace(id=tid)), inputs)
    # Recovery re-publishes the latest verdict's findings into the exchange and points {review_path}
    # there; the private findings.json stays the audit record.
    expected = (
        exchange_node_run_dir(orch._exchange_root, tid, "review", 9) / "findings.json"
    ).as_posix()
    assert inputs.review_path == expected
    assert findings.exists()


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
    # The third terminal with no prose by design gets the same deterministic report — one renderer
    # writes the body on every terminal — and no degradation callout, because none was expected.
    summary = (art / "logs" / "task-rev-infra" / "summary.md").read_text(encoding="utf-8")
    assert "## Changes" in summary and "## Steps" in summary
    assert "- `implementation` (agent):" in summary
    assert "Fallback summary" not in summary


def test_review_infra_empty_diff_annotates_reason(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # EXPERIMENTAL(no-work-infra) — remove with the feature.
    # Honest terminal: when the evaluator-infra degrade-to-manual fires with NO working-tree
    # change, the reason must say so plainly, so the manual terminal never implies a diff to review
    # that does not exist. Here implementation makes no edit (no _patch_impl_edit), so the diff is
    # empty when review infra-fails.
    providers = _both(infra_fail={"review"})
    orch, store, _ledger, _art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )

    result = orch.run_task(_complete_task(tmp_path, "task-rev-empty"))
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    task = store.get_task("task-rev-empty")
    assert task is not None and task.cleanup_last_error is not None
    assert "no changes were produced to review" in task.cleanup_last_error


# --- B-lite: transient-infra exhaustion parks the task as resumable -----------------------------


class _Clock:
    """A controllable wall clock (ISO strings) for the B-lite max_blocked ceiling test."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> str:
        return datetime.fromtimestamp(self._t, tz=UTC).isoformat()

    def advance(self, seconds: float) -> None:
        self._t += seconds


def test_transient_exhaustion_parks_task_resumable(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Every provider is transiently unavailable on implementation (PROVIDER_UNAVAILABLE): the task
    # must NOT go terminal — it parks as resumable (still RUNNING, blocked_since stamped), with no
    # failure report and no ledger record.
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-park"))

    assert result.final_status is Status.RUNNING  # parked, not terminal
    task = store.get_task("task-park")
    assert task is not None
    assert task.status is Status.RUNNING
    assert task.blocked_since is not None
    assert ledger.records() == []  # no terminal ledger record yet
    assert not (art / "logs" / "task-park" / "failure_report.json").exists()
    assert "publish" not in _ran_nodes(store, "task-park")


def test_rate_limited_exhaustion_parks_task_resumable(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A subscription/session limit that exhausts every provider (RATE_LIMITED) must NOT go terminal
    # It parks as resumable (RUNNING, blocked_since stamped) and
    # waits out the reset — no failure report, no ledger record, no burned queue / fix budget.
    providers = _both(infra_fail={"implementation"}, infra_error_class=ErrorClass.RATE_LIMITED)
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-limit"))

    assert result.final_status is Status.RUNNING  # parked, not terminal FAILED
    task = store.get_task("task-limit")
    assert task is not None
    assert task.status is Status.RUNNING
    assert task.blocked_since is not None
    assert ledger.records() == []
    assert not (art / "logs" / "task-limit" / "failure_report.json").exists()


def test_containment_unverified_goes_manual_action_required(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # An unproven provider process tree on implementation is a security / manual-action
    # condition. Unlike a transient infra class it must NOT park (an auto-resume must not paper over
    # an uncontained writer), and unlike a quality failure it must NOT fall back — the task goes
    # terminal MANUAL_ACTION_REQUIRED so an operator intervenes, and publish never runs.
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.CONTAINMENT_UNVERIFIED
    )
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-contain"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    task = store.get_task("task-contain")
    assert task is not None and task.status is Status.MANUAL_ACTION_REQUIRED
    assert "publish" not in _ran_nodes(store, "task-contain")  # nothing downstream ran
    assert ledger.records()[0]["final_status"] == "manual_action_required"


def test_live_control_plane_edit_during_run_is_manual_not_fallback(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # An agent that rewrites a live control file (here the flow YAML) during a provider
    # attempt is caught by the post-node verify — the provider tree is already proven quiescent
    # so a live diff means a control file was mutated under the run. It is a non-fallback
    # manual-action security violation, and no downstream node runs on the provider-selected bytes.
    providers = _both()
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    flow_yaml = git_repo.clone / ".worc" / "flows" / "implementation.yaml"
    orig = providers[ProviderId.CLAUDE].run

    def run_mutate(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "planning":  # mutate a live control input mid-attempt
            flow_yaml.write_text(
                flow_yaml.read_text(encoding="utf-8") + "\n# tampered by agent\n", encoding="utf-8"
            )
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_mutate  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-mut"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    ran = _ran_nodes(store, "task-mut")
    assert "planning" in ran  # the mutating node completed
    assert "implementation" not in ran  # ...but nothing downstream ran on the mutated control
    assert "publish" not in ran
    assert ledger.records()[0]["final_status"] == "manual_action_required"


def test_autorecovery_after_parked_live_edit_stays_conflict_manual(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # AUTOMATIC crash-recovery — `resume()` WITHOUT an operator `continue_task`
    # — must NOT adopt a live control-plane edit made while the task was parked. A crash can follow
    # an agent mutation, so auto-recovery keeps the frozen bundle and refuses the drift, routing to
    # manual. (An operator `rerun --continue` deliberately DOES adopt — see the next test.)
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    first = orch.run_task(_complete_task(tmp_path, "task-parkconf"))
    assert first.final_status is Status.RUNNING  # parked, resumable

    for provider in providers.values():
        provider.heal()
    flow_yaml = git_repo.clone / ".worc" / "flows" / "implementation.yaml"
    flow_yaml.write_text(
        flow_yaml.read_text(encoding="utf-8") + "\n# operator edit while parked\n", encoding="utf-8"
    )

    result = orch.resume()  # auto-recovery path (not continue_task) — no adopt marker

    assert result is not None and result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert ledger.records()[0]["final_status"] == "manual_action_required"


def test_continue_task_after_parked_live_edit_adopts_flow(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # An operator `rerun --continue` (continue_task) after editing the live flow while parked
    # ADOPTS the edit — it re-freezes the control plane from the current on-disk flow, records a new
    # digest, and resumes to DONE rather than refusing like auto-recovery above. Agent-tamper
    # detection during the resumed run is unaffected (the post-node hook rebaselines to the new
    # digest).
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    first = orch.run_task(_complete_task(tmp_path, "task-adopt"))
    assert first.final_status is Status.RUNNING  # parked, resumable
    control_before = store.get_control_bundle_digest("task-adopt")
    inst_before = store.get_instruction_manifest_digest("task-adopt")

    for provider in providers.values():
        provider.heal()
    flow_yaml = git_repo.clone / ".worc" / "flows" / "implementation.yaml"
    flow_yaml.write_text(
        flow_yaml.read_text(encoding="utf-8") + "\n# operator edit while parked\n", encoding="utf-8"
    )

    result = orch.continue_task("task-adopt")  # operator --continue → adopt the edited flow

    assert result.final_status is Status.DONE  # adopted the edited flow and resumed, not refused
    assert store.get_control_bundle_digest("task-adopt") != control_before  # re-frozen on adopt
    # The composite identity stays consistent: the instruction manifest re-binds the new
    # control digest, so its persisted digest changes too.
    assert store.get_instruction_manifest_digest("task-adopt") != inst_before
    assert "publish" in _ran_nodes(store, "task-adopt")  # resumed past implementation
    assert ledger.records()[0]["final_status"] == "done"


def test_stop_cancellation_parks_task_resumable(git_repo, make_git_config, tmp_path: Path) -> None:
    # Reliable-stop: an operator stop kills the implementation agent (surfaces as PROCESS_CRASHED),
    # the Router reclassifies it as CANCELLED (is_cancelled True) instead of falling back, and the
    # Core parks the task resumable — never a fallback respawn, never a terminal FAILED report.
    providers = _both(infra_fail={"implementation"}, infra_error_class=ErrorClass.PROCESS_CRASHED)
    stop = {"requested": False}
    orch, store, ledger, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        is_cancelled=lambda: stop["requested"],
    )
    _patch_impl_edit(providers, git_repo)
    original_run = providers[ProviderId.CLAUDE].run

    def cancel_during_implementation(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            stop["requested"] = True
        return original_run(request)

    providers[ProviderId.CLAUDE].run = cancel_during_implementation  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-cancel"))

    assert result.final_status is Status.RUNNING  # parked, not terminal FAILED
    task = store.get_task("task-cancel")
    assert task is not None
    assert task.status is Status.RUNNING
    assert task.blocked_since is not None
    assert ledger.records() == []  # no terminal ledger record
    assert not (art / "logs" / "task-cancel" / "failure_report.json").exists()
    # No fallback respawn after the stop: the second provider never ran the implementation node.
    impl_runs = [
        r
        for provider in providers.values()
        for r in provider.requests
        if r.node_id == "implementation"
    ]
    assert len(impl_runs) == 1


def test_boundary_cancellation_parks_then_resumes_from_untouched_node(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    providers = _both()
    stop = {"requested": False}
    orch, store, ledger, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        is_cancelled=lambda: stop["requested"],
    )
    _patch_impl_edit(providers, git_repo)
    original_run = providers[ProviderId.CLAUDE].run

    def stop_after_planning(request: AgentRunRequest) -> AgentRunResult:
        result = original_run(request)
        if request.node_id == "planning":
            stop["requested"] = True
        return result

    providers[ProviderId.CLAUDE].run = stop_after_planning  # type: ignore[method-assign]

    first = orch.run_task(_complete_task(tmp_path, "task-boundary-cancel"))

    assert first.final_status is Status.RUNNING
    row = store.get_task("task-boundary-cancel")
    assert row is not None and row.status is Status.RUNNING and row.blocked_since is not None
    assert store.get_flow_checkpoint("task-boundary-cancel")[0] == "implementation"
    assert not any(
        request.node_id == "implementation"
        for provider in providers.values()
        for request in provider.requests
    )
    assert ledger.records() == []
    assert not (art / "logs" / "task-boundary-cancel" / "failure_report.json").exists()

    stop["requested"] = False
    resumed = orch.resume()

    assert resumed is not None and resumed.final_status is Status.DONE
    implementation_runs = [
        request
        for provider in providers.values()
        for request in provider.requests
        if request.node_id == "implementation"
    ]
    assert len(implementation_runs) == 1


def test_parked_task_resumes_when_provider_recovers(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # After parking, the outage clears: resume() continues from the checkpoint to DONE, clearing
    # blocked_since. The implementation is committed exactly once (the parked run never committed).
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    first = orch.run_task(_complete_task(tmp_path, "task-resume"))
    assert first.final_status is Status.RUNNING

    for provider in providers.values():
        provider.heal()
    result = orch.resume()

    assert result is not None and result.final_status is Status.DONE
    task = store.get_task("task-resume")
    assert task is not None and task.blocked_since is None  # cleared at terminal
    assert ledger.records()[0]["final_status"] == "done"
    assert "publish" in _ran_nodes(store, "task-resume")


def test_parked_task_fails_after_max_blocked(git_repo, make_git_config, tmp_path: Path) -> None:
    # A sustained outage: the task stays parked past agents.retry.max_blocked_s (default 6h) →
    # on the next resume it goes terminal FAILED (nothing hangs forever).
    clock = _Clock()
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0], clock=clock
    )
    _patch_impl_edit(providers, git_repo)

    first = orch.run_task(_complete_task(tmp_path, "task-ceiling"))
    assert first.final_status is Status.RUNNING

    clock.advance(21600 + 60)  # past the default max_blocked_s
    result = orch.resume()

    assert result is not None and result.final_status is Status.FAILED
    assert (art / "logs" / "task-ceiling" / "failure_report.json").exists()
    assert ledger.records()[0]["final_status"] == "failed"


def test_non_transient_infra_still_fails_immediately(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A non-transient infra class (TIMEOUT) is NOT parked — it goes terminal FAILED at once, with a
    # failure report and no blocked_since. Only the transient classes earn a soft pause.
    providers = _both(infra_fail={"implementation"})  # default error class is TIMEOUT
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-hard"))

    assert result.final_status is Status.FAILED
    task = store.get_task("task-hard")
    assert task is not None and task.blocked_since is None
    assert (art / "logs" / "task-hard" / "failure_report.json").exists()


def test_no_work_exhaustion_fails_task(git_repo, make_git_config, tmp_path: Path) -> None:
    # EXPERIMENTAL(no-work-infra) — remove with the feature.
    # A no-work run that exhausts every provider (AGENT_NO_PROGRESS — fallback-eligible but NOT
    # park-eligible) goes terminal FAILED with a failure report, never a park (the single queue slot
    # must not be held for a possibly-permanent dead run) and never fed into the review/fix loop.
    providers = _both(infra_fail={"implementation"}, infra_error_class=ErrorClass.AGENT_NO_PROGRESS)
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-nowork"))

    assert result.final_status is Status.FAILED
    task = store.get_task("task-nowork")
    assert task is not None and task.blocked_since is None
    assert (art / "logs" / "task-nowork" / "failure_report.json").exists()
    assert ledger.records()[0]["final_status"] == "failed"


# --- the exhaustion class is aggregated across attempts, not taken from the last one -------------


def _impl_attempts(providers: dict[ProviderId, FakeProvider]) -> list[AgentRunRequest]:
    return [
        r
        for provider in providers.values()
        for r in provider.requests
        if r.node_id == "implementation"
    ]


def test_mixed_class_exhaustion_parks_on_the_park_eligible_attempt(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The primary hits a subscription limit and the fallback dies on expired credentials, so the
    # class the Router settles on is the fallback's — which is not park-eligible. The task must
    # still park: whether work survives must not depend on how badly a provider that ran nothing
    # failed.
    providers = _both(
        infra_fail={"implementation"},
        claude={"infra_error_class": ErrorClass.RATE_LIMITED},
        codex={"infra_error_class": ErrorClass.AUTHENTICATION_FAILED},
    )
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-mixed"))

    assert result.final_status is Status.RUNNING  # parked, not terminal FAILED
    task = store.get_task("task-mixed")
    assert task is not None and task.status is Status.RUNNING
    assert task.blocked_since is not None
    assert ledger.records() == []
    assert not (art / "logs" / "task-mixed" / "failure_report.json").exists()
    assert "publish" not in _ran_nodes(store, "task-mixed")
    # Both providers really were tried, so the park is not passing for want of a fallback hop.
    assert len(_impl_attempts(providers)) == 2


def test_containment_on_the_fallback_is_manual_not_parked(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Security outranks a resumable sibling: a rate-limited primary must never let an unproven
    # process tree on the fallback auto-resume. The task goes to manual with the exchange flagged
    # unsafe (so the terminal seam holds the tree) and is NOT parked.
    providers = _both(
        infra_fail={"implementation"},
        claude={"infra_error_class": ErrorClass.RATE_LIMITED},
        codex={"infra_error_class": ErrorClass.CONTAINMENT_UNVERIFIED},
    )
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-mixed-contain"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    task = store.get_task("task-mixed-contain")
    assert task is not None and task.status is Status.MANUAL_ACTION_REQUIRED
    assert task.blocked_since is None  # never parked
    assert store.get_exchange_guard("task-mixed-contain")[1] is True  # active exchange unsafe
    assert "publish" not in _ran_nodes(store, "task-mixed-contain")
    assert ledger.records()[0]["final_status"] == "manual_action_required"


def test_cancel_park_keeps_the_cancel_class_over_a_park_eligible_attempt(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # An operator stop that lands on a rate-limited stage still reads as stopped, not as waiting on
    # a provider window. Both dispositions park today, so the recorded class is the only observable
    # difference — assert it, because it is what decides whether a wake instant may be inherited.
    providers = _both(
        infra_fail={"implementation"}, claude={"infra_error_class": ErrorClass.RATE_LIMITED}
    )
    stop = {"requested": False}
    orch, store, ledger, _art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        is_cancelled=lambda: stop["requested"],
    )
    _patch_impl_edit(providers, git_repo)
    original_run = providers[ProviderId.CLAUDE].run

    def cancel_during_implementation(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            stop["requested"] = True
        return original_run(request)

    providers[ProviderId.CLAUDE].run = cancel_during_implementation  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-cancel-limit"))

    assert result.final_status is Status.RUNNING
    task = store.get_task("task-cancel-limit")
    assert task is not None and task.blocked_since is not None
    assert ledger.records() == []
    impl_run = [
        r for r in store.get_node_runs("task-cancel-limit") if r.node_id == "implementation"
    ][-1]
    assert impl_run.error_class == ErrorClass.CANCELLED.value
    # The killed attempt's own row keeps the class the provider actually raised.
    attempt_classes = [a.error_class for a in store.get_provider_attempts(impl_run.id)]
    assert attempt_classes == [ErrorClass.RATE_LIMITED.value]


def test_rate_limited_evaluator_parks_then_resumes_to_done(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # An evaluator that cannot run degrades to manual to preserve an already-green diff. That is
    # the right answer for "could not run, ever" — not for "the window resets shortly". Parking
    # preserves strictly more: the diff survives AND the review still runs, with no operator.
    providers = _both(infra_fail={"review"}, infra_error_class=ErrorClass.RATE_LIMITED)
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    first = orch.run_task(_complete_task(tmp_path, "task-eval-limit"))

    assert first.final_status is Status.RUNNING  # parked, not degraded to manual
    parked = store.get_task("task-eval-limit")
    assert parked is not None and parked.blocked_since is not None
    assert ledger.records() == []
    assert not (art / "logs" / "task-eval-limit" / "failure_report.json").exists()

    for provider in providers.values():
        provider.heal()
    result = orch.resume()

    assert result is not None and result.final_status is Status.DONE
    task = store.get_task("task-eval-limit")
    assert task is not None and task.blocked_since is None
    assert "review" in _ran_nodes(store, "task-eval-limit")
    assert "publish" in _ran_nodes(store, "task-eval-limit")


def test_infra_stuck_report_names_every_provider_attempt(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A terminal that names only the class the Router settled on hides the real cause and the fact a
    # fallback was tried at all — which is what made the incident expensive to diagnose.
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.AUTHENTICATION_FAILED
    )
    orch, store, _ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-report"))
    assert result.final_status is Status.FAILED

    report = json.loads(
        (art / "logs" / "task-report" / "failure_report.json").read_text(encoding="utf-8")
    )
    assert report["node_id"] == "implementation"
    attempts = report["provider_attempts"]
    assert {a["provider"] for a in attempts} == {"claude", "codex"}
    assert {a["error_class"] for a in attempts} == {ErrorClass.AUTHENTICATION_FAILED.value}
    # Only the operator-facing fields: no private-tree path, no usage/cost columns.
    assert all(
        set(a) == {"provider", "attempt", "error_class", "exit_code", "started_at"}
        for a in attempts
    )

    stuck = (art / "logs" / "task-report" / "stuck.md").read_text(encoding="utf-8")
    assert "## Provider attempts" in stuck
    assert "claude" in stuck and "codex" in stuck
    assert "This task could not run:" in stuck
    assert "fix loop exhausted" not in stuck  # no loop ran and no budget was spent


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
    # A degenerate flow (implementation → publish, no checks / review / fixing) is a valid
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
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-min"))
    assert result.final_status is Status.DONE
    ran = _ran_nodes(store, "task-min")
    assert set(ran) == {"implementation", "publish"}  # only the two declared nodes ran
    assert "testing" not in ran and "review" not in ran  # degenerate: no checks / review nodes
    assert (task_artifact_dir(art, "task-min") / "summary.md").exists()  # supervisor still wrote it


def test_summary_fallback_when_provider_fails(git_repo, make_git_config, tmp_path: Path) -> None:
    # Both providers fail the supervisor's summary synthesis with an infra error → the
    # deterministic report, still DONE. The layer runs under its own "supervisor" node id.
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
    # A real report of what the run did, not the four-field stub whose `## How` read "No
    # provider-authored summary was available".
    assert "## Changes" in summary and "## Steps" in summary
    assert "- `implementation` (agent):" in summary
    assert "- `s.py`" in summary  # the changed path, derived from the durable current.diff
    for dead in ("## How", "## Integration", "No provider-authored summary was available"):
        assert dead not in summary
    # The layer ran and could not finish, so its spend is still recorded beside the degradation.
    metadata = json.loads((art / "logs" / "task-006" / "summary.json").read_text(encoding="utf-8"))
    assert metadata["degraded"] is True
    assert metadata["supervisor_usage"]["total"]["calls"] >= 1


@contextmanager
def _collected_warnings() -> Iterator[list[str]]:
    """Collect ``wastech_orchestrator`` WARNING messages emitted inside the block.

    A handler on the package logger rather than ``caplog``: the per-task logger is a bound adapter
    whose records do not reach pytest's capture handler.
    """
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


def test_degraded_summary_is_loud_on_done_path(git_repo, make_git_config, tmp_path: Path) -> None:
    # Decision A (a): when a provider-authored synthesis was expected on the publish path but
    # failed, the deterministic fallback is marked loud — a WARNING plus a visible callout in the
    # PR body — so a stub is never mistaken for the full synthesis.
    finding_text = "docstring drift in the new helper"
    providers = _both(
        infra_fail={"supervisor"},
        outputs={"review": ("advisory", {"findings": [{"severity": "low", "what": finding_text}]})},
    )
    orch, _, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    task_file = _complete_task(tmp_path, "task-degraded")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "s.py").write_text("b = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    with _collected_warnings() as messages:
        result = orch.run_task(task_file)

    assert result.final_status is Status.DONE
    summary = (art / "logs" / "task-degraded" / "summary.md").read_text(encoding="utf-8")
    assert "Fallback summary" in summary  # visible degradation callout in the PR body
    assert any("summary degraded to deterministic fallback" in m for m in messages)
    # The p0-2 hole, closed: a finding the gate let past used to land in summary.json and vanish
    # from the PR body on exactly this path, because the derivation lived inside the turn that
    # failed. It is deterministic, so it survives the failure.
    assert "## Technical debt / follow-ups" in summary
    assert finding_text in summary


def test_native_memory_opt_in_is_announced_per_run(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The one relaxation whose effects land OUTSIDE the run's audit: Claude's own per-project memory
    # store lives in the operator's HOME, so what a task writes there escapes the frozen bundle, the
    # diff, and the redaction net — and a later task on the same repo reads it. The hatch stays (it
    # is operator-owned) but it is never silent, like read-isolation and git-evidence before it.
    for opted_in, announced in ((True, True), (False, False)):
        providers = _both()
        orch, _, _, _ = _build(
            git_repo,
            make_git_config,
            tmp_path / f"native-{int(opted_in)}",
            providers=providers,
            check_verdicts=[0],
            config_kwargs={"allow_native_memory": opted_in},
        )
        _patch_impl_edit(providers, git_repo)
        with _collected_warnings() as messages:
            result = orch.run_task(_complete_task(tmp_path, f"task-native-{int(opted_in)}"))
        assert result.final_status is Status.DONE
        assert any("native Claude memory ON" in m for m in messages) is announced


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
    # In decomposition mode each subtask's edit nodes must be scoped to that subtask — the
    # active immutable spec path (and "subtask N of M") is rendered into the implementation prompt,
    # so subtask 1 sees 01-first.md and subtask 2 sees 02-second.md.
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


def test_subtask_handoff_context_reaches_successor_implementation(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # subtask-context-handoff: each successor subtask's implementation prompt receives
    # {predecessor_context} pointing at a handoff brief assembled from its depends_on predecessors
    # (the deterministic factual floor). A diamond (3 <- [1,2]) selects BOTH predecessors; subtask 1
    # (no deps) gets no brief. The briefs live under logs/ — never in the memory tiers.
    subtasks = {
        "decompose": True,
        "subtasks": [
            {
                "order": 1,
                "title": "First",
                "slug": "first",
                "acceptance_criteria": ["crit-one"],
                "depends_on": [],
            },
            {
                "order": 2,
                "title": "Second",
                "slug": "second",
                "acceptance_criteria": ["crit-two"],
                "depends_on": [1],
            },
            {
                "order": 3,
                "title": "Third",
                "slug": "third",
                "acceptance_criteria": ["crit-three"],
                "depends_on": [1, 2],
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
    orch, _store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"decomposition": True},
    )
    result = orch.run_task(_complete_task(tmp_path, "task-hnd"))
    assert result.final_status is Status.DONE

    impl_prompts = [
        r.prompt for r in providers[ProviderId.CLAUDE].requests if r.node_id == "implementation"
    ]
    assert len(impl_prompts) == 3
    assert ".handoff.md" not in impl_prompts[0]  # subtask 1 has no predecessors → no brief injected
    assert "02-second.handoff.md" in impl_prompts[1]  # subtask 2 reads its brief
    assert "03-third.handoff.md" in impl_prompts[2]  # subtask 3 reads its brief

    subtasks_dir = task_artifact_dir(art, "task-hnd") / "subtasks"
    # The floor is assembled from existing artifacts: predecessor spec pointer + acceptance criteria
    # + changed files. Subtask 2's brief names predecessor 1.
    h2 = (subtasks_dir / "02-second.handoff.md").read_text("utf-8")
    assert "01-first.md" in h2 and "crit-one" in h2 and "Changed files" in h2
    # The diamond: subtask 3's brief names BOTH predecessors 1 and 2.
    h3 = (subtasks_dir / "03-third.handoff.md").read_text("utf-8")
    assert "01-first.md" in h3 and "02-second.md" in h3
    assert "crit-one" in h3 and "crit-two" in h3
    # Reading the briefs from logs/<task>/subtasks/ IS the memory-tier isolation: they are written
    # to the transient task-scoped dir, never to the .worc/memory/ store.
    assert not (subtasks_dir / "01-first.handoff.md").exists()  # no brief for the depless subtask


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
    # Pre-seed another active task occupying the slot, parked at a checkpoint node.
    store.insert_task(TaskRow(task_id="other", title="o", status=Status.RUNNING))
    store.save_flow_checkpoint(
        "other",
        current_node="planning",
        counters_json="{}",
        flow_fingerprint="fp",
        fix_iterations=0,
    )
    # The refusal names the blocker (id + node) so it reads as "resumable task", not "live run".
    with pytest.raises(SlotBusyError, match=r"another task is active: other at node planning"):
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
            "governance_changed": (),
            "details": None,  # a validation reject has no tasks row → no enrichment
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
        lambda _config, _checks: [
            "codex: sandbox 'danger-full-access' grants full filesystem access"
        ],
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
    # A failed terminal legitimately has no synthesis — the deterministic report is the expected
    # artifact, not a degradation, so it carries no "Fallback summary" callout (Decision A (a)).
    failed_summary = git_run(
        ["show", f"{branch}:tasks/failed/task-fail.summary.md"], git_repo.clone
    )
    assert "Fallback summary" not in failed_summary
    # And it is a real report: which steps ran and what the run changed is exactly what an operator
    # needs on a failed attempt, and it is the half the four-field stub never carried.
    assert "## Changes" in failed_summary and "## Steps" in failed_summary
    assert "_Task file: `task-fail.md`. Flow: `implementation`._" in failed_summary
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
    # done/ (never stranded as a failed/ artifact, never marked FAILED).
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

    rows = store._conn.execute(
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
    # there is no operator gate — auto-merge is a publishing-policy call owned by the task author,
    # the same trusted party as the config, not something the orchestrator second-guesses.
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
    n_checks = store._conn.execute("SELECT COUNT(*) AS n FROM check_runs").fetchone()["n"]
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
    # runs the declared test-fix loop (the skipped fixing node is a no-op each cycle). Because that
    # loop can never change the working tree, the no-effective-work stall guard aborts it after N=2
    # unchanged cycles → manual, instead of burning the full max_fix_cycles budget. fix_iterations
    # is therefore a small, honest count (the reworks charged before the stall), well below the cap.
    # EXPERIMENTAL(no-work-infra): the guard changed this expectation — if the feature is reverted,
    # restore the old assertion `fix_iterations == orch._config.agents.max_fix_cycles`.
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert 0 < store.get_counters("task-001").fix_iterations < orch._config.agents.max_fix_cycles
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


def test_supervisor_proposed_skills_reach_downstream_stages(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # skills-selection-rework: discovery is whole-repo (git ls-files); the supervisor proposes a
    # node->skills map once per task and the Core accepts it deterministically (an unknown node or
    # skill is filtered, never an error). Seed a committed repo skill so the inventory is non-empty.
    skill_dir = git_repo.clone / ".claude" / "skills" / "safe-change"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: safe-change\ndescription: review your change\n---\n\n# Body\nguidance\n",
        encoding="utf-8",
    )
    git_run(["add", ".claude/skills/safe-change/SKILL.md"], git_repo.clone)
    git_run(["commit", "-m", "add safe-change skill"], git_repo.clone)
    proposal = {
        "assignments": [
            {"node": "implementation", "skills": ["safe-change", "ghost"]},  # ghost is filtered
            {"node": "ghost-node", "skills": ["safe-change"]},  # not a flow node → filtered
        ]
    }
    providers = {
        ProviderId.CLAUDE: FakeProvider("claude", outputs={"supervisor": ("ok", proposal)}),
        ProviderId.CODEX: FakeProvider("codex"),
    }
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    # This test exercises the dynamic proposal, which is opt-in (default off).
    from dataclasses import replace

    from wastech_orchestrator.config.schema import SkillsConfig

    orch._config = replace(orch._config, skills=SkillsConfig(dynamic=True))

    result = orch.run_task(_complete_task(tmp_path, "task-skills"))
    assert result.final_status is Status.DONE

    # The effective per-node map is persisted (resume restores it without re-proposing): only the
    # known node + known skill survive; the unknown node/skill are filtered.
    skill_map = json.loads((art / "logs" / "task-skills" / "skill_map.json").read_text("utf-8"))
    assert [s["name"] for s in skill_map["implementation"]] == ["safe-change"]
    assert skill_map["implementation"][0]["path"] == ".claude/skills/safe-change/SKILL.md"
    assert "ghost-node" not in skill_map  # a proposal for a non-flow node is dropped

    # The chosen SKILL.md reaches the implementation node as an absolute read-only reference path,
    # not its body — and only that node (the proposal was node-scoped).
    impl = next(r for p in providers.values() for r in p.requests if r.node_id == "implementation")
    assert any(path.endswith("safe-change/SKILL.md") for path in impl.skill_reference_paths)
    assert "ghost" not in str(impl.skill_reference_paths)  # unknown name never surfaced
    assert "# Body" not in impl.prompt  # the skill body is never inlined into the prompt

    # The proposal is recorded as one advisory evaluation row (it proposes, never routes).
    rows = _evaluations(store, "task-skills")
    assert any(
        r["kind"] == "supervisor_skill_proposal" and r["verdict"] == "advisory" for r in rows
    )


def _write_pinned_flow(flows: Path, pins: list[str]) -> None:
    """A minimal implement→publish flow with operator ``skills:`` pins on the implement node."""
    (flows / "roles").mkdir(parents=True, exist_ok=True)
    (flows / "roles" / "implementation.md").write_text("Implement {task_path}.", "utf-8")
    flow = _MINIMAL_FLOW.replace(
        "      permission_profile: workspace-write\n",
        f"      permission_profile: workspace-write\n      skills: {json.dumps(pins)}\n",
    )
    (flows / "implementation.yaml").write_text(flow, "utf-8")


def test_operator_pinned_skill_reaches_node(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # The static layer: a skill pinned on a flow node is always included (deterministic, no LLM).
    # Here the dynamic proposal contributes nothing, so the pin alone drives the per-node selection.
    from wastech_orchestrator.core.flow.registry import FlowRegistry

    skill_dir = git_repo.clone / ".claude" / "skills" / "safe-change"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: safe-change\ndescription: d\n---\n# Body\n", "utf-8"
    )
    git_run(["add", ".claude/skills/safe-change/SKILL.md"], git_repo.clone)
    git_run(["commit", "-m", "add skill"], git_repo.clone)
    flows = tmp_path / "flows"
    _write_pinned_flow(flows, ["safe-change"])
    providers = _both()
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-pin"))
    assert result.final_status is Status.DONE
    impl = next(r for p in providers.values() for r in p.requests if r.node_id == "implementation")
    assert any(path.endswith("safe-change/SKILL.md") for path in impl.skill_reference_paths)
    skill_map = json.loads((art / "logs" / "task-pin" / "skill_map.json").read_text("utf-8"))
    assert [s["name"] for s in skill_map["implementation"]] == ["safe-change"]


def test_strict_unresolved_pin_stops_task(git_repo, make_git_config, tmp_path: Path) -> None:
    # skills.strict: an operator pin that does not resolve (here: no such skill in the repo) stops
    # the task in manual_action_required before any node runs — a fixable config/repo error.
    from wastech_orchestrator.config.schema import SkillsConfig
    from wastech_orchestrator.core.flow.registry import FlowRegistry

    flows = tmp_path / "flows"
    _write_pinned_flow(flows, ["ghost-skill"])
    providers = _both()
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)
    orch._config = replace(orch._config, skills=SkillsConfig(dynamic=False, strict=True))

    result = orch.run_task(_complete_task(tmp_path, "task-strict"))
    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert "implementation" not in _ran_nodes(store, "task-strict")  # stopped before any node


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
    records = [json.loads(p.read_text()) for p in step_files]
    node_records = [r for r in records if r["node_id"] != "supervisor"]
    supervisor_records = [r for r in records if r["node_id"] == "supervisor"]
    # complete task → refinement skipped; planning/implementation/review/documentation run agents.
    node_ids = [r["node_run_id"] for r in node_records]
    assert node_ids == sorted(node_ids)
    stages = [r["node_id"] for r in node_records]
    assert stages == ["planning", "implementation", "review", "documentation"]

    # The constant supervisor layer is itself part of the audit trail: an observation reuses the
    # observed step's node_run_id, and the once-per-task finalize turn is namespaced under the
    # reserved node_run_id=0 sentinel (it runs last but is namespaced first, since it is not a graph
    # node). Which steps are observed is the cadence's call — this run's is the packaged `events`
    # and
    # nothing deviated, so finalize is the only turn. The invariant that holds under every cadence:
    # a supervisor turn's id is the finalize sentinel or some real step's id, never anything else.
    assert supervisor_records, "supervisor turns are part of the prompt-audit trail"
    supervisor_ids = {r["node_run_id"] for r in supervisor_records}
    assert 0 in supervisor_ids
    assert supervisor_ids <= set(node_ids) | {0}

    # The combined timeline has one line per step, in the same chronological order. Real
    # graph-node entries stay chronological by node_run_id; the supervisor's synthetic ids (a
    # per-step observe reusing that step's id, and finalize's reserved 0 — written last despite
    # sorting first) do not participate in that invariant.
    lines = (audit_dir / "timeline.jsonl").read_text().splitlines()
    assert len(lines) == len(step_files)
    timeline = [json.loads(line) for line in lines]
    timeline_node_ids = [r["node_run_id"] for r in timeline if r["node_id"] != "supervisor"]
    assert timeline_node_ids == sorted(timeline_node_ids)
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
    rows = store._conn.execute(
        "SELECT kind FROM artifacts WHERE task_id = ?", ("task-001",)
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert {"prompt_audit", "prompt_audit_timeline"} <= kinds


# NOTE: there is no prompt-audit assertion for the live "primary fails → fallback runs" path, only
# because a prompt audit is not what proves it. The path itself is real and reachable here: an
# unpinned node resolves to the global primary whose fallback is the other allowed provider, so the
# packaged flow driven by two allowed providers does fall back — the mixed-class exhaustion tests
# above depend on exactly that. The fallback who-metadata (is_fallback across primary+fallback
# attempts) is unit-covered in test_flow_observability.py.


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
    rows = store._conn.execute(
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


def test_dependency_eligibility_dep_merged_out_of_band_records_pr_merge_op(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A dependency whose PR was merged out of band (no orchestrator-armed pr_merge op) still
    # gets a `pr_merge` audit op written when the daemon auto-advances the dependent on the live
    # merged-PR check — so the merge-event audit ledger is complete for watch-driven merge-gated
    # tasks without stopping the daemon for `worc prs --sync`.
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        gh=_merge_state_gh("MERGED", sha="oobsha"),
    )
    _seed_task(store, "dep", Status.DONE)
    _seed_pr(store, "dep")  # a PR exists but NO pr_merge op (merged on GitHub, not by the orch)
    assert store.get_publish_op("dep", KIND_PR_MERGE) is None

    verdict = orch.dependency_eligibility("task-001", ("dep",), pending={})
    assert verdict.state is Eligibility.ELIGIBLE
    op = store.get_publish_op("dep", KIND_PR_MERGE)
    assert op is not None and op.status == "completed" and op.result_ref == "oobsha"

    # Idempotent: a second poll does not error or double-write.
    orch.dependency_eligibility("task-001", ("dep",), pending={})
    assert store.get_publish_op("dep", KIND_PR_MERGE).result_ref == "oobsha"


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


def test_dependency_eligibility_abandoned_dep_hints_replacement(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Abandon+retry-under-a-new-id leaves the dependent WAITING forever with no clue. The
    # detail must point at the done same-title replacement so the operator can fix depends_on.
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    store.insert_task(
        TaskRow(
            task_id="dep-old",
            title="Context graph model",
            status=Status.MANUAL_ACTION_REQUIRED,
        )
    )
    ledger.append(
        LedgerRecord(
            id="dep-new",
            title="Context graph model",  # same title, retried under a new id
            final_status=Status.DONE.value,
            finished_at="2026-07-04T00:00:00Z",
        )
    )
    verdict = orch.dependency_eligibility("task-001", ("dep-old",), pending={})
    assert verdict.state is Eligibility.WAITING
    assert "dep-new" in verdict.detail and "abandoned" in verdict.detail


def test_dependency_eligibility_failed_dep_without_replacement_has_no_hint(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # No same-title done record → the message stays the plain one (the hint only fires on a match).
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    store.insert_task(TaskRow(task_id="dep", title="Solo task", status=Status.FAILED))
    verdict = orch.dependency_eligibility("task-001", ("dep",), pending={})
    assert verdict.state is Eligibility.WAITING
    assert "did you mean" not in verdict.detail


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


# -- frozen agent instruction inputs -----------------------------------------------------------


def _commit_agents_md(git_repo, git_run, body: str) -> Path:
    """Add + commit a tracked root ``AGENTS.md`` so the bundle discovers/freezes it."""
    agents = git_repo.clone / "AGENTS.md"
    agents.write_text(body, encoding="utf-8")
    git_run(["add", "AGENTS.md"], git_repo.clone)
    git_run(["commit", "-m", "add agents"], git_repo.clone)
    return agents


def test_instruction_inputs_are_frozen_end_to_end(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A complete task persists a composite instruction-manifest digest and freezes the task packet +
    # per-source repository-instruction files into the private bundle for the digest. (No
    # concatenated payload is built or injected; the agent reads the live root files itself, and a
    # workspace-write attempt write-denies them so what it reads stays immutable for the run.)
    _commit_agents_md(git_repo, git_run, "ORIGINAL REPO RULES\n")
    providers = _both()
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs=_KEEP_RUN_ARTIFACTS,
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-frz"))
    assert result.final_status is Status.DONE

    assert store.get_instruction_manifest_digest("task-frz")  # composite digest persisted
    bundle = art / RUNS_DIRNAME / "instruction-bundles" / "task-frz"
    assert (bundle / "task" / "task.md").is_file()  # frozen (private) task packet
    # The per-source file is frozen for the digest — no concatenated repository.md payload.
    src = bundle / "instructions" / "src" / "AGENTS.md"
    assert src.is_file() and "ORIGINAL REPO RULES" in src.read_text(encoding="utf-8")
    assert not (bundle / "instructions" / "repository.md").exists()

    # A workspace-write implementation attempt does NOT deny the tracked root instruction
    # files — editing them is ordinary repository work, reported to the operator not blocked.
    # (The per-source freeze above still records what the agent read, for the audit digest.)
    def _denies_agents_md(r: AgentRunRequest) -> bool:
        paths = r.write_guard.denied_write_paths if r.write_guard else ()
        return any(p.name == "AGENTS.md" for p in paths)

    impl = [r for r in providers[ProviderId.CLAUDE].requests if r.node_id == "implementation"]
    assert impl and not any(_denies_agents_md(r) for r in impl)


def test_resume_repopulates_task_packet_digest(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # on ``rerun --continue`` the frozen (key, digest) entries must be
    # repopulated from the verified manifest so ``_task_packet_digest`` is NOT None — otherwise
    # ``commit_audit`` silently skips the lifecycle-vs-packet check on resume and a task file
    # rewritten while the task was parked could be committed unchecked. The fresh path always
    # records the digest; this asserts the resume freeze path recovers the SAME one.
    from types import SimpleNamespace

    from wastech_orchestrator.core.flow.nodes import NodeInputs

    _commit_agents_md(git_repo, git_run, "REPO RULES\n")
    providers = _both()
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs=_KEEP_RUN_ARTIFACTS,
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    tid = "task-h3-resume"
    result = orch.run_task(_complete_task(tmp_path, tid))
    assert result.final_status is Status.DONE
    fresh_digest = store.get_instruction_manifest_digest(tid)
    assert fresh_digest  # fresh run persisted the composite manifest digest

    # The resume freeze path loads + verifies the persisted bundle and repopulates the entries.
    p = SimpleNamespace(task=SimpleNamespace(id=tid), instruction_entries=[])
    orch._freeze_task_and_repo_instructions(p, NodeInputs(flow_dir=str(tmp_path)), resume=True)  # type: ignore[arg-type]
    packet_digest = orch._task_packet_digest(p)  # type: ignore[arg-type]
    assert packet_digest is not None  # regression: was None on resume (check silently skipped)
    # And it is the sha256 of the frozen private task packet, so commit_audit verifies against it.
    import hashlib

    frozen_packet = art / RUNS_DIRNAME / "instruction-bundles" / tid / "task" / "task.md"
    assert packet_digest == hashlib.sha256(frozen_packet.read_bytes()).hexdigest()


def test_resume_after_live_governance_edit_does_not_fail_closed(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # Editing a governance file mid-task must never fail-close a continue/resume. The
    # resume freeze path re-hashes the FROZEN copies under the bundle dir (the task-start snapshot),
    # not the live files — so a live AGENTS.md edit after the run does not raise or block resume.
    from types import SimpleNamespace

    from wastech_orchestrator.core.flow.nodes import NodeInputs

    agents = _commit_agents_md(git_repo, git_run, "ORIGINAL REPO RULES\n")
    providers = _both()
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs=_KEEP_RUN_ARTIFACTS,
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    tid = "task-gov-resume"
    assert orch.run_task(_complete_task(tmp_path, tid)).final_status is Status.DONE

    # A governance file is now edited in the live repo (deliberately allowed).
    agents.write_text("EDITED AFTER THE RUN\n", encoding="utf-8")

    # The resume freeze path must still load + verify the persisted bundle without fail-closing:
    # if it re-hashed the LIVE file, the changed AGENTS.md would raise InstructionBundleError.
    p = SimpleNamespace(task=SimpleNamespace(id=tid), instruction_entries=[])
    orch._freeze_task_and_repo_instructions(p, NodeInputs(flow_dir=str(tmp_path)), resume=True)  # type: ignore[arg-type]
    assert orch._task_packet_digest(p) is not None  # no InstructionBundleError, no manual gate


def test_frozen_instruction_copy_is_task_start_snapshot(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # The agent reads the LIVE root files and may edit them (ordinary work, reported not
    # blocked). The private per-source freeze — the digest/audit record — is still captured once at
    # task start, so a later live edit does not change it: "record drift, don't prevent it" (the
    # frozen copy stays the task-start snapshot even though the live file changed).
    agents = _commit_agents_md(git_repo, git_run, "ORIGINAL REPO RULES\n")
    providers = _both()
    orch, _, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs=_KEEP_RUN_ARTIFACTS,
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_mutate(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "planning":
            agents.write_text("TAMPERED RULES\n", encoding="utf-8")  # rewrite the live repo file
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_mutate  # type: ignore[method-assign]

    orch.run_task(_complete_task(tmp_path, "task-imm"))

    bundles = art / RUNS_DIRNAME / "instruction-bundles"
    frozen = (bundles / "task-imm" / "instructions" / "src" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert "ORIGINAL REPO RULES" in frozen  # the task-start freeze (digest record) is unchanged
    assert "TAMPERED" not in frozen


# --- governance-change notice (never block; report on every surface) -----------------------------


def _pending_task_in_repo(git_repo, task_id: str) -> str:
    """Write a complete task into the repo's ``tasks/pending`` tree, so its committed summary is
    reachable via ``git show <branch>:tasks/done/<id>.summary.md``. Returns the task-file path."""
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    task_file = pending / f"{task_id}.md"
    task_file.write_text(
        f'---\nid: {task_id}\ntitle: "Do the thing"\n---\n\n'
        "## Description\n\nDo it.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    return str(task_file)


def test_governance_edit_reports_notice_on_all_surfaces(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A run that edits governance/instruction files is NOT blocked — it completes, the edit
    # lands, and the operator is notified on every surface: a console/log WARNING, a section in the
    # committed PR/commit summary, the completed-ledger record, and the Telegram terminal message.
    _commit_agents_md(git_repo, git_run, "ORIGINAL REPO RULES\n")
    rules = git_repo.clone / ".agents" / "rules" / "security.md"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text("original rule\n", encoding="utf-8")
    git_run(["add", ".agents/rules/security.md"], git_repo.clone)
    git_run(["commit", "-m", "add rules"], git_repo.clone)

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
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "AGENTS.md").write_text("EDITED BY TASK\n", encoding="utf-8")
            rules.write_text("edited rule\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    warnings: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warnings.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        result = orch.run_task(_pending_task_in_repo(git_repo, "task-gov"))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    assert result.final_status is Status.DONE
    expected = (".agents/rules/security.md", "AGENTS.md")  # sorted: "." < "A"
    # (1) console/log WARNING naming the changed governance files
    assert any("governance" in m and "AGENTS.md" in m for m in warnings)
    # (2) a section in the committed PR/commit summary (on the task branch)
    branch = store.get_task("task-gov").branch
    summary = git_run(["show", f"{branch}:tasks/done/task-gov.summary.md"], git_repo.clone)
    assert "## Governance files changed" in summary
    assert "`AGENTS.md`" in summary and "`.agents/rules/security.md`" in summary
    # (3) the completed-ledger record
    assert ledger.records()[-1]["governance_changed"] == list(expected)
    # (4) the Telegram terminal notification
    assert notifier.calls[-1]["governance_changed"] == expected


def test_ordinary_task_emits_no_governance_notice(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # No noise: a run that touches no governance file emits no notice on any surface.
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
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "s.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    warnings: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warnings.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        result = orch.run_task(_pending_task_in_repo(git_repo, "task-plain"))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    assert result.final_status is Status.DONE
    assert not any("governance" in m for m in warnings)
    branch = store.get_task("task-plain").branch
    summary = git_run(["show", f"{branch}:tasks/done/task-plain.summary.md"], git_repo.clone)
    assert "Governance files changed" not in summary
    assert ledger.records()[-1]["governance_changed"] == []
    assert notifier.calls[-1]["governance_changed"] == ()


# --- terminal-exchange sealing (orchestrator wiring) ----------------------------------------------


def test_terminal_seals_and_removes_active_exchange(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A DONE task leaves no active exchange dir for the next task and retains a verified private
    # snapshot; the guard flags stay clean (a clean seal, not an unsafe/contaminated teardown).
    # Retention off, which is what an operator sets to analyze finished runs — the default evicts
    # the seal at the same terminal (see the next test).
    providers = _both()
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs=_KEEP_RUN_ARTIFACTS,
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-seal"))

    assert result.final_status is Status.DONE
    assert not exchange_task_dir(orch._exchange_root, "task-seal").exists()  # no active exchange
    assert store.get_exchange_guard("task-seal") == (False, False)  # clean seal, launches unblocked
    # The curated exchange is preserved privately as a verified snapshot (the pipeline published to
    # the exchange, so at least one seal-<NNNNNN> exists with its manifest).
    seals = exchange_seal_root(art, "task-seal")
    latest = sorted(seals.glob("seal-*"))
    assert latest and (latest[-1] / "manifest.json").is_file()


def test_successful_terminal_evicts_run_artifacts_by_default(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The shipped default, end to end: a task that finishes cleanly leaves nothing behind under
    # runs/, so the operator never accumulates one directory per task per root. The committed audit
    # trail (ledger record, task file + summary) is what survives — not these caches.
    providers = _both()
    orch, store, ledger, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-evict"))

    assert result.final_status is Status.DONE
    runs = art / RUNS_DIRNAME
    for root in ("control-bundles", "instruction-bundles", "exchange-seals"):
        assert not (runs / root / "task-evict").exists()
    assert [rec["id"] for rec in ledger.records()] == ["task-evict"]  # the audit trail is intact
    assert store.get_task("task-evict") is not None


def test_seal_terminal_exchange_quarantines_on_mutation(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The terminal seam quarantines a tamper-flagged tree as evidence instead of sealing it.
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    store.insert_task(TaskRow(task_id="task-contam", title="t", status=Status.RUNNING))
    task_dir = exchange_task_dir(orch._exchange_root, "task-contam")
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text("the plan\n", encoding="utf-8")
    before = build_exchange_manifest(task_dir, "task-contam")
    (task_dir / "plan.md").write_text("MUTATED BY AGENT\n", encoding="utf-8")  # agent edit
    after = build_exchange_manifest(task_dir, "task-contam")
    mutation = ExchangeMutationManual("mutated", before=before, after=after)
    store.update_task("task-contam", exchange_contaminated=1)

    orch._seal_terminal_exchange(
        "task-contam", final=Status.MANUAL_ACTION_REQUIRED, mutation=mutation
    )

    assert not task_dir.exists()  # removed from the active root
    qroot = exchange_quarantine_root(art, "task-contam")
    assert qroot.is_dir() and any(qroot.iterdir())  # relocated as contaminated evidence
    assert not exchange_seal_root(art, "task-contam").exists()  # never sealed / restore-eligible


def test_seal_terminal_exchange_survives_bare_oserror(
    git_repo, make_git_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `seal_exchange` can raise a bare OSError (ENOSPC on a full disk / EACCES) AFTER the terminal
    # status + ledger are committed. `_seal_terminal_exchange` promises "Never raises" — it must
    # flag the tree unsafe (blocking later launches) instead of crashing into the daemon-crash
    # path with no signal.
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )
    store.insert_task(TaskRow(task_id="task-enospc", title="t", status=Status.RUNNING))
    task_dir = exchange_task_dir(orch._exchange_root, "task-enospc")
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text("the plan\n", encoding="utf-8")

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("wastech_orchestrator.core.orchestrator.seal_exchange", _boom)
    orch._seal_terminal_exchange("task-enospc", final=Status.DONE)  # must not raise

    assert store.get_exchange_guard("task-enospc")[1] is True  # exchange_active_unsafe set


def test_containment_unverified_marks_exchange_unsafe_and_skips_seal(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Unproven quiescence must not seal (an unknown descendant may still write); the task is
    # flagged unsafe so every later provider launch is blocked until an operator resolves it.
    providers = _both(
        infra_fail={"implementation"}, infra_error_class=ErrorClass.CONTAINMENT_UNVERIFIED
    )
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-unsafe"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert store.get_exchange_guard("task-unsafe")[1] is True  # exchange_active_unsafe set
    assert not exchange_seal_root(art, "task-unsafe").exists()  # no snapshot built over the tree
    # The terminal Git/cleanup is withheld while the tree is not proven quiescent — the
    # cleanup checkout did not run (no Git action against a possibly-live working tree).
    task = store.get_task("task-unsafe")
    assert task is not None and task.cleanup_completed is False
    assert task.cleanup_last_error is not None and "not proven quiescent" in task.cleanup_last_error


def test_containment_unverified_on_evaluator_marks_unsafe_and_skips_seal(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # An EVALUATOR node (review) whose provider tree cannot be proven
    # quiescent must fail closed exactly like an agent node — even though its (green) diff would
    # otherwise be shippable. Before the fix the evaluator branch degraded straight to manual
    # WITHOUT flagging the exchange unsafe, so the seal ran and the leftover tree leaked to the next
    # task. Now the error-class dispatch precedes the evaluator/agent split.
    providers = _both(infra_fail={"review"}, infra_error_class=ErrorClass.CONTAINMENT_UNVERIFIED)
    orch, store, _, art = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-eval-unsafe"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    assert "review" in _ran_nodes(store, "task-eval-unsafe")  # the evaluator did run
    assert "publish" not in _ran_nodes(store, "task-eval-unsafe")  # nothing downstream
    assert store.get_exchange_guard("task-eval-unsafe")[1] is True  # exchange_active_unsafe set
    assert not exchange_seal_root(art, "task-eval-unsafe").exists()  # not sealed over the tree
    task = store.get_task("task-eval-unsafe")
    assert task is not None and task.cleanup_completed is False  # Git/cleanup withheld


def test_stale_foreign_exchange_goes_manual_not_crash(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A stale/foreign entry left in the exchange root (e.g. a prior task whose seal was
    # interrupted) must fail closed to manual_action_required — never let a bare ExchangeError
    # escape uncaught and crash-loop the daemon. The border still holds (no provider launches over a
    # dirty exchange), so nothing downstream runs.
    providers = _both()
    orch, store, ledger, _ = _build(
        git_repo, make_git_config, tmp_path, providers=providers, check_verdicts=[0]
    )
    exchange_root = Path(git_repo.clone) / ".worc-io"
    (exchange_root / "task-OTHER").mkdir(parents=True)  # foreign leftover from a prior task

    # Must NOT raise — returns a clean terminal instead of crashing.
    result = orch.run_task(_complete_task(tmp_path, "task-h1"))

    assert result.final_status is Status.MANUAL_ACTION_REQUIRED
    task = store.get_task("task-h1")
    assert task is not None and task.status is Status.MANUAL_ACTION_REQUIRED
    assert not _ran_nodes(store, "task-h1")  # no node launched over the dirty exchange
    assert task.cleanup_last_error is not None and "task-OTHER" in task.cleanup_last_error
    assert ledger.records()[0]["final_status"] == "manual_action_required"


_GIT_EVIDENCE_FLOW = """
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
    - id: audit
      kind: agent
      role_file: roles/audit.md
      permission_profile: read-only
      git_evidence: true
    - id: publish
      kind: publish
      policy: pull_request
  edges:
    - { from: implementation, to: audit }
    - { from: audit, to: publish }
  budgets:
    global_fix_iterations: 30
"""


def test_read_only_node_that_writes_warns_operator_and_never_parks_the_task(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # A read-only node holding the git-evidence grant is held to reading by the provider's sandbox
    # (the whole clone is write-denied). If a write lands anyway the operator is told — console
    # warning + the ⚠️ trace — and the run continues to DONE. It is never parked in
    # manual_action_required: the grant exists so an audit node can read delivery history, and
    # trading that for a stray file would be the wrong bargain.
    from wastech_orchestrator.core.flow.registry import FlowRegistry
    from wastech_orchestrator.notify import TRACE_READ_ONLY_WRITE

    flows = tmp_path / "flows"
    (flows / "roles").mkdir(parents=True)
    (flows / "roles" / "implementation.md").write_text("Implement {task_path}.", "utf-8")
    (flows / "roles" / "audit.md").write_text("Audit the change.", "utf-8")
    (flows / "implementation.yaml").write_text(_GIT_EVIDENCE_FLOW, "utf-8")

    providers = _both()
    notifier = RecordingNotifier()
    orch, _store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
        config_kwargs={"telegram_trace": True, "allow_git_evidence": True},
    )
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)

    # The implementation node writes legitimately; the audit node then writes too — the case the
    # sandbox is supposed to prevent, simulated here because the fake provider has no sandbox.
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        if request.node_id == "audit":
            assert request.git_evidence is True  # the grant reached the provider
            (git_repo.clone / "stray.txt").write_text("should not exist\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        result = orch.run_task(_complete_task(tmp_path, "task-row"))
    finally:
        logger.removeHandler(handler)

    assert result.final_status is Status.DONE  # warned, not parked
    audit_traces = [c["outcome"] for c in notifier.trace_calls if c["node_id"] == "audit"]
    assert audit_traces == [TRACE_READ_ONLY_WRITE]
    assert any("changed the working tree" in m for m in messages)


def test_read_only_node_that_poisons_a_git_hook_warns_operator_and_never_parks_the_task(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The sharper half of the same rule (operator decision 2, read literally): the granted read-only
    # node plants a `.git/hooks/post-commit`, the drift event — the next git command in
    # that clone is the orchestrator's own, so a hook is how a read-only node borrows the
    # orchestrator's credentials. It still does not park the task: the operator gets a warning
    # naming the drifted aspect plus the ⚠️ trace, and the run continues. A workspace-write node
    # doing the same is still terminal (see test_workspace_write_git_control_drift_is_manual).
    from wastech_orchestrator.core.flow.registry import FlowRegistry
    from wastech_orchestrator.notify import TRACE_READ_ONLY_GIT_DRIFT

    flows = tmp_path / "flows"
    (flows / "roles").mkdir(parents=True)
    (flows / "roles" / "implementation.md").write_text("Implement {task_path}.", "utf-8")
    (flows / "roles" / "audit.md").write_text("Audit the change.", "utf-8")
    (flows / "implementation.yaml").write_text(_GIT_EVIDENCE_FLOW, "utf-8")

    providers = _both()
    notifier = RecordingNotifier()
    orch, _store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
        config_kwargs={"telegram_trace": True, "allow_git_evidence": True},
    )
    orch._flow_registry = FlowRegistry(operator_flows_dir=flows)

    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.node_id == "implementation":
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        if request.node_id == "audit":
            hook = git_repo.clone / ".git" / "hooks" / "post-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\necho poisoned\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        result = orch.run_task(_complete_task(tmp_path, "task-row"))
    finally:
        logger.removeHandler(handler)

    assert result.final_status is Status.DONE  # warned, not parked
    audit_traces = [c["outcome"] for c in notifier.trace_calls if c["node_id"] == "audit"]
    assert audit_traces == [TRACE_READ_ONLY_GIT_DRIFT]
    assert any("changed git control state" in m for m in messages)


# --- supervisor.enabled: false — the whole layer removed (P3) -------------------------------


def test_disabled_layer_makes_no_calls_and_still_writes_the_pr_body(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The switch, end to end on the PACKAGED implementation flow — which declares `observe.mode:
    # events`. That declaration is the trap the switch had to be designed around: expressing "off"
    # as a global `mode: none` fails validation AFTER the task is claimed, so the task lands in a
    # terminal `failed` to re-queue by hand (and `watch` would grind the whole queue). With
    # `enabled: false` the narrowing check is skipped, the flow runs, and nothing calls the layer.
    finding_text = "docstring drift in the new helper"
    providers = _both(
        outputs={"review": ("advisory", {"findings": [{"severity": "low", "what": finding_text}]})}
    )
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"supervisor_enabled": False},
    )
    _patch_impl_edit(providers, git_repo)

    result = orch.run_task(_complete_task(tmp_path, "task-nosup"))
    assert result.final_status is Status.DONE
    assert orch._supervisor is None  # the object is never built — that is the whole mechanism

    # Not one provider call, not one row, not one artifact belonging to the layer.
    assert _supervisor_attempts(store, "task-nosup") == []
    kinds = {row["kind"] for row in _evaluations(store, "task-nosup")}
    assert not kinds & {"supervisor_step", "supervisor_final", "supervisor_skill_proposal"}
    task_dir = task_artifact_dir(art, "task-nosup")
    assert not (task_dir / "packet.json").exists()

    # The PR body is still there, and it is the deterministic report — with no degradation callout,
    # because nothing degraded: this is the artifact the mode is supposed to produce.
    summary = (task_dir / "summary.md").read_text("utf-8")
    assert "## Changes" in summary and "## Steps" in summary
    assert "Fallback summary" not in summary
    # The third and last mode for the p0-2 criterion: a finding the gate let past reaches the PR
    # body with the layer ON (test_accepted_evaluator_findings_reach_the_pr_body), on a DEGRADED
    # finalize (test_degraded_summary_is_loud_on_done_path), and here with the layer gone entirely.
    assert "## Technical debt / follow-ups" in summary
    assert finding_text in summary
    # No spend block either, which is how an operator tells this apart from a degraded run.
    metadata = json.loads((task_dir / "summary.json").read_text("utf-8"))
    assert "supervisor_usage" not in metadata and "degraded" not in metadata
    assert [fu["title"] for fu in metadata["follow_ups"]] == [finding_text]


def test_absent_enabled_key_matches_an_explicit_true(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The default-parity regression: the new key must change nothing when absent. Compared on what
    # the layer actually did — its provider calls and the evaluation kinds it recorded — not merely
    # on "the object was built".
    def run(task_id: str, **config_kwargs: object):
        providers = _both()
        orch, store, _, _ = _build(
            git_repo,
            make_git_config,
            tmp_path / task_id,
            providers=providers,
            check_verdicts=[0],
            config_kwargs=config_kwargs,
        )
        _patch_impl_edit(providers, git_repo)
        assert (
            orch.run_task(_complete_task(tmp_path / task_id, task_id)).final_status is Status.DONE
        )
        return len(_supervisor_attempts(store, task_id)), sorted(
            {row["kind"] for row in _evaluations(store, task_id)}
        )

    assert run("task-default") == run("task-explicit", supervisor_enabled=True)


def test_disabled_layer_leaves_only_the_operators_skill_pins(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # `skills.dynamic: true` with no layer degrades to "only what the flow pins", which is correct
    # (the dynamic layer is fail-open by design) but silent — hence the config warning. Here: the
    # inventory is non-empty and dynamic is on, yet nothing is proposed, so the map stays empty.
    skill_dir = git_repo.clone / ".claude" / "skills" / "safe-change"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: safe-change\ndescription: review your change\n---\n\n# Body\nguidance\n",
        encoding="utf-8",
    )
    git_run(["add", ".claude/skills/safe-change/SKILL.md"], git_repo.clone)
    git_run(["commit", "-m", "add safe-change skill"], git_repo.clone)

    providers = _both()
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"supervisor_enabled": False, "skills_dynamic": True},
    )
    _patch_impl_edit(providers, git_repo)

    assert orch.run_task(_complete_task(tmp_path, "task-nosup-skills")).final_status is Status.DONE
    skill_map = json.loads(
        (task_artifact_dir(art, "task-nosup-skills") / "skill_map.json").read_text("utf-8")
    )
    # The packaged flow pins nothing, so with no proposal the effective set is empty for every node.
    assert all(not refs for refs in skill_map.values())
    assert not any(
        row["kind"] == "supervisor_skill_proposal"
        for row in _evaluations(store, "task-nosup-skills")
    )
    impl = next(r for p in providers.values() for r in p.requests if r.node_id == "implementation")
    assert impl.skill_reference_paths == ()


def test_disabled_layer_still_writes_the_deterministic_handoff_floor(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The handoff phase disappears with the layer, but the FLOOR it decorated does not: the
    # predecessor brief (spec pointer + acceptance criteria + changed files) is assembled from
    # artifacts, so a decomposed run keeps its subtask context with zero LLM calls.
    subtasks = {
        "decompose": True,
        "subtasks": [
            {
                "order": 1,
                "title": "First",
                "slug": "first",
                "acceptance_criteria": ["crit-one"],
                "depends_on": [],
            },
            {
                "order": 2,
                "title": "Second",
                "slug": "second",
                "acceptance_criteria": ["crit-two"],
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
    orch, store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"decomposition": True, "supervisor_enabled": False},
    )
    assert orch.run_task(_complete_task(tmp_path, "task-nosup-hnd")).final_status is Status.DONE
    assert _supervisor_attempts(store, "task-nosup-hnd") == []

    brief = task_artifact_dir(art, "task-nosup-hnd") / "subtasks" / "02-second.handoff.md"
    assert brief.exists() and brief.read_text("utf-8").strip()
    body = brief.read_text("utf-8")
    assert "01-first.md" in body and "crit-one" in body and "Changed files" in body
