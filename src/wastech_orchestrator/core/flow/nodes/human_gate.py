"""Durable human round-trip for the flow runners.

:class:`HumanGate` is the shared primitive behind the embedded refinement/planning HITL and the
dangerous-diff approval: it sends one correlated prompt, persists a durable ``waiting`` interaction
artifact, waits for the answer against the persisted deadline, and records the answer — reusing the
core HITL helpers (``write_waiting_interaction``/``write_answer``/``handle_from_artifact``) and the
:class:`~wastech_orchestrator.core.flow.nodes.base.NotifierPort`. The artifact is durable so a
restarted process resumes against the original deadline (the caller resolves an already-``waiting``
interaction via :meth:`resume`).

Observability: the wait is the one long blocking operation that otherwise emits nothing to the run
log (the prompt goes only to the notifier). On entering the wait the gate logs an info line and, for
the configured ``heartbeat_seconds`` interval, a periodic heartbeat — mirroring the provider/git
heartbeats — then a resolution line on exit, so a waiting run is never a silent gap. Only
secret-free ids/kind/timeout are logged; the question text and paths stay in the redacted artifact.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from wastech_orchestrator.core.flow.nodes.base import NotifierPort
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    handle_from_artifact,
    interaction_id,
    write_answer,
    write_waiting_interaction,
)
from wastech_orchestrator.notify import AskHandle, AskResult
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.observability.progress import run_with_heartbeat

_LOG = logging.getLogger(__name__)


class HumanGate:
    """Send a prompt, persist it durably, and wait for the answer (one round-trip)."""

    def __init__(
        self,
        notifier: NotifierPort,
        *,
        timeout_s: int,
        contacts: tuple[str, ...] = (),
        heartbeat_seconds: float = 0.0,
    ) -> None:
        self._notifier = notifier
        self._timeout_s = timeout_s
        self._contacts = contacts
        self._heartbeat_seconds = heartbeat_seconds

    def request(
        self,
        *,
        task_id: str,
        node_id: str,
        subtask: int | None,
        signal: HumanInputSignal,
        path: Path,
    ) -> AskResult:
        """Start a fresh prompt, persist the ``waiting`` artifact, wait, and record the answer.

        ``node_id`` is the interaction key: the flow node id of the embedded refinement/planning
        HITL, the dangerous-diff guard, or a standalone ``hitl`` gate node.
        """
        handle = self._notifier.start_ask(
            question=signal.question,
            context=signal.context,
            task_id=task_id,
            kind=signal.kind,
            timeout_s=self._timeout_s,
            interaction_id=interaction_id(task_id, node_id, subtask),
            contacts=self._contacts,
        )
        write_waiting_interaction(
            path, task_id=task_id, node_id=node_id, subtask=subtask, signal=signal, handle=handle
        )
        log = bind(_LOG, task_id=task_id, node_id=node_id)
        fields = {
            "interaction_id": handle.interaction_id,
            "kind": signal.kind,
            "subtask": subtask,
            "timeout_seconds": self._timeout_s,
        }
        result = self._wait(log, handle, fields)
        write_answer(path, result)
        return result

    def resume(self, path: Path, persisted: dict[str, object]) -> AskResult:
        """Resume an interaction left ``waiting`` by a previous (interrupted) run."""
        handle = handle_from_artifact(persisted)
        log = bind(
            _LOG,
            task_id=str(persisted.get("task_id", "")),
            node_id=str(persisted.get("node_id", "")),
        )
        fields = {
            "interaction_id": handle.interaction_id,
            "kind": handle.kind,
            "subtask": persisted.get("subtask"),
            "timeout_seconds": self._timeout_s,
            "resumed": True,
        }
        result = self._wait(log, handle, fields)
        write_answer(path, result)
        return result

    def _wait(
        self,
        log: logging.LoggerAdapter[logging.Logger],
        handle: AskHandle,
        fields: Mapping[str, object],
    ) -> AskResult:
        """Wait for the answer, bracketing the blocking call with an entry/heartbeat/exit signal."""
        log.info("awaiting human input", extra=fields)
        result = run_with_heartbeat(
            lambda: self._notifier.wait_for_answer(handle),
            logger=log,
            message="awaiting human input heartbeat",
            interval_seconds=self._heartbeat_seconds,
            fields=fields,
        )
        log.info(
            "human input resolved",
            extra={**fields, "status": result.failure or "answered"},
        )
        return result
