"""Provider argument security policy (.agents/rules/security.md).

The common detector covers options that bypass sandbox/approvals for every CLI. Codex additionally
uses a closed typed parser because its generic flags and config overrides can expand authority. The
same decisions run at load time and immediately before provider spawn:

* the config validator (:mod:`wastech_orchestrator.config.validation`) — at load time;
* the provider command builders (e.g. :mod:`wastech_orchestrator.providers.codex`) — at run time,
  so the policy cannot be weakened through a task or ``extra_args`` even if a config check is ever
  bypassed.

The common detector covers Codex ``--dangerously-bypass-approvals-and-sandbox`` / ``--yolo`` /
``--dangerously-bypass-hook-trust`` / ``--ignore-rules`` and Claude
``--dangerously-skip-permissions`` — plus any future ``--dangerously*`` flag, defensively. The
**structured** full-access selectors are detected separately by :func:`find_full_access_args` for
the legacy/common isolation gate. Codex ``extra_args`` rejects every sandbox selector; its typed
``sandbox`` field is the only Codex full-access opt-in. Claude's ``--permission-mode
bypassPermissions`` remains gated by ``security.strict_isolation``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Full-filesystem sandbox marker used by the typed Codex field's strict-isolation gate.
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

# Claude's permission-mode flag and its full-bypass value. Used only by ``find_full_access_args``;
# the flag itself is legitimate (the orchestrator sets it) — only the bypass *value* is full access.
_PERMISSION_MODE_FLAG = "--permission-mode"
_BYPASS_PERMISSION_MODE = "bypassPermissions"

_BYPASS_REASON = "may not disable the sandbox/approvals"

# Codex ``extra_args`` is deliberately much smaller than the CLI surface. Every unlisted option is
# rejected because Codex adds authority-bearing flags and config keys over time; carrying a denylist
# would silently turn a CLI upgrade into a privilege expansion. These two switches only make config
# loading stricter, so they are safe on both fresh and resumed ``codex exec`` invocations.
_CODEX_SAFE_FLAGS: frozenset[str] = frozenset({"--ignore-user-config", "--strict-config"})

# These overrides affect response presentation or reasoning shape, not filesystem, network,
# approvals, tools, executable selection, environment inheritance, or instruction/rule loading.
# Model and reasoning effort already have typed provider/node fields, but accepting the effort key
# preserves the existing benign extension point without opening arbitrary config paths.
CODEX_SAFE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "disable_response_storage",
        "hide_agent_reasoning",
        "model_reasoning_effort",
        "model_reasoning_summary",
        "model_verbosity",
        "personality",
        "show_raw_agent_reasoning",
    }
)
_CODEX_CONFIG_FLAGS: frozenset[str] = frozenset({"-c", "--config"})
_CODEX_CONFIG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")


@dataclass(frozen=True, slots=True)
class CodexExtraArg:
    """One parsed, allowlisted Codex extension rendered in a canonical argv form."""

    option: str
    value: str | None = None

    def to_argv(self) -> tuple[str, ...]:
        """Render the typed option without changing its literal config value."""
        if self.value is None:
            return (self.option,)
        return (self.option, self.value)


class CodexExtraArgsError(ValueError):
    """Fail-closed Codex argument error whose messages never contain option values."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


def parse_codex_extra_args(args: Sequence[str]) -> tuple[CodexExtraArg, ...]:
    """Parse Codex ``extra_args`` into a closed set of harmless typed options.

    Both ``-c key=value`` and equals spellings such as ``--config=key=value`` are accepted and
    canonicalized. Unknown options, positional tokens, malformed pairs, profiles, feature
    switches, and authority-bearing config paths fail closed. Findings name only the option or
    validated config key; values are intentionally omitted because operator config can contain
    credentials.
    """
    parsed: list[CodexExtraArg] = []
    reasons: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        option, separator, inline_value = token.partition("=")

        if option in _CODEX_SAFE_FLAGS:
            if separator:
                reasons.append(f"Codex option {option!r} does not accept a value")
            else:
                parsed.append(CodexExtraArg(option))
            index += 1
            continue

        if option in _CODEX_CONFIG_FLAGS:
            if separator:
                override = inline_value
            elif index + 1 < len(args) and not args[index + 1].startswith("-"):
                index += 1
                override = args[index]
            else:
                reasons.append(f"Codex option {option!r} requires a key=value override")
                index += 1
                continue
            config_key = _codex_config_key(override)
            if config_key is None:
                reasons.append(f"Codex option {option!r} requires a well-formed key=value override")
            elif config_key not in CODEX_SAFE_CONFIG_KEYS:
                reasons.append(
                    f"Codex config key {config_key!r} is not allowed in extra_args; "
                    "use a typed orchestrator setting for authority-bearing options"
                )
            else:
                # Always render the long spelling as two tokens. This keeps parsing deterministic
                # across the fresh and resume grammars while preserving the exact TOML value.
                parsed.append(CodexExtraArg("--config", override))
            index += 1
            continue

        if token.startswith("-"):
            # The part before '=' identifies the rejected option while suppressing any inline value.
            reasons.append(f"Codex option {option!r} is not allowed in extra_args")
        else:
            reasons.append("Codex positional values are not allowed in extra_args")
        index += 1

    if reasons:
        raise CodexExtraArgsError(reasons)
    return tuple(parsed)


def render_codex_extra_args(args: Sequence[str]) -> tuple[str, ...]:
    """Validate and flatten Codex ``extra_args`` for the final process argv."""
    return tuple(token for item in parse_codex_extra_args(args) for token in item.to_argv())


def _codex_config_key(override: str) -> str | None:
    key, separator, value = override.partition("=")
    if not separator or not value or _CODEX_CONFIG_KEY_RE.fullmatch(key) is None:
        return None
    return key


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
