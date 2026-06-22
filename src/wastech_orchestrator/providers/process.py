"""Safe process runner (.agents/rules/coding-style.md).

The single chokepoint for launching any external CLI. Every provider subprocess goes through
``run_process``. The runner is deliberately provider-agnostic: it knows nothing about Codex/Claude
syntax or :class:`~wastech_orchestrator.providers.base.ErrorClass`. It only launches an argv list
safely and reports a raw result for an adapter to normalize.

Invariants enforced here:

* launch via an **argv list** — never a string, never ``shell=True``, never user strings
  interpolated into the command;
* a **mandatory** timeout;
* the child receives exactly the ``env`` mapping passed in (the allowlisted env, see
  :mod:`wastech_orchestrator.security.env`) — never the parent's full environment;
* the prompt is fed on **stdin** (``stdin_text``), keeping argv free of task content; with no
  ``stdin_text`` the child's stdin is ``DEVNULL`` (parent stdin is never inherited);
* stdout is streamed to ``stdout_path``; stderr is captured (it is small and secret-prone, so the
  adapter redacts it before it ever touches an artifact).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProcessResult:
    """Raw outcome of a single subprocess launch, before any provider-specific normalization."""

    exit_code: int | None  # None when the process timed out or never launched
    timed_out: bool
    launch_error: str | None  # set (secret-free) when the binary could not be launched at all
    duration_seconds: float
    stdout_path: str
    stderr_text: str  # captured stderr, NOT yet redacted — the caller redacts before writing it


def run_process(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    stdout_path: str | Path,
    stdin_text: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProcessResult:
    """Launch ``argv`` safely and return a raw :class:`ProcessResult`.

    :param argv: the command and its arguments as a list (never a shell string).
    :param cwd: working directory for the child (the clone).
    :param env: the **entire** child environment (already allowlisted); not merged with the parent.
    :param timeout_seconds: mandatory wall-clock timeout; on expiry the child is killed and
        ``timed_out`` is set with ``exit_code=None``.
    :param stdout_path: file the child's stdout is streamed to (created/overwritten).
    :param stdin_text: text fed to the child's stdin; ``None`` means ``DEVNULL`` (no parent stdin).
    :param monotonic: monotonic clock seam for the measured duration (injected in tests).
    :returns: a :class:`ProcessResult`. A failed launch (missing/!executable binary) is reported via
        ``launch_error`` rather than raised; a timeout via ``timed_out``.
    """
    start = monotonic()
    timed_out = False
    launch_error: str | None = None
    exit_code: int | None = None
    stderr_text = ""

    # ``input`` and ``stdin`` are mutually exclusive: feed the prompt via ``input``, or send EOF
    # immediately via DEVNULL so a prompt-less child can never hang on inherited stdin.
    stdin_kwargs: dict[str, Any] = (
        {"stdin": subprocess.DEVNULL} if stdin_text is None else {"input": stdin_text}
    )

    try:
        stdout_file = open(stdout_path, "wb")  # noqa: SIM115 — closed in the inner `with`
    except OSError as exc:
        # The stdout sink itself could not be opened (unwritable dir, bad path). Degrade rather than
        # raise, and name the *path* — not argv[0], which launched fine and is not the culprit.
        launch_error = f"could not open stdout path {os.fspath(stdout_path)!r}: {_reason(exc)}"
    else:
        try:
            with stdout_file:
                completed = subprocess.run(
                    list(argv),
                    cwd=os.fspath(cwd),
                    env=dict(env),
                    stdout=stdout_file,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    shell=False,
                    **stdin_kwargs,
                )
            exit_code = completed.returncode
            stderr_text = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stderr_text = _coerce_stderr(exc.stderr)
        except OSError as exc:
            # The binary could not be launched (missing / not executable / bad cwd). argv[0] comes
            # from config (no secret); safe to name. FileNotFoundError/PermissionError/
            # NotADirectoryError are all OSError, so one clause covers them.
            command = argv[0] if argv else "<empty argv>"
            launch_error = f"could not launch {command!r}: {_reason(exc)}"

    duration_seconds = monotonic() - start
    return ProcessResult(
        exit_code=exit_code,
        timed_out=timed_out,
        launch_error=launch_error,
        duration_seconds=duration_seconds,
        stdout_path=os.fspath(stdout_path),
        stderr_text=stderr_text,
    )


def _reason(exc: OSError) -> str:
    """A short, secret-free reason from an OS error (its ``strerror``, else the exception type)."""
    return exc.strerror or type(exc).__name__


def _coerce_stderr(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw
