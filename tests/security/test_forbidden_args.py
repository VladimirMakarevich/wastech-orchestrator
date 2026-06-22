"""Tests for the shared forbidden-flag detector (spec §11, §12.7)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
)


@pytest.mark.parametrize(
    "args",
    [
        ("--dangerously-bypass-approvals-and-sandbox",),
        ("--dangerously-skip-permissions",),
        ("--dangerously-bypass-hook-trust",),
        ("--yolo",),
        ("--ignore-rules",),
        ("--sandbox=danger-full-access",),
        ("--sandbox", "danger-full-access"),
        ("-s", "danger-full-access"),
        ("-s=danger-full-access",),
        ("--model", "gpt-x", "--yolo"),  # offending flag not first
        ("--sandbox",),  # dangling: no value (last token)
        ("-s",),  # dangling short form
        ("--sandbox=",),  # trailing '=' with empty value
    ],
)
def test_forbidden_args_are_detected(args: tuple[str, ...]) -> None:
    assert find_forbidden_args(args) != []


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--model", "gpt-5"),
        ("--sandbox", "workspace-write"),
        ("--sandbox=read-only",),
        ("--json", "--output-last-message", "/tmp/last.txt"),
    ],
)
def test_safe_args_yield_no_reasons(args: tuple[str, ...]) -> None:
    assert find_forbidden_args(args) == []


def test_sandbox_reason_names_the_forbidden_value() -> None:
    reasons = find_forbidden_args(("--sandbox", "danger-full-access"))
    assert any(FORBIDDEN_SANDBOX_VALUE in reason for reason in reasons)
