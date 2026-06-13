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
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult, Notifier
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
        if request.stage in self._infra_fail:
            raise ProviderError(error_class=ErrorClass.TIMEOUT, message="infra fail")
        message, structured = self._outputs.get(request.stage, ("done", None))
        if request.stage is Stage.REFINEMENT:
            structured = (
                structured
                if isinstance(structured, dict) and "content" in structured
                else {"content": message, "human_input": None}
            )
        elif request.stage is Stage.PLANNING and (
            not isinstance(structured, dict) or "content" not in structured
        ):
            planning = structured or {}
            structured = {
                "content": message,
                "human_input": None,
                "decompose": planning.get("decompose") is True,
                "subtasks": planning.get("subtasks") or [],
            }
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
            "contacts": (),
        }
    ]
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # The commit landed on the task branch.
    branches = git_run(["branch", "--list", "agent/task-001-add-a-thing"], git_repo.clone)
    assert "agent/task-001-add-a-thing" in branches


def test_per_stage_model_reasoning_reaches_provider(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    """A ``stages.<stage>`` override sets the model/reasoning for that stage only; other stages
    fall back to the task-wide values. The resolved values travel on the AgentRunRequest."""
    providers = _both()
    orch, _store, _, _art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
    )
    path = tmp_path / "task-001.md"
    path.write_text(
        '---\nid: task-001\ntitle: "Add a thing"\nrefined: true\n'
        "model: claude-sonnet-4-6\n"
        "reasoning: low\n"
        "stages:\n"
        "  planning:\n"
        "    model: claude-opus-4-8\n"
        "    reasoning: high\n"
        "---\n\n## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(str(path))
    assert result.final_status is Status.DONE

    requests = providers[ProviderId.CLAUDE].requests + providers[ProviderId.CODEX].requests
    planning = next(r for r in requests if r.stage is Stage.PLANNING)
    impl = next(r for r in requests if r.stage is Stage.IMPLEMENTATION)
    # Planning uses the per-stage override; implementation inherits the task-wide values.
    assert (planning.model, planning.reasoning) == ("claude-opus-4-8", "high")
    assert (impl.model, impl.reasoning) == ("claude-sonnet-4-6", "low")


def test_prompt_override_reaches_provider_and_is_audited(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    """A replace-mode override changes the prompt the provider receives; it is also audited.

    The rendered prompt is written per stage run and redacted before storage; provider argv is
    untouched (the prompt only ever travels on the request, never as a CLI arg).
    """
    tdir = tmp_path / "prompts"
    tdir.mkdir()
    # Include a token-shaped secret to prove the audit artifact is redacted defensively.
    (tdir / "implementation.md").write_text(
        "CUSTOM-IMPL-INSTRUCTION leaked=ghp_abcdefghij0123456789ABCDEFGHIJ\n",
        encoding="utf-8",
    )
    prompts_block = (
        "prompts:\n"
        f"  templates_dir: {str(tdir)!r}\n"
        "  mode: replace\n"
        "  overrides:\n"
        "    implementation: 'implementation.md'\n"
    )
    providers = _both()
    orch, _store, _, art = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"prompts_block": prompts_block},
    )
    task_file = _complete_task(tmp_path, "task-pc1")
    orig = providers[ProviderId.CLAUDE].run

    def run_with_edit(request: AgentRunRequest) -> AgentRunResult:
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(task_file)
    assert result.final_status is Status.DONE

    impl_request = next(
        r for r in providers[ProviderId.CLAUDE].requests if r.stage is Stage.IMPLEMENTATION
    )
    assert "CUSTOM-IMPL-INSTRUCTION" in impl_request.prompt
    # Replace mode: the packaged default text is gone.
    assert "following the plan" not in impl_request.prompt

    rendered = art / "logs" / "task-pc1" / "stages" / "implementation" / "rendered-prompt.md"
    assert rendered.exists()
    body = rendered.read_text(encoding="utf-8")
    assert "CUSTOM-IMPL-INSTRUCTION" in body
    assert "ghp_abcdefghij0123456789ABCDEFGHIJ" not in body  # redacted


