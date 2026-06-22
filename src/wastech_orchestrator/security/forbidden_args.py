"""Forbidden CLI flag detector (spec §11, §12.7, .agents/rules/security.md).

The single source of truth for "an option that bypasses the sandbox/approvals". It is called from
two places so the security invariant is enforced in depth:

* the config validator (:mod:`wastech_orchestrator.config.validation`) — at load time;
* the provider command builders (e.g. :mod:`wastech_orchestrator.providers.codex`) — at run time,
  so the policy cannot be weakened through a task or ``extra_args`` even if a config check is ever
  bypassed.

Covers Codex ``--dangerously-bypass-approvals-and-sandbox`` / ``--yolo`` /
``--dangerously-bypass-hook-trust`` / ``--ignore-rules`` / ``--sandbox danger-full-access`` and
Claude ``--dangerously-skip-permissions`` — plus any future ``--dangerously*`` flag, defensively.
"""

from __future__ import annotations

from collections.abc import Sequence

# Sandbox value that must never be selected (full filesystem access, no sandbox).
FORBIDDEN_SANDBOX_VALUE = "danger-full-access"

# Standalone flags that disable the sandbox/approvals (those not caught by the ``--dangerously*``
# prefix rule).
_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "--yolo",
        "--ignore-rules",
    }
)

# Flags that select the sandbox mode (long and short form).
_SANDBOX_FLAGS: frozenset[str] = frozenset({"--sandbox", "-s"})

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
                reasons.append(f"{flag} requires a sandbox value (none given)")
            elif value == FORBIDDEN_SANDBOX_VALUE:
                reasons.append(f"--sandbox may not be set to {FORBIDDEN_SANDBOX_VALUE!r}")
    return reasons


def _peek(args: Sequence[str], index: int) -> str:
    return args[index] if index < len(args) else ""
