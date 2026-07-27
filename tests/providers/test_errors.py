"""Tests for error normalization."""

from __future__ import annotations

import pytest

from wastech_orchestrator.providers.base import FALLBACK_ELIGIBLE, ErrorClass
from wastech_orchestrator.providers.claude import _CLAUDE_SIGNATURES
from wastech_orchestrator.providers.codex import _CODEX_SIGNATURES
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


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("error: unexpected argument '--cd' found", ErrorClass.INVALID_INVOCATION),
        ("unknown option '--foo'", ErrorClass.INVALID_INVOCATION),
        ("unrecognized option: --bar", ErrorClass.INVALID_INVOCATION),
        ("this codex build reports an unsupported version", ErrorClass.UNSUPPORTED_VERSION),
    ],
)
def test_codex_argparse_error_is_invalid_invocation_not_version(
    stderr: str, expected: ErrorClass
) -> None:
    # An argparse/exit-2 rejection of OUR argv must classify as INVALID_INVOCATION (surfaced
    # loudly), distinct from a genuine unsupported-version gate — never silently masked as version.
    err = classify(
        exit_code=2,
        stderr_text=stderr,
        timed_out=False,
        launch_error=None,
        signatures=_CODEX_SIGNATURES,
    )
    assert err.error_class is expected


def test_invalid_invocation_is_not_fallback_eligible() -> None:
    # A bad argv we generated must surface, not silently fail over to the other provider.
    assert ErrorClass.INVALID_INVOCATION not in FALLBACK_ELIGIBLE
    assert message_for(ErrorClass.INVALID_INVOCATION)  # has a secret-free category message


@pytest.mark.parametrize("signatures", [_CODEX_SIGNATURES, _CLAUDE_SIGNATURES])
@pytest.mark.parametrize(
    "stderr",
    [
        "API error: 400 Bad Request",
        "request failed with status 400",
        "invalid_json_schema: property 'x' is unsupported",
        "invalid schema for output",
        "unsupported parameter: response_format",
    ],
)
def test_model_400_is_model_request_invalid(signatures: object, stderr: str) -> None:
    # A provider model/schema 400 must classify apart from a generic PROCESS_CRASHED, so it
    # surfaces loudly instead of wastefully falling over to the other provider (which 400s too).
    err = classify(
        exit_code=1,
        stderr_text=stderr,
        timed_out=False,
        launch_error=None,
        signatures=signatures,  # type: ignore[arg-type]
    )
    assert err.error_class is ErrorClass.MODEL_REQUEST_INVALID


def test_model_request_invalid_is_not_fallback_eligible() -> None:
    # A 400 the provider rejected must surface, not silently burn the fallback provider.
    assert ErrorClass.MODEL_REQUEST_INVALID not in FALLBACK_ELIGIBLE
    assert message_for(ErrorClass.MODEL_REQUEST_INVALID)  # has a secret-free category message


@pytest.mark.parametrize("signatures", [_CODEX_SIGNATURES, _CLAUDE_SIGNATURES])
@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        # The new \b400\b / bad-request signature must not steal the neighbouring HTTP-status or
        # argparse cases (word boundaries keep them disjoint).
        ("unauthorized (401)", ErrorClass.AUTHENTICATION_FAILED),
        ("forbidden 403", ErrorClass.AUTHORIZATION_FAILED),
        ("429 Too Many Requests", ErrorClass.RATE_LIMITED),
        ("500 internal server error", ErrorClass.PROVIDER_UNAVAILABLE),
        ("502 bad gateway", ErrorClass.PROVIDER_UNAVAILABLE),
        ("503 service unavailable", ErrorClass.PROVIDER_UNAVAILABLE),
        ("error: unexpected argument '--cd' found", ErrorClass.INVALID_INVOCATION),
    ],
)
def test_400_signature_does_not_shadow_siblings(
    signatures: object, stderr: str, expected: ErrorClass
) -> None:
    err = classify(
        exit_code=1,
        stderr_text=stderr,
        timed_out=False,
        launch_error=None,
        signatures=signatures,  # type: ignore[arg-type]
    )
    assert err.error_class is expected


def test_every_error_class_has_a_message() -> None:
    # message_for/_error do a bare dict lookup, so a missing _MESSAGES entry KeyErrors at classify
    # time. Guard the whole taxonomy so a new ErrorClass can never ship without its message.
    for error_class in ErrorClass:
        assert message_for(error_class)


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
