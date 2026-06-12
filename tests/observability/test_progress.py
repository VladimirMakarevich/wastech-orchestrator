"""Tests for safe heartbeat reporting around blocking operations."""

from __future__ import annotations

import logging
import time
from unittest.mock import Mock

from wastech_orchestrator.observability.progress import run_with_heartbeat


def test_heartbeat_is_emitted_until_operation_finishes() -> None:
    logger = Mock(spec=logging.LoggerAdapter)

    def operation() -> int:
        deadline = time.monotonic() + 1.0
        while not logger.info.called and time.monotonic() < deadline:
            time.sleep(0.001)
        return 7

    result = run_with_heartbeat(
        operation,
        logger=logger,
        message="provider heartbeat",
        interval_seconds=0.005,
        fields={"provider": "codex"},
    )

    assert result == 7
    logger.info.assert_called()
    _, kwargs = logger.info.call_args
    assert kwargs["extra"]["provider"] == "codex"
    assert kwargs["extra"]["elapsed_seconds"] >= 0


def test_non_positive_interval_disables_heartbeat() -> None:
    logger = Mock(spec=logging.LoggerAdapter)

    result = run_with_heartbeat(
        lambda: "done",
        logger=logger,
        message="check heartbeat",
        interval_seconds=0,
    )

    assert result == "done"
    logger.info.assert_not_called()
