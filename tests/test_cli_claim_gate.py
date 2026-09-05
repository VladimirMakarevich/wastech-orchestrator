"""The auto-mode claim gate: its own timeout, and a decline it remembers between ticks.

The gate asks the operator before claiming each pending task. Two properties live here. It must not
borrow ``telegram.ask_timeout_s`` — that is the HITL ceiling, eight hours on the shipped-style
value, and a gate holding an idle slot open that long is a different question from a node asking a
human to decide something. And a decline must be remembered: ``break`` ends only the current cycle,
so without a memory the operator who says no once is asked again on every tick, forever.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.orchestrator import PipelineResult
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult
from wastech_orchestrator.state_store import Status


@pytest.fixture
def in_repo_config(
    tmp_path: Path, make_git_config: Callable[..., OrchestratorConfig]
) -> OrchestratorConfig:
    """An in-repo config whose artifact root is an isolated clone dir, pull requests off."""
    clone = tmp_path / "clone"
    clone.mkdir()
    return make_git_config(clone, create_pr=False)


_TASK_BODY = (
    '---\nid: {tid}\ntitle: "T {tid}"\n---\n\n'
    "## Description\n\nDo it.\n\n## Acceptance criteria\n\n- works\n"
)


def _gate_config(base: OrchestratorConfig, *, confirm_timeout_s: int) -> OrchestratorConfig:
    """``base`` with the gate armed: auto mode on, its own gate timeout, an 8h HITL ceiling."""
    return replace(
        base,
        orchestrator=replace(
            base.orchestrator,
            auto_mode=replace(
                base.orchestrator.auto_mode,
                enabled=True,
                confirm_next_task=True,
                confirm_timeout_s=confirm_timeout_s,
            ),
        ),
        telegram=replace(base.telegram, enabled=True, ask_timeout_s=28800),
    )


class _RecordingNotifier:
    """Answers every ask the same way and records what it was asked with."""

    def __init__(self, *, approved: bool | None = False) -> None:
        self.asks: list[dict[str, object]] = []
        self._approved = approved

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
        self.asks.append({"task_id": task_id, "timeout_s": timeout_s, "context": context})
        return AskResult(answered=True, approved=self._approved, interaction_id=interaction_id)

    def send_notification(self, **_: object) -> None:
        return None

    def send_trace(self, **_: object) -> None:
        return None

    def start_ask(self, **_: object) -> AskHandle:  # pragma: no cover - unused by the gate
        raise NotImplementedError

    def wait_for_answer(self, _handle: AskHandle) -> AskResult:  # pragma: no cover - unused
        raise NotImplementedError


class _GateOrch:
    """Minimal orchestrator surface ``watch_once`` touches, with a free slot and no history."""

    def __init__(self, notifier: _RecordingNotifier) -> None:
        self.notifier = notifier
        self.ran: list[str] = []

    def resume(self) -> None:
        return None

    def refresh_repo(self) -> None:
        return None

    def acquire_slot(self, _task_id: str) -> bool:
        return True

    def lookup_task(self, _task_id: str) -> None:
        return None

    def run_task(self, task_file: str) -> PipelineResult:
        self.ran.append(task_file)
        return PipelineResult(task_id=Path(task_file).stem, final_status=Status.DONE)


def test_the_claim_gate_asks_with_its_own_timeout_not_the_hitl_ceiling(
    in_repo_config: OrchestratorConfig,
) -> None:
    config = _gate_config(in_repo_config, confirm_timeout_s=900)
    notifier = _RecordingNotifier()

    approved = cli._confirm_next_task(_GateOrch(notifier), config, "task-1", "T")  # type: ignore[arg-type]

    assert approved is False  # the notifier denies: fail-closed, the task stays pending
    assert notifier.asks[0]["timeout_s"] == 900
    assert notifier.asks[0]["timeout_s"] != config.telegram.ask_timeout_s


def test_a_declined_task_is_not_asked_about_again_on_the_next_tick(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    config = _gate_config(in_repo_config, confirm_timeout_s=60)
    folder = tmp_path / "pending"
    folder.mkdir()
    (folder / "task-301.md").write_text(_TASK_BODY.format(tid="task-301"), encoding="utf-8")
    notifier = _RecordingNotifier()  # denies
    orch = _GateOrch(notifier)
    notes = cli.WatchNotes()

    for _tick in range(3):
        cli.watch_once(orch, config, folder, notes=notes)  # type: ignore[arg-type]

    assert len(notifier.asks) == 1, notifier.asks  # asked once across three ticks
    assert orch.ran == []  # and never claimed — the decline is still fail-closed


def test_the_decline_is_forgotten_once_its_cool_off_expires(
    in_repo_config: OrchestratorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A decline means "not now", not "never": it must not silence the task for the daemon's life.
    config = _gate_config(in_repo_config, confirm_timeout_s=60)
    folder = tmp_path / "pending"
    folder.mkdir()
    (folder / "task-302.md").write_text(_TASK_BODY.format(tid="task-302"), encoding="utf-8")
    notifier = _RecordingNotifier()
    orch = _GateOrch(notifier)
    notes = cli.WatchNotes()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["now"])

    cli.watch_once(orch, config, folder, notes=notes)  # type: ignore[arg-type]
    clock["now"] += cli._DECLINE_COOLOFF_S + 1
    cli.watch_once(orch, config, folder, notes=notes)  # type: ignore[arg-type]

    assert len(notifier.asks) == 2


def test_without_a_memory_the_gate_behaves_exactly_as_before(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    # `run`/single-pass callers pass none, and one tick has nothing to remember anyway.
    config = _gate_config(in_repo_config, confirm_timeout_s=60)
    folder = tmp_path / "pending"
    folder.mkdir()
    (folder / "task-303.md").write_text(_TASK_BODY.format(tid="task-303"), encoding="utf-8")
    notifier = _RecordingNotifier()
    orch = _GateOrch(notifier)

    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    cli.watch_once(orch, config, folder)  # type: ignore[arg-type]

    assert len(notifier.asks) == 2


def test_an_approved_task_is_claimed_and_nothing_is_remembered(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    config = _gate_config(in_repo_config, confirm_timeout_s=60)
    folder = tmp_path / "pending"
    folder.mkdir()
    (folder / "task-304.md").write_text(_TASK_BODY.format(tid="task-304"), encoding="utf-8")
    notifier = _RecordingNotifier(approved=True)
    orch = _GateOrch(notifier)
    notes = cli.WatchNotes()

    cli.watch_once(orch, config, folder, notes=notes)  # type: ignore[arg-type]

    assert len(orch.ran) == 1
    assert notes.declined == {}  # only a refusal is remembered


def test_a_withheld_task_is_not_summarised_as_an_empty_queue(
    in_repo_config: OrchestratorConfig, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The two lines were adjacent and disagreed: the gate said it was not claiming a named pending
    # task, then the summary said "no pending tasks". A gate-declined task produces no
    # `PipelineResult`, and an empty result list was read as an empty queue.
    config = _gate_config(in_repo_config, confirm_timeout_s=60)
    folder = tmp_path / "pending"
    folder.mkdir()
    (folder / "task-305.md").write_text(_TASK_BODY.format(tid="task-305"), encoding="utf-8")
    orch = _GateOrch(_RecordingNotifier())
    notes = cli.WatchNotes()

    results = cli.watch_once(orch, config, folder, notes=notes)  # type: ignore[arg-type]
    code = cli._summarize_watch(results, withheld=notes.withheld)

    out = capsys.readouterr().out
    assert code == 0  # fail-closed, not an error
    assert notes.withheld == ["task-305"]
    assert "no pending tasks" not in out
    assert "task-305" in out


def test_an_empty_queue_still_reads_as_an_empty_queue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._summarize_watch([]) == 0
    assert "no pending tasks" in capsys.readouterr().out
