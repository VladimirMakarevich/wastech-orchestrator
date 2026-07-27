"""Structured, secret-free operator logging.

A thin layer over the stdlib :mod:`logging` that gives the pipeline a clear operator-facing trace —
keyed by ``task_id`` / ``stage`` / ``attempt`` / ``provider`` — without ever emitting a secret. The
machine-readable audit lives elsewhere (SQLite, ``events.jsonl``, ``completed.jsonl``); these logs
are for a human watching a run, so the default rendering is **logfmt** (``key=value``), which is
greppable in a terminal.

Two guarantees back "never logs secrets": the call sites log only ids / enums / counters
(never argv, prompts, env, or file content), and a :class:`RedactionFilter` scrubs every record
through :func:`~wastech_orchestrator.providers.redaction.redact_text` as a defence-in-depth net.

Usage::

    _LOG = logging.getLogger(__name__) # module level, no handler/config
    log = bind(_LOG, task_id=task_id) # bind stable context
    log.info("route resolved", extra={"stage": "planning", "source": "config"})

``configure_logging`` is called once from the CLI; library modules only ever ``getLogger`` and bind,
never configure a handler (so tests stay silent and there are no import-time side effects).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

from wastech_orchestrator.providers.redaction import redact_text

LOGGER_NAME = "wastech_orchestrator"

# Reserved record attribute that carries the structured fields (bound context + per-call extras).
_FIELDS_ATTR = "logfmt_fields"

_configured = False


def configure_logging(
    *,
    level: int = logging.INFO,
    fmt: str = "logfmt",
    stream: TextIO | None = None,
    file_path: str | Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Idempotently install terminal and optional rotating-file handlers.

    ``fmt`` is ``"logfmt"`` (default, ``key=value``) or ``"json"``. Safe to call more than once
    (``watch`` may re-enter): the second call is a no-op. Both sinks use the same redaction filter.
    """
    global _configured
    if _configured:
        return
    formatter: logging.Formatter = _JsonFormatter() if fmt == "json" else _LogfmtFormatter()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.addHandler(handler)
    if file_path is not None:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(RedactionFilter())
        logger.addHandler(file_handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured = True


def set_log_level(level: int) -> None:
    """Update the verbosity of the configured logger after :func:`configure_logging`.

    Used to apply a persisted ``logging.level`` once the config has loaded (the ``--log-level``
    flag, when given, wins and skips this). Bypasses the first-call-wins guard so it takes effect
    even though the handlers are already installed; a plain ``setLevel`` on the package logger.
    """
    logging.getLogger(LOGGER_NAME).setLevel(level)


def bind(logger: logging.Logger, **context: Any) -> logging.LoggerAdapter[logging.Logger]:
    """Return a logger adapter that stamps ``context`` (task_id/stage/attempt/…) on every record."""
    return _BoundLogger(logger, context)


class _BoundLogger(logging.LoggerAdapter[logging.Logger]):
    """Merge bound context with any per-call ``extra=`` mapping into ``record.logfmt_fields``."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        fields: dict[str, Any] = dict(self.extra or {})
        per_call = kwargs.pop("extra", None)
        if isinstance(per_call, Mapping):
            fields.update(per_call)
        kwargs["extra"] = {_FIELDS_ATTR: fields}
        return msg, kwargs


class RedactionFilter(logging.Filter):
    """Scrub each record's message, args, and structured fields through :func:`redact_text`.

    A *structural* safety net: it is called without ``extra_secrets``, so it catches what
    :func:`redact_text` recognizes by shape — token-shaped credentials (GitHub/OpenAI/Slack/AWS
    keys, Bearer tokens, JWTs) and sensitive ``NAME=VALUE`` assignments. It does **not** know any
    task's specific secret literals (denied-file values, raw session ids); those are redacted at
    their source (the providers / the artifact writer), and the logging contract keeps call sites to
    ids/enums/counters in the first place. This filter is the last-line backstop for a token-shaped
    value that slips into a log message, not a guarantee against logging an arbitrary literal.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        record.args = _redact_args(record.args)
        fields = getattr(record, _FIELDS_ATTR, None)
        if isinstance(fields, dict):
            setattr(
                record,
                _FIELDS_ATTR,
                {k: redact_text(v) if isinstance(v, str) else v for k, v in fields.items()},
            )
        return True


def _redact_args(args: Any) -> Any:
    if isinstance(args, tuple):
        return tuple(redact_text(a) if isinstance(a, str) else a for a in args)
    if isinstance(args, Mapping):
        return {k: redact_text(v) if isinstance(v, str) else v for k, v in args.items()}
    return args


class _LogfmtFormatter(logging.Formatter):
    """Render a record as ``ts=… level=… <fields> msg="…"`` (logfmt)."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"ts={self.formatTime(record)}",
            f"level={record.levelname.lower()}",
        ]
        fields = getattr(record, _FIELDS_ATTR, {})
        if isinstance(fields, dict):
            parts += [f"{key}={_logfmt_value(value)}" for key, value in fields.items()]
        parts.append(f"msg={_logfmt_value(record.getMessage())}")
        return " ".join(parts)


class _JsonFormatter(logging.Formatter):
    """Render a record as a single JSON line (the machine-parseable alternative to logfmt)."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        fields = getattr(record, _FIELDS_ATTR, {})
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, sort_keys=False)


def _logfmt_value(value: Any) -> str:
    """Render a scalar for logfmt, quoting when it contains whitespace, ``=`` or a quote."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    text = str(value)
    if text == "" or any(ch in text for ch in ' "=\n\t'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", " ").replace("\t", " ")
        return f'"{escaped}"'
    return text
