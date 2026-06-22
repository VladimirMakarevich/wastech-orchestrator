"""Candidate validation (backlog: automatic check discovery).

Enforces the security rules on every check candidate — deterministic or agent-supplied — before it
is probed or run: an argv with no shell metacharacters, no sandbox-weakening flag, and not a denied
command. A candidate that is really a dependency-install/setup command is **rejected** (a check must
not mutate the environment). The same predicates run at config-load time, so the policy holds in
depth.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from wastech_orchestrator.checks.model import (
    CheckCandidate,
    argv_matches_denied,
    shell_metachars,
)
from wastech_orchestrator.security.forbidden_args import find_forbidden_args

# Verbs that mutate the environment / install dependencies — never a check.
_INSTALL_VERBS: frozenset[str] = frozenset({"install", "sync", "add", "update"})
_NODE_PMS: frozenset[str] = frozenset({"npm", "pnpm", "yarn"})


@dataclass(frozen=True)
class ValidationResult:
    """A validated candidate, or a rejection reason."""

    candidate: CheckCandidate | None
    rejection: str | None


class CheckCandidateValidator:
    """Apply the argv/security rules to a candidate (provider-agnostic, no I/O)."""

    def __init__(self, *, denied_commands: tuple[str, ...] = ()) -> None:
        self._denied = denied_commands

    def validate(self, candidate: CheckCandidate) -> ValidationResult:
        argv = candidate.argv
        if not argv:
            return ValidationResult(None, "empty argv")
        bad = shell_metachars(argv)
        if bad is not None:
            return ValidationResult(None, f"shell metacharacter in token {bad!r}")
        forbidden = find_forbidden_args(argv)
        if forbidden:
            return ValidationResult(None, "; ".join(forbidden))
        denied = argv_matches_denied(argv, self._denied)
        if denied is not None:
            return ValidationResult(None, f"matches denied command {denied!r}")
        if _is_install_command(argv):
            return ValidationResult(None, "dependency-install/setup command is not a check")
        return ValidationResult(candidate, None)


def _is_install_command(argv: Sequence[str]) -> bool:
    rest = list(argv[1:])
    if any(token in _INSTALL_VERBS for token in rest):
        return True
    head = argv[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return head in _NODE_PMS and "ci" in rest
