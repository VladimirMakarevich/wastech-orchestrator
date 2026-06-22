"""Notifier protocol + null implementation.

The Core is typed against this protocol rather than ``python-telegram-bot``, so the transport stays
an implementation detail and tests can inject a fake. The protocol is intentionally narrow: one
fire-and-forget terminal notification plus a durable two-phase primitive for clarifying questions
or yes/no approvals on a dangerous action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

AskKind = Literal["question", "approval"]
AskFailure = Literal["timeout", "transport_error", "invalid_response"]


@dataclass(frozen=True)
class AskHandle:
    """Durable, secret-free handle for one human interaction.

    The Core persists this shape in a HITL artifact before it starts waiting. ``message_id`` and
    ``update_offset`` are Telegram correlation metadata, not credentials. ``expires_at`` is a Unix
    timestamp so a restarted process can continue waiting against the original deadline.
    """

    interaction_id: str
    kind: AskKind
    expires_at: float
    message_id: int | None = None
    update_offset: int | None = None
    delivered: bool = True


@dataclass(frozen=True)
class AskResult:
    """Outcome of an :meth:`Notifier.ask_human` round-trip.

    ``timed_out=True`` is the deterministic no-reply path. For ``kind="approval"`` the matcher sets
    ``approved`` to ``True`` / ``False``; ``approved=None`` means the reply was free-form text or no
    reply arrived. ``text`` carries the raw reply for ``kind="question"`` (and is ``None`` on
    timeout).
    """

    answered: bool
    text: str | None = None
    approved: bool | None = None
    timed_out: bool = False
    failure: AskFailure | None = None
    interaction_id: str | None = None
    message_id: int | None = None


@runtime_checkable
class Notifier(Protocol):
    """Narrow Core-facing interface; the only contract a transport must satisfy."""

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
        contacts: tuple[str, ...] = (),
    ) -> None:
        """Best-effort terminal notification — never raises, never blocks the pipeline."""

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
        """Send one correlated prompt and return a durable handle without waiting."""

    def wait_for_answer(self, handle: AskHandle) -> AskResult:
        """Wait for the correlated answer until the handle's persisted deadline."""

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
        """Blocking compatibility facade over :meth:`start_ask` + :meth:`wait_for_answer`."""


class NullNotifier:
    """Silent terminal notifier whose blocking requests fail closed as transport errors."""

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
        contacts: tuple[str, ...] = (),
    ) -> None:
        return None

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
        return AskHandle(
            interaction_id=interaction_id,
            kind=kind,
            expires_at=0.0,
            delivered=False,
        )

    def wait_for_answer(self, handle: AskHandle) -> AskResult:
        return AskResult(
            answered=False,
            timed_out=False,
            failure="transport_error",
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
