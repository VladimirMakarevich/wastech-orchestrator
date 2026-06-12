"""Notifier protocol + null implementation (spec §4.7).

The Core is typed against this protocol rather than ``python-telegram-bot``, so the transport stays
an implementation detail and tests can inject a fake. The protocol is intentionally narrow: one
fire-and-forget terminal notification, plus one blocking ``ask_human`` primitive for clarifying
questions or yes/no approvals on a dangerous action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

AskKind = Literal["question", "approval"]


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
    ) -> None:
        """Best-effort terminal notification — never raises, never blocks the pipeline."""

    def ask_human(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
    ) -> AskResult:
        """Blocking HITL primitive bounded by ``timeout_s``; timeout is deterministic (§4.7)."""


class NullNotifier:
    """Silent no-op notifier — the default when the feature is disabled or secrets are absent.

    Every send is dropped; every ``ask_human`` returns a deterministic timed-out result so callers
    can rely on a clean fallback without branching on the notifier type.
    """

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
    ) -> None:
        return None

    def ask_human(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
    ) -> AskResult:
        return AskResult(answered=False, timed_out=True)
