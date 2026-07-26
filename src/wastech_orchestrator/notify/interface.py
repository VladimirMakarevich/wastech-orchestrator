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

#: Synthetic ``send_trace`` outcome label for a non-blocking evaluator that accepted only because
#: its whole ``max_rework_per_stage`` budget was spent (findings still open). Distinct from a clean
#: ``accept`` so the live trace reads as a soft warning (⚠️) rather than a green pass — the
#: operator layer produces this label, the transport maps it to an emoji. Kept here (notify
#: vocabulary) so producer and transport share one source of truth.
TRACE_REWORK_EXHAUSTED = "accept (rework budget exhausted)"

#: Synthetic ``send_trace`` outcome label for a read-only node holding the git-evidence grant that
#: changed the working tree — something its sandbox is supposed to make impossible. The node still
#: finished (``done``) and the run continues; the ⚠️ says the read-only guarantee did not hold and
#: the tree needs a look. Same producer/transport split as :data:`TRACE_REWORK_EXHAUSTED`.
TRACE_READ_ONLY_WRITE = "done (read-only node wrote to the workspace)"

#: Maps an internal terminal reason / loop ``limit_name`` (:mod:`core.flow.engine`) to one human
#: sentence for the operator-facing terminal notification (VF-22). These tokens are code-path
#: identifiers, never written to be read; :func:`terminal_reason_prose` turns the known ones into
#: prose and passes an unknown token through verbatim (never dropped). Kept here in the notify
#: vocabulary — beside :data:`TRACE_REWORK_EXHAUSTED` — so producer and transport share one source.
_TERMINAL_REASON_PROSE: dict[str, str] = {
    "no_file_change": (
        "the fix loop produced no file changes for consecutive rounds — the agent kept emitting "
        "output without editing the tree, so the loop was cut short of max_fix_cycles; check the "
        "latest fixing output and the review findings for what blocked progress"
    ),
    "max_fix_cycles": (
        "the review/fix loop hit its per-loop cap (max_fix_cycles) without the reviewer accepting"
    ),
    "max_total_fix_iterations": (
        "the run hit the global fix-iteration backstop (max_total_fix_iterations) across all loops"
    ),
}


def terminal_reason_prose(reason: str | None) -> str | None:
    """One human sentence for a terminal ``reason`` token, or the raw token when unmapped (VF-22).

    Returns ``None`` for an empty reason so the caller omits the line. A dynamic inline-budget token
    (``budget:<from>-><to>``) is described generically; any other unknown token is returned verbatim
    rather than dropped, so a reason is never silently lost.
    """
    if not reason:
        return None
    if reason in _TERMINAL_REASON_PROSE:
        return _TERMINAL_REASON_PROSE[reason]
    if reason.startswith("budget:"):
        return f"an inline routing budget was exhausted ({reason})"
    return reason


@dataclass(frozen=True)
class TerminalFinding:
    """The single most-severe blocking finding to surface on a terminal notification (VF-22)."""

    severity: str
    reason: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerminalDetails:
    """Operator-facing enrichment for a needs-attention terminal notification (VF-22).

    Every field is optional: the producer fills what the task's ``TaskRow`` + on-disk failure report
    carry, and the transport degrades to today's terse line when they are absent (a clean ``done``
    passes ``None``). Assembled once in the Core (which owns the store + report) and passed through
    the narrow transport, so no transport gains store/filesystem access.
    """

    title: str | None = None
    branch: str | None = None
    stop_node: str | None = None
    loop: str | None = None
    fix_rounds: int | None = None
    finding: TerminalFinding | None = None
    report_path: str | None = None


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
        governance_changed: tuple[str, ...] = (),
        details: TerminalDetails | None = None,
    ) -> None:
        """Best-effort terminal notification — never raises, never blocks the pipeline.

        ``governance_changed`` (VF-20) lists the repo-relative governance/instruction paths this
        task's diff changed, surfaced on the terminal message; empty on ordinary runs.

        ``details`` (VF-22) carries the operator-facing enrichment for a needs-attention terminal
        (title, stop node/loop, blocking finding, report path); ``None`` — a clean ``done``, or a
        call site without the context — renders the terse one-line message.
        """

    def send_trace(self, *, task_id: str, node_id: str, outcome: str) -> None:
        """Best-effort live progress trace for one finished flow node — never raises, never blocks.

        Carries only the node id and its outcome (never diff/prompt/agent text), preserving the
        no-secrets invariant by construction.
        """

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
        governance_changed: tuple[str, ...] = (),
        details: TerminalDetails | None = None,
    ) -> None:
        return None

    def send_trace(self, *, task_id: str, node_id: str, outcome: str) -> None:
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
