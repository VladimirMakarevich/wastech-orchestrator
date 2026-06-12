"""Heartbeat helper for long-running blocking operations.

The orchestrator deliberately uses synchronous external calls. This helper keeps that simple
control flow while emitting a safe operator heartbeat from a daemon thread when an operation runs
longer than the configured interval. Callers provide only safe structured fields; prompts, argv,
environment values, and child-process output must never be included.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping


def run_with_heartbeat[T](
    operation: Callable[[], T],
    *,
    logger: logging.LoggerAdapter[logging.Logger],
    message: str,
    interval_seconds: float,
    fields: Mapping[str, object] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Run ``operation`` and emit periodic heartbeats until it returns or raises.

    A non-positive interval disables the heartbeat. The operation always runs in the calling
    thread; only the timer loop is threaded, so exception and return-value behavior is unchanged.
    """
    if interval_seconds <= 0:
        return operation()

    started = monotonic()
    stopped = threading.Event()
    safe_fields = dict(fields or {})

    def emit() -> None:
        while not stopped.wait(interval_seconds):
            logger.info(
                message,
                extra={
                    **safe_fields,
                    "elapsed_seconds": round(monotonic() - started, 1),
                },
            )

    thread = threading.Thread(target=emit, name="wastech-heartbeat", daemon=True)
    thread.start()
    try:
        return operation()
    finally:
        stopped.set()
        thread.join(timeout=min(interval_seconds, 1.0))