def test_strict_missing_prompt_override_fails_at_construction(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    from wastech_orchestrator.config.loader import ConfigError

    prompts_block = (
        "prompts:\n"
        f"  templates_dir: {str(tmp_path / 'absent')!r}\n"
        "  strict: true\n"
        "  overrides:\n"
        "    implementation: 'implementation.md'\n"
    )
    with pytest.raises(ConfigError):
        _build(
            git_repo,
            make_git_config,
            tmp_path,
            providers=_both(),
            check_verdicts=[0],
            config_kwargs={"prompts_block": prompts_block},
        )


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
                    structured_output={
                        "content": "plan",
                        "human_input": None,
                        **subtasks,
                    },
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


# --- Phase 6: security & observability (spec §6.1/§6.5) -------------------------------------------


def test_check_launch_failure_is_infra_not_a_fix_cycle(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The original incident: a configured check whose executable cannot be launched. The launch
    # failure must terminate the task as infrastructure — never entering fixing or spending budget.
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
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "f.py").write_text("a = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]

    result = orch.run_task(_complete_task(tmp_path, "task-launch"))
    assert result.final_status is Status.FAILED
    row = store.get_task("task-launch")
    assert row is not None and row.status is Status.FAILED
    assert row.fix_iterations == 0  # a launch failure never consumed a fix iteration


def test_check_preflight_not_ready_stops_before_branch(
    git_repo, make_git_config, git_run, tmp_path: Path
) -> None:
    # A resolver that cannot produce a launchable profile stops the task before any branch (§11).
    from wastech_orchestrator.checks.model import CheckSource
    from wastech_orchestrator.checks.profile import ResolvedCheckProfile

    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0]
    )

    class _NotReady:
        def resolve(self, *, allow_agent: bool = False) -> ResolvedCheckProfile:
            return ResolvedCheckProfile(
                schema_version=1,
                ready=False,
                source=CheckSource.DETECTED,
                checks=(),
                candidates=(),
                platform="linux",
                fingerprint="x",
                created_at="t",
                last_validated_at="t",
            )

    orch._resolver = _NotReady()  # type: ignore[assignment]

    result = orch.run_task(_complete_task(tmp_path, "task-pf"))
    assert result.final_status is Status.FAILED
    row = store.get_task("task-pf")
    assert row is not None and row.status is Status.FAILED
    assert not row.branch  # no branch was ever created
    assert git_run(["branch", "--list", "agent/*"], git_repo.clone) == ""


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
                    structured_output={
                        "content": "plan",
                        "human_input": None,
                        **subtasks,
                    },
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
        stage=request.stage,
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
            if request.stage is Stage.REFINEMENT:
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
            if request.stage is Stage.IMPLEMENTATION:
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
    refinement_requests = [r for r in claude.requests if r.stage is Stage.REFINEMENT]
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
    providers = _both(outputs={Stage.REFINEMENT: ("", signal)})
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
    providers = _both(outputs={Stage.REFINEMENT: ("", signal)})
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
    providers = _both(outputs={Stage.REFINEMENT: ("", signal)})
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
            if request.stage is Stage.IMPLEMENTATION:
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
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        notifier=notifier,
    )

    result = orch.run_task(_complete_task(tmp_path, f"task-{danger}-approval"))

    assert result.final_status is Status.DONE
    assert len(notifier.ask_calls) == 1
    assert notifier.ask_calls[0]["kind"] == "approval"


def test_denied_dependency_change_gets_one_safe_reconsideration(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    class ReconsideringProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.stage is Stage.IMPLEMENTATION:
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
    implementation = [r for r in claude.requests if r.stage is Stage.IMPLEMENTATION]
    assert len(implementation) == 2
    assert implementation[1].human_input_path is not None


def test_denied_dangerous_change_that_remains_requires_manual_action(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    class PersistentDangerProvider(FakeProvider):
        def run(self, request: AgentRunRequest) -> AgentRunResult:
            if request.stage is Stage.IMPLEMENTATION:
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
            if request.stage is Stage.PLANNING:
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
            if request.stage is Stage.IMPLEMENTATION:
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
            if request.stage is Stage.PLANNING:
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
            if request.stage is Stage.IMPLEMENTATION:
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


# --- auto-merge bypass (§ git.auto_merge*) ------------------------------------------------


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
        if request.stage is Stage.IMPLEMENTATION:
            (git_repo.clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return orig(request)

    providers[ProviderId.CLAUDE].run = run_with_edit  # type: ignore[method-assign]


def _task_with_auto_merge(tmp_path: Path, value: bool, task_id: str = "task-001") -> str:
    path = tmp_path / f"{task_id}.md"
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\nrefined: true\n'
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

    def eff(task_am: bool | None, cfg_am: bool, cfg_allow: bool) -> bool:
        orch._config = replace(
            orch._config,
            git=replace(base_git, auto_merge=cfg_am, auto_merge_allow_per_task=cfg_allow),
        )
        task = NormalizedTask(id="t", title="T", description="d", auto_merge=task_am)
        return orch._auto_merge_on(task)

    # Explicit per-task False always opts out, in every config combination.
    for cfg_am in (True, False):
        for cfg_allow in (True, False):
            assert eff(False, cfg_am, cfg_allow) is False
    # Absent (None) defers to the global flag.
    assert eff(None, True, False) is True
    assert eff(None, True, True) is True
    assert eff(None, False, False) is False
    assert eff(None, False, True) is False
    # Per-task True is honored only with operator opt-in; otherwise it falls through to the global.
    assert eff(True, False, True) is True
    assert eff(True, True, True) is True
    assert eff(True, False, False) is False  # ignored → global False
    assert eff(True, True, False) is True  # ignored → global True


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


def test_per_task_true_ignored_without_operator_optin(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    providers = _both()
    calls: list[list[str]] = []
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": False, "auto_merge_allow_per_task": False},
        gh=_merge_gh(calls),
    )
    _patch_impl_edit(providers, git_repo)
    result = orch.run_task(_task_with_auto_merge(tmp_path, True))
    assert result.final_status is Status.DONE
    assert _merge_calls(calls) == []  # per-task opt-in ignored: operator never enabled it


def test_per_task_true_honored_with_operator_optin(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    providers = _both()
    calls: list[list[str]] = []
    orch, _, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=providers,
        check_verdicts=[0],
        config_kwargs={"auto_merge": False, "auto_merge_allow_per_task": True},
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
