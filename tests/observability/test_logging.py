"""Structured, secret-free operator logging — observability/logging.py (spec §6.6)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from wastech_orchestrator.observability import logging as obslog

_GH_TOKEN = "ghp_" + "A" * 20  # token-shaped value the redaction net recognizes


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    """Isolate each test from the process-wide package logger / configure-once flag."""
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    for handler in pkg.handlers:
        handler.close()
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("n", logging.INFO, "p", 1, msg, None, None)


def test_redaction_filter_scrubs_message() -> None:
    record = _record(f"leaked {_GH_TOKEN}")
    obslog.RedactionFilter().filter(record)
    assert "ghp_" not in record.getMessage()


def test_redaction_filter_scrubs_fields_but_keeps_safe_ones() -> None:
    record = _record("msg")
    record.logfmt_fields = {"token": _GH_TOKEN, "stage": "review"}  # type: ignore[attr-defined]
    obslog.RedactionFilter().filter(record)
    assert "ghp_" not in record.logfmt_fields["token"]  # type: ignore[attr-defined]
    assert record.logfmt_fields["stage"] == "review"  # type: ignore[attr-defined]


def test_logfmt_renders_context_and_quotes_spaces() -> None:
    stream = io.StringIO()
    obslog.configure_logging(fmt="logfmt", stream=stream)
    log = obslog.bind(logging.getLogger(obslog.LOGGER_NAME), task_id="task-9")
    log.info("route resolved", extra={"stage": "planning", "source": "config"})
    out = stream.getvalue()
    assert "level=info" in out
    assert "task_id=task-9" in out
    assert "stage=planning" in out
    assert "source=config" in out
    assert 'msg="route resolved"' in out  # spaces force quoting


def test_json_format_round_trips() -> None:
    stream = io.StringIO()
    obslog.configure_logging(fmt="json", stream=stream)
    log = obslog.bind(logging.getLogger(obslog.LOGGER_NAME), task_id="t")
    log.info("hello", extra={"n": 3})
    record = json.loads(stream.getvalue().strip())
    assert record["msg"] == "hello"
    assert record["task_id"] == "t"
    assert record["n"] == 3


def test_configure_logging_is_idempotent() -> None:
    obslog.configure_logging()
    obslog.configure_logging()
    assert len(logging.getLogger(obslog.LOGGER_NAME).handlers) == 1


def test_seeded_secret_never_reaches_the_sink() -> None:
    stream = io.StringIO()
    obslog.configure_logging(stream=stream)
    log = obslog.bind(logging.getLogger(obslog.LOGGER_NAME), task_id="t")
    log.info("agent printed %s", _GH_TOKEN, extra={"echoed": _GH_TOKEN})
    assert _GH_TOKEN not in stream.getvalue()


def test_json_file_handler_writes_redacted_records(tmp_path) -> None:
    stream = io.StringIO()
    log_path = tmp_path / "operator" / "orchestrator.jsonl"
    obslog.configure_logging(fmt="json", stream=stream, file_path=log_path)
    log = obslog.bind(logging.getLogger(obslog.LOGGER_NAME), task_id="task-9")

    log.info("provider heartbeat", extra={"token": _GH_TOKEN, "elapsed_seconds": 30.0})
    for handler in logging.getLogger(obslog.LOGGER_NAME).handlers:
        handler.flush()

    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["msg"] == "provider heartbeat"
    assert record["task_id"] == "task-9"
    assert record["elapsed_seconds"] == 30.0
    assert _GH_TOKEN not in record["token"]
    assert _GH_TOKEN not in stream.getvalue()
