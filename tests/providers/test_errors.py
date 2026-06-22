"""Tests for error normalization."""

from __future__ import annotations

import pytest

from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.errors import classify, make_signatures, message_for

SIGNATURES = make_signatures(
    [
        (ErrorClass.RATE_LIMITED, r"rate limit|\b429\b"),
        (ErrorClass.AUTHENTICATION_FAILED, r"not logged in|unauthorized"),
        (ErrorClass.NETWORK_UNAVAILABLE, r"could not resolve|connection refused"),
    ]
)


def test_launch_error_takes_precedence() -> None:
    err = classify(
        exit_code=0,
        stderr_text="rate limit reached",
        timed_out=True,
        launch_error="could not launch 'codex'",
        signatures=SIGNATURES,
    )
    assert err.error_class is ErrorClass.BINARY_NOT_FOUND


def test_timeout_takes_precedence_over_exit_code_and_signature() -> None:
    err = classify(
        exit_code=1,
        stderr_text="rate limit reached",
        timed_out=True,
        launch_error=None,
        signatures=SIGNATURES,
    )
    assert err.error_class is ErrorClass.TIMEOUT


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Error: rate limit exceeded", ErrorClass.RATE_LIMITED),
        ("got HTTP 429 from server", ErrorClass.RATE_LIMITED),
        ("you are not logged in", ErrorClass.AUTHENTICATION_FAILED),
        ("could not resolve host api.openai.com", ErrorClass.NETWORK_UNAVAILABLE),
    ],
)
def test_stderr_signature_classification(stderr: str, expected: ErrorClass) -> None:
    err = classify(
        exit_code=1, stderr_text=stderr, timed_out=False, launch_error=None, signatures=SIGNATURES
    )
    assert err.error_class is expected


def test_clean_exit_is_task_failure() -> None:
    err = classify(exit_code=0, stderr_text="", timed_out=False, launch_error=None)
    assert err.error_class is ErrorClass.TASK_FAILURE


def test_unrecognized_nonzero_exit_is_process_crashed() -> None:
    err = classify(
        exit_code=1, stderr_text="weird unmatched output", timed_out=False, launch_error=None
    )
    assert err.error_class is ErrorClass.PROCESS_CRASHED


def test_signal_exit_is_process_crashed() -> None:
    err = classify(exit_code=-11, stderr_text="", timed_out=False, launch_error=None)
    assert err.error_class is ErrorClass.PROCESS_CRASHED


def test_message_never_echoes_stderr_secret() -> None:
    secret = "ghp_planted_secret_in_stderr_123456"
    err = classify(
        exit_code=1,
        stderr_text=f"unauthorized; token was {secret}",
        timed_out=False,
        launch_error=None,
        signatures=SIGNATURES,
    )
    assert secret not in err.message
    assert err.message == message_for(ErrorClass.AUTHENTICATION_FAILED)
