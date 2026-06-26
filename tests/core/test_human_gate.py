"""Observability tests for :class:`HumanGate`: the blocking HITL wait must not be a silent gap.

The gate logs an info entry line on entering the wait, a periodic heartbeat for the configured
interval, and a resolution line on exit — mirroring the provider/git heartbeats. Only secret-free
ids/kind/timeout are logged; the answer is still recorded in the durable artifact.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.nodes.human_gate import HumanGate
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    load_interaction,
    write_waiting_interaction,
)
from wastech_orchestrator.notify import AskHandle, AskResult

# The package logger; ``configure_logging`` (called by other tests) sets ``propagate=False`` on it,
# so caplog's root handler can miss our records. Attach caplog's handler here directly to capture
# the gate's log lines regardless of global logging state / test ordering.
_PKG_LOGGER = "wastech_orchestrator"


@pytest.fixture
def gate_caplog(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=_PKG_LOGGER)
    logger = logging.getLogger(_PKG_LOGGER)
    logger.addHandler(caplog.handler)
    try:
        yield caplog  # type: ignore[misc]
    finally:
        logger.removeHandler(caplog.handler)


class FakeNotifier:
    """Returns a programmed answer; an optional ``on_wait`` makes the wait block."""

    def __init__(
        self, result: AskResult, *, on_wait: object | None = None
    ) -> None:
        self._result = result
        self._on_wait = on_wait

    def start_ask(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: str,
        timeout_s: int,
        interaction_id: str,
        contacts: tuple[str, ...] = (),
    ) -> AskHandle:
        return AskHandle(interaction_id=interaction_id, kind=kind, expires_at=1.0, message_id=1)

    def wait_for_answer(self, handle: AskHandle) -> AskResult:
        if callable(self._on_wait):
            self._on_wait()
        return self._result


def _signal() -> HumanInputSignal:
    return HumanInputSignal(kind="approval", question="ok?", context="ctx", risk="other", paths=())


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def test_request_logs_entry_and_resolution(
    tmp_path: Path, gate_caplog: pytest.LogCaptureFixture
) -> None:
    gate = HumanGate(FakeNotifier(AskResult(answered=True, approved=True)), timeout_s=42)
    path = tmp_path / "hitl.json"

    result = gate.request(
        task_id="T-1", node_id="refinement", subtask=None, signal=_signal(), path=path
    )

    assert result.approved is True
    assert "awaiting human input" in _messages(gate_caplog)
    assert "human input heartbeat" not in " ".join(_messages(gate_caplog))  # off by default (0)
    resolved = next(r for r in gate_caplog.records if r.getMessage() == "human input resolved")
    assert resolved.logfmt_fields["status"] == "answered"
    assert resolved.logfmt_fields["kind"] == "approval"
    assert resolved.logfmt_fields["interaction_id"]
    # the answer is still recorded in the durable artifact.
    persisted = load_interaction(path)
    assert persisted is not None and persisted["status"] == "answered"


def test_request_resolution_reports_failure_status(
    tmp_path: Path, gate_caplog: pytest.LogCaptureFixture
) -> None:
    gate = HumanGate(
        FakeNotifier(AskResult(answered=False, timed_out=True, failure="timeout")), timeout_s=5
    )
    gate.request(
        task_id="T-1",
        node_id="refinement",
        subtask=None,
        signal=_signal(),
        path=tmp_path / "h.json",
    )
    resolved = next(r for r in gate_caplog.records if r.getMessage() == "human input resolved")
    assert resolved.logfmt_fields["status"] == "timeout"


def test_resume_logs_from_persisted_context(
    tmp_path: Path, gate_caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "h.json"
    handle = AskHandle(interaction_id="hAbc", kind="approval", expires_at=1.0, message_id=1)
    write_waiting_interaction(
        path, task_id="T-9", node_id="approval_gate", subtask=2, signal=_signal(), handle=handle
    )
    persisted = load_interaction(path)
    assert persisted is not None
    gate = HumanGate(FakeNotifier(AskResult(answered=True, approved=False)), timeout_s=5)

    result = gate.resume(path, dict(persisted))

    assert result.approved is False
    assert "awaiting human input" in _messages(gate_caplog)
    entry = next(r for r in gate_caplog.records if r.getMessage() == "awaiting human input")
    assert entry.logfmt_fields["task_id"] == "T-9"
    assert entry.logfmt_fields["node_id"] == "approval_gate"
    assert entry.logfmt_fields["subtask"] == 2
    assert entry.logfmt_fields["resumed"] is True


def test_heartbeat_emitted_while_blocked(
    tmp_path: Path, gate_caplog: pytest.LogCaptureFixture
) -> None:
    notifier = FakeNotifier(
        AskResult(answered=True, approved=True), on_wait=lambda: time.sleep(0.08)
    )
    gate = HumanGate(notifier, timeout_s=5, heartbeat_seconds=0.01)

    gate.request(
        task_id="T-1",
        node_id="refinement",
        subtask=None,
        signal=_signal(),
        path=tmp_path / "h.json",
    )

    heartbeats = [
        r for r in gate_caplog.records if r.getMessage() == "awaiting human input heartbeat"
    ]
    assert heartbeats, "expected at least one heartbeat while the wait blocked"
    assert "elapsed_seconds" in heartbeats[0].logfmt_fields
