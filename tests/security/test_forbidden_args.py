"""Tests for the shared forbidden-flag detector and the gated full-access selector detector."""

from __future__ import annotations

import pytest

from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
    find_full_access_args,
)


@pytest.mark.parametrize(
    "args",
    [
        ("--dangerously-bypass-approvals-and-sandbox",),
        ("--dangerously-skip-permissions",),
        (
            "--allow-dangerously-skip-permissions",
        ),  # WRI-002: same bypass class, no --dangerously prefix
        ("--dangerously-bypass-hook-trust",),
        ("--yolo",),
        ("--ignore-rules",),
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
        # The structured full-access selectors are NO LONGER an absolute ban — they are gated by
        # strict_isolation (find_full_access_args), so find_forbidden_args must let them through.
        ("--sandbox=danger-full-access",),
        ("--sandbox", "danger-full-access"),
        ("-s", "danger-full-access"),
        ("-s=danger-full-access",),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode=bypassPermissions",),
    ],
)
def test_safe_args_yield_no_reasons(args: tuple[str, ...]) -> None:
    assert find_forbidden_args(args) == []


@pytest.mark.parametrize(
    "args",
    [
        ("--sandbox=danger-full-access",),
        ("--sandbox", "danger-full-access"),
        ("-s", "danger-full-access"),
        ("-s=danger-full-access",),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode=bypassPermissions",),
        ("--model", "gpt-x", "--sandbox", "danger-full-access"),  # selector not first
    ],
)
def test_full_access_selectors_are_detected(args: tuple[str, ...]) -> None:
    assert find_full_access_args(args) != []


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--sandbox", "workspace-write"),
        ("--sandbox=read-only",),
        ("--permission-mode", "acceptEdits"),  # an escalation, but not the full bypass value
        ("--dangerously-skip-permissions",),  # an absolute-ban flag, not a structured selector
    ],
)
def test_non_full_access_args_yield_no_full_access_reasons(args: tuple[str, ...]) -> None:
    assert find_full_access_args(args) == []


def test_full_access_reason_names_the_forbidden_sandbox_value() -> None:
    reasons = find_full_access_args(("--sandbox", "danger-full-access"))
    assert any(FORBIDDEN_SANDBOX_VALUE in reason for reason in reasons)


def test_full_access_reason_names_bypass_permission_mode() -> None:
    reasons = find_full_access_args(("--permission-mode", "bypassPermissions"))
    assert any("bypassPermissions" in reason for reason in reasons)
