"""Tests for the shared detector of options that bypass the sandbox or the approval policy."""

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
        ("--allow-dangerously-skip-permissions",),  # same bypass class, no --dangerously prefix
        ("--dangerously-bypass-hook-trust",),
        ("--yolo",),
        ("--ignore-rules",),
        ("--model", "gpt-x", "--yolo"),  # offending flag not first
        ("--sandbox",),  # dangling: no value (last token)
        ("-s",),  # dangling short form
        ("--sandbox=",),  # trailing '=' with empty value
        # The structured full-access selectors: no configuration may select either, so every
        # spelling of both — long and short flag, split and inline value — is an absolute reason.
        ("--sandbox=danger-full-access",),
        ("--sandbox", "danger-full-access"),
        ("-s", "danger-full-access"),
        ("-s=danger-full-access",),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode=bypassPermissions",),
        ("--model", "gpt-x", "--sandbox", "danger-full-access"),  # selector not first
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
        # The permission-mode flag itself is legitimate (the adapter emits it) — only the bypass
        # value is full access. A mere escalation is caught elsewhere, against the requested
        # profile, because whether it escalates cannot be known from the token alone.
        ("--permission-mode", "acceptEdits"),
    ],
)
def test_safe_args_yield_no_reasons(args: tuple[str, ...]) -> None:
    assert find_forbidden_args(args) == []


def test_reason_names_the_forbidden_sandbox_value() -> None:
    reasons = find_forbidden_args(("--sandbox", "danger-full-access"))
    assert any(FORBIDDEN_SANDBOX_VALUE in reason for reason in reasons)


def test_reason_names_bypass_permission_mode() -> None:
    reasons = find_forbidden_args(("--permission-mode", "bypassPermissions"))
    assert any("bypassPermissions" in reason for reason in reasons)
