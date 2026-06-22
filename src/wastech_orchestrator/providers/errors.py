"""Error normalization.

Maps a raw process failure — launch error, timeout, exit code, and provider stderr signatures —
onto a normalized :class:`~wastech_orchestrator.providers.base.ErrorClass` with a **secret-free**
message. The classification *taxonomy* is shared here; the provider-specific stderr/exit-code
signatures live in each adapter (e.g. ``providers.codex``) and are passed in.

``INVALID_OUTPUT`` is **not** produced here: the structured-output parser raises it directly, since
it is the component that holds the (unparseable) data. ``TASK_FAILURE`` is the clean-exit default —
a run that completed at the OS level but did not satisfy the task.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from wastech_orchestrator.providers.base import ErrorClass, NormalizedError

# Category-level, secret-free messages. The raw stderr is never echoed into a normalized message.
_MESSAGES: dict[ErrorClass, str] = {
    ErrorClass.BINARY_NOT_FOUND: "the provider executable could not be launched",
    ErrorClass.UNSUPPORTED_VERSION: "the provider CLI version is unsupported",
    ErrorClass.AUTHENTICATION_FAILED: "the provider reported an authentication failure",
    ErrorClass.AUTHORIZATION_FAILED: "the provider reported an authorization failure",
    ErrorClass.RATE_LIMITED: "the provider reported a rate limit",
    ErrorClass.NETWORK_UNAVAILABLE: "the provider reported a network failure",
    ErrorClass.PROVIDER_UNAVAILABLE: "the provider reported that the service is unavailable",
    ErrorClass.TIMEOUT: "the provider run exceeded its timeout",
    ErrorClass.PROCESS_CRASHED: "the provider process exited abnormally",
    ErrorClass.INVALID_OUTPUT: "the provider produced unparseable structured output",
    ErrorClass.PERMISSION_DENIED: "the provider reported a sandbox/permission denial",
    ErrorClass.CONFIGURATION_ERROR: "the provider invocation was rejected by the security policy",
    ErrorClass.TASK_FAILURE: "the provider completed without satisfying the task",
}


def message_for(error_class: ErrorClass) -> str:
    """Return the canonical secret-free message for a normalized error class."""
    return _MESSAGES[error_class]


@dataclass(frozen=True)
class StderrSignature:
    """A provider-specific stderr pattern that maps to a normalized error class."""

    error_class: ErrorClass
    pattern: re.Pattern[str]


def make_signatures(pairs: Iterable[tuple[ErrorClass, str]]) -> tuple[StderrSignature, ...]:
    """Compile ``(error_class, regex)`` pairs into case-insensitive signatures.

    The most specific signatures should come first — :func:`classify` returns the first match.
    """
    return tuple(
        StderrSignature(error_class, re.compile(regex, re.IGNORECASE))
        for error_class, regex in pairs
    )


def classify(
    *,
    exit_code: int | None,
    stderr_text: str,
    timed_out: bool,
    launch_error: str | None,
    signatures: Sequence[StderrSignature] = (),
) -> NormalizedError:
    """Normalize a raw failure into a :class:`NormalizedError`.

    Precedence: launch failure → timeout → provider stderr signature → clean exit (``TASK_FAILURE``)
    → otherwise an abnormal exit (``PROCESS_CRASHED``). The returned message never contains the raw
    stderr.
    """
    if launch_error is not None:
        return _error(ErrorClass.BINARY_NOT_FOUND)
    if timed_out:
        return _error(ErrorClass.TIMEOUT)
    for signature in signatures:
        if signature.pattern.search(stderr_text):
            return _error(signature.error_class)
    if exit_code == 0:
        return _error(ErrorClass.TASK_FAILURE)
    return _error(ErrorClass.PROCESS_CRASHED)


def _error(error_class: ErrorClass) -> NormalizedError:
    return NormalizedError(error_class=error_class, message=_MESSAGES[error_class])
