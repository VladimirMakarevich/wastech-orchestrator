"""Human-in-the-loop notifications and `ask_human` primitive (spec §4.7).

The Core depends only on the narrow :class:`Notifier` protocol; the Telegram transport is selected
by :func:`build_notifier` from :class:`TelegramConfig` + the process environment. When the feature
is disabled or its env-named secrets are missing, the factory returns a silent :class:`NullNotifier`
so the pipeline behaves exactly as it did before.
"""

from __future__ import annotations

from wastech_orchestrator.notify.interface import (
    AskFailure,
    AskHandle,
    AskKind,
    AskResult,
    Notifier,
    NullNotifier,
)
from wastech_orchestrator.notify.telegram import TelegramNotifier, build_notifier

__all__ = [
    "AskFailure",
    "AskHandle",
    "AskKind",
    "AskResult",
    "NullNotifier",
    "Notifier",
    "TelegramNotifier",
    "build_notifier",
]
