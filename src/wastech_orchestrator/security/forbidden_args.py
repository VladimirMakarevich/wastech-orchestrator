"""Forbidden CLI flag detector (.agents/rules/security.md).

The single source of truth for "an option that bypasses the sandbox/approvals". It is called from
two places so the security invariant is enforced in depth:

* the config validator (:mod:`wastech_orchestrator.config.validation`) — at load time;
* the provider command builders (e.g. :mod:`wastech_orchestrator.providers.codex`) — at run time,
  so the policy cannot be weakened through a task or ``extra_args`` even if a config check is ever
  bypassed.

Covers Codex ``--dangerously-bypass-approvals-and-sandbox`` / ``--yolo`` /
``--dangerously-bypass-hook-trust`` / ``--ignore-rules`` and Claude
``--dangerously-skip-permissions`` / ``--allow-dangerously-skip-permissions`` — plus any future
``--dangerously*`` flag, defensively. The
**structured** full-access selectors
(Codex ``--sandbox danger-full-access``, Claude ``--permission-mode bypassPermissions``) are *not*
absolutely forbidden — an operator may opt in under ``security.strict_isolation: false`` — so they
are detected separately by :func:`find_full_access_args`, which the ``strict_isolation`` gate uses.
"""

from __future__ import annotations

from collections.abc import Sequence

# Sandbox value that must never be selected (full filesystem access, no sandbox).
FORBIDDEN_SANDBOX_VALUE = "danger-full-access"

# Standalone flags that disable the sandbox/approvals (those not caught by the ``--dangerously*``
# prefix rule). ``--allow-dangerously-skip-permissions`` (Claude) enables the bypass as an option
# without the ``--dangerously`` prefix, so it must be listed explicitly — it is the same bypass
# class
# as ``--dangerously-skip-permissions`` and stays absolutely forbidden (never operator-selectable).
_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "--yolo",
        "--ignore-rules",
        "--allow-dangerously-skip-permissions",
    }
)

# Flags that select the sandbox mode (long and short form).
_SANDBOX_FLAGS: frozenset[str] = frozenset({"--sandbox", "-s"})

# Claude's permission-mode flag and its full-bypass value. Used only by ``find_full_access_args``;
# the flag itself is legitimate (the orchestrator sets it) — only the bypass *value* is full access.
_PERMISSION_MODE_FLAG = "--permission-mode"
_BYPASS_PERMISSION_MODE = "bypassPermissions"

_BYPASS_REASON = "may not disable the sandbox/approvals"


def find_forbidden_args(args: Sequence[str]) -> list[str]:
    """Return a reason per offending token; an empty list means the args are safe.

    Reasons are unqualified (no config path / provider prefix) so each caller can frame them in its
    own terms (a config issue vs. a :class:`ProviderError` message).
    """
    reasons: list[str] = []
    for index, token in enumerate(args):
        flag = token.split("=", 1)[0]
        if flag.startswith("--dangerously") or flag in _FORBIDDEN_FLAGS:
            reasons.append(f"flag {token!r} {_BYPASS_REASON}")
            continue
        if flag in _SANDBOX_FLAGS:
            has_value = "=" in token or index + 1 < len(args)
            value = token.split("=", 1)[1] if "=" in token else _peek(args, index + 1)
            if not has_value or value == "":
                # A sandbox flag with no value (last token, or a trailing ``=``) is malformed: the
                # CLI would consume the next real flag as its value or error out. Reject it rather
                # than treat it as safe — defense in depth, it can never weaken isolation.
                # (``danger-full-access`` is no longer rejected here — it is a gated full-access
                # selector handled by ``find_full_access_args``.)
                reasons.append(f"{flag} requires a sandbox value (none given)")
    return reasons


def find_full_access_args(args: Sequence[str]) -> list[str]:
    """Return a reason per token that selects a provider full-access / no-isolation mode.

    Covers the **structured** full-access selectors — Codex ``--sandbox danger-full-access`` and
    Claude ``--permission-mode bypassPermissions`` (either ``flag value`` or ``flag=value`` form).
    Unlike :func:`find_forbidden_args` these are *not* absolutely forbidden: an operator may opt in
    under ``security.strict_isolation: false`` and owns the risk. The ``strict_isolation`` gate
    (provider preflight and the flow validator) uses this to reject them while the default
    ``strict_isolation: true`` holds — preserving the fail-closed posture (security rule #3).
    """
    reasons: list[str] = []
    for index, token in enumerate(args):
        flag, sep, inline = token.partition("=")
        value = inline if sep else _peek(args, index + 1)
        if flag in _SANDBOX_FLAGS and value == FORBIDDEN_SANDBOX_VALUE:
            reasons.append(
                f"--sandbox {FORBIDDEN_SANDBOX_VALUE!r} grants full access (no isolation)"
            )
        elif flag == _PERMISSION_MODE_FLAG and value == _BYPASS_PERMISSION_MODE:
            reasons.append(
                f"--permission-mode {_BYPASS_PERMISSION_MODE!r} disables permission prompts "
                "(no isolation)"
            )
    return reasons


def _peek(args: Sequence[str], index: int) -> str:
    return args[index] if index < len(args) else ""
