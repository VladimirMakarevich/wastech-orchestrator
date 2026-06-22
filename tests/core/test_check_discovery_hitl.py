"""The sensitive-change approval gate for a changed set of check commands (post-test-run)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from tests.core.test_orchestrator import RecordingNotifier, _both, _build

from wastech_orchestrator.checks.model import CheckSource, ResolvedCheck
from wastech_orchestrator.checks.profile import ResolvedCheckProfile, commands_signature
from wastech_orchestrator.core.decomposition import DecompositionDecision
from wastech_orchestrator.core.loop_control import LoopCounters
from wastech_orchestrator.core.orchestrator import Orchestrator, _Pipeline
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import ManualActionRequired
from wastech_orchestrator.notify.interface import AskResult
from wastech_orchestrator.state_store import TaskRow
from wastech_orchestrator.task.model import NormalizedTask


def _profile(*checks: ResolvedCheck, approved: bool = False) -> ResolvedCheckProfile:
    return ResolvedCheckProfile(
        schema_version=2,
        ready=True,
        source=CheckSource.DETECTED,
        checks=checks,
        candidates=(),
        platform="linux",
        fingerprint="fp",
        created_at="t",
        last_validated_at="t",
        commands_signature=commands_signature(checks),
        approved=approved,
    )


def _pipeline() -> _Pipeline:
    task = NormalizedTask(id="task-disc", title="T", description="d")
    return _Pipeline(
        task=task,
        task_file="x",
        status=Status.PREPARING,
        counters=LoopCounters(),
        decomposition=DecompositionDecision(accepted=False, reason="x", n=1),
    )


def _orch(
    git_repo, make_git_config, tmp_path: Path, *, ask_results: Sequence[AskResult] | None = None
) -> tuple[Orchestrator, RecordingNotifier]:
    notifier = RecordingNotifier(ask_results=list(ask_results or []))
    orch, store, _, _ = _build(
        git_repo,
        make_git_config,
        tmp_path,
        providers=_both(),
        check_verdicts=[0],
        notifier=notifier,
    )
    # The approval gate registers an HITL artifact, which FK-references the task row.
    store.insert_task(TaskRow(task_id="task-disc", title="T", status=Status.NEW))
    return orch, notifier


_TESTS = ResolvedCheck(name="tests", argv=("pytest",))
_TESTS_V = ResolvedCheck(name="tests", argv=(".venv/bin/pytest",))


def test_first_ever_set_is_auto_approved_without_prompt(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, notifier = _orch(git_repo, make_git_config, tmp_path)
    gated = orch._gate_check_commands(_pipeline(), _profile(_TESTS), prev_approved_sig="")
    assert gated.approved is True
    assert gated.approved_interaction_id == "bootstrap"
    assert notifier.ask_calls == []  # first-ever resolution is recorded, never prompted


def test_unchanged_set_is_not_reprompted(git_repo, make_git_config, tmp_path: Path) -> None:
    orch, notifier = _orch(git_repo, make_git_config, tmp_path)
    profile = _profile(_TESTS, approved=True)
    gated = orch._gate_check_commands(
        _pipeline(), profile, prev_approved_sig=profile.commands_signature
    )
    assert gated is profile  # already approved, same signature → returned unchanged
    assert notifier.ask_calls == []


def test_changed_set_prompts_and_records_approval(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    orch, notifier = _orch(
        git_repo,
        make_git_config,
        tmp_path,
        ask_results=[AskResult(answered=True, approved=True)],
    )
    gated = orch._gate_check_commands(_pipeline(), _profile(_TESTS_V), prev_approved_sig="old-sig")
    assert gated.approved is True
    assert gated.approved_interaction_id.startswith("d")  # the HITL interaction id
    assert len(notifier.ask_calls) == 1  # the changed set prompted exactly once


def test_changed_set_denied_fails_closed(git_repo, make_git_config, tmp_path: Path) -> None:
    orch, _ = _orch(
        git_repo,
        make_git_config,
        tmp_path,
        ask_results=[AskResult(answered=True, approved=False)],
    )
    with pytest.raises(ManualActionRequired, match="was not approved"):
        orch._gate_check_commands(_pipeline(), _profile(_TESTS_V), prev_approved_sig="old-sig")


def test_changed_set_without_notifier_fails_closed(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # No Telegram (NullNotifier): a *changed* command set cannot be approved → manual action.
    orch, store, _, _ = _build(
        git_repo, make_git_config, tmp_path, providers=_both(), check_verdicts=[0], notifier=None
    )
    store.insert_task(TaskRow(task_id="task-disc", title="T", status=Status.NEW))
    with pytest.raises(ManualActionRequired):
        orch._gate_check_commands(_pipeline(), _profile(_TESTS_V), prev_approved_sig="old-sig")
