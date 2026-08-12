"""Human-in-the-loop notifications and `ask_human` primitive.

The Core depends only on the narrow :class:`Notifier` protocol; the Telegram transport is selected
by :func:`build_notifier` from :class:`TelegramConfig` + the process environment. When the feature
is disabled or its env-named secrets are missing, the factory returns a silent :class:`NullNotifier`
so the pipeline behaves exactly as it did before.
"""

from __future__ import annotations

from wastech_orchestrator.notify.interface import (
    TRACE_READ_ONLY_GIT_DRIFT,
    TRACE_READ_ONLY_WRITE,
    TRACE_REWORK_EXHAUSTED,
    AskFailure,
    AskHandle,
    AskKind,
    AskResult,
    Notifier,
    NullNotifier,
    TerminalDetails,
    TerminalFinding,
    terminal_reason_prose,
)
from wastech_orchestrator.notify.telegram import TelegramNotifier, build_notifier

__all__ = [
    "TRACE_READ_ONLY_GIT_DRIFT",
    "TRACE_READ_ONLY_WRITE",
    "TRACE_REWORK_EXHAUSTED",
    "AskFailure",
    "AskHandle",
    "AskKind",
    "AskResult",
    "Notifier",
    "NullNotifier",
    "TelegramNotifier",
    "TerminalDetails",
    "TerminalFinding",
    "build_notifier",
    "terminal_reason_prose",
]
