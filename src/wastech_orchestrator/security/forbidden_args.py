"""Forbidden CLI flag detector.

The single source of truth for "an option that bypasses the sandbox/approvals". It is called from
two places so the security invariant is enforced in depth:

* the config validator (:mod:`wastech_orchestrator.config.validation`) — at load time;
* the provider command builders (e.g. :mod:`wastech_orchestrator.providers.codex`) — at run time,
  so the policy cannot be weakened through a task or ``extra_args`` even if a config check is ever
  bypassed.

Covers Codex ``--dangerously-bypass-approvals-and-sandbox`` / ``--yolo`` /
``--dangerously-bypass-hook-trust`` / ``--ignore-rules`` and Claude
``--dangerously-skip-permissions`` / ``--allow-dangerously-skip-permissions`` — plus any future
``--dangerously*`` flag, defensively. The **structured** full-access selectors (Codex
``--sandbox danger-full-access``, Claude ``--permission-mode bypassPermissions``) are covered here
too, and just as absolutely: each removes the write floor the whole product rests on — the first
discards the generated permission profile wholesale, so the clone's ``.git`` becomes writable and
the enforcement canary has no profile left to prove; the second drops the permission prompts. No
configuration selects either, at any value of any other key.
"""

from __future__ import annotations

from collections.abc import Sequence

# Sandbox value that must never be selected (full filesystem access, no sandbox).
FORBIDDEN_SANDBOX_VALUE = "danger-full-access"

# Standalone flags that disable the sandbox/approvals (those not caught by the ``--dangerously*``
# prefix rule). ``--allow-dangerously-skip-permissions`` (Claude) enables the bypass as an option
# without the ``--dangerously`` prefix, so it must be listed explicitly — it is the same bypass
# class
# as ``--dangerously-skip-permissions``.
_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "--yolo",
        "--ignore-rules",
        "--allow-dangerously-skip-permissions",
    }
)

# Flags that select the sandbox mode (long and short form).
_SANDBOX_FLAGS: frozenset[str] = frozenset({"--sandbox", "-s"})

# Claude's permission-mode flag and its full-bypass value: the flag itself is legitimate (the
# orchestrator sets it), only the bypass *value* is full access.
_PERMISSION_MODE_FLAG = "--permission-mode"
_BYPASS_PERMISSION_MODE = "bypassPermissions"

_BYPASS_REASON = "may not disable the sandbox/approvals"


def find_forbidden_args(args: Sequence[str]) -> list[str]:
    """Return a reason per offending token; an empty list means the args are safe.

    Reasons are unqualified (no config path / provider prefix) so each caller can frame them in its
    own terms (a config issue vs. a :class:`ProviderError` message). Both spellings of a valued
    flag are recognized (``--flag value`` and ``--flag=value``), so neither form slips through.
    """
    reasons: list[str] = []
    for index, token in enumerate(args):
        flag, separator, inline = token.partition("=")
        value = inline if separator else _peek(args, index + 1)
        if flag.startswith("--dangerously") or flag in _FORBIDDEN_FLAGS:
            reasons.append(f"flag {token!r} {_BYPASS_REASON}")
        elif flag in _SANDBOX_FLAGS:
            if value == FORBIDDEN_SANDBOX_VALUE:
                reasons.append(
                    f"--sandbox {FORBIDDEN_SANDBOX_VALUE!r} grants full access (no isolation)"
                )
            elif value == "":
                # A sandbox flag with no value (last token, or a trailing ``=``) is malformed: the
                # CLI would consume the next real flag as its value or error out. Reject it rather
                # than treat it as safe — defense in depth, it can never weaken isolation.
                reasons.append(f"{flag} requires a sandbox value (none given)")
        elif flag == _PERMISSION_MODE_FLAG and value == _BYPASS_PERMISSION_MODE:
            reasons.append(
                f"--permission-mode {_BYPASS_PERMISSION_MODE!r} disables permission prompts "
                "(no isolation)"
            )
    return reasons


def _peek(args: Sequence[str], index: int) -> str:
    return args[index] if index < len(args) else ""
