"""Durable human round-trip for the flow runners (P1.4 step B).

:class:`HumanGate` is the shared primitive behind the embedded refinement/planning HITL and the
dangerous-diff approval: it sends one correlated prompt, persists a durable ``waiting`` interaction
artifact, waits for the answer against the persisted deadline, and records the answer — reusing the
core HITL helpers (``write_waiting_interaction``/``write_answer``/``handle_from_artifact``) and the
:class:`~wastech_orchestrator.core.flow.nodes.base.NotifierPort`. The artifact is durable so a
restarted process resumes against the original deadline (the caller resolves an already-``waiting``
interaction via :meth:`resume`).
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.nodes.base import NotifierPort
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    handle_from_artifact,
    interaction_id,
    write_answer,
    write_waiting_interaction,
)
from wastech_orchestrator.notify import AskResult


class HumanGate:
    """Send a prompt, persist it durably, and wait for the answer (one round-trip)."""

    def __init__(
        self, notifier: NotifierPort, *, timeout_s: int, contacts: tuple[str, ...] = ()
    ) -> None:
        self._notifier = notifier
        self._timeout_s = timeout_s
        self._contacts = contacts

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
        result = self._notifier.wait_for_answer(handle)
        write_answer(path, result)
        return result

    def resume(self, path: Path, persisted: dict[str, object]) -> AskResult:
        """Resume an interaction left ``waiting`` by a previous (interrupted) run."""
        handle = handle_from_artifact(persisted)
        result = self._notifier.wait_for_answer(handle)
        write_answer(path, result)
        return result
