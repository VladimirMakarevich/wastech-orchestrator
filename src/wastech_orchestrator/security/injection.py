"""Front-matter injection scanner.

The structural guarantee comes first: task content reaches providers **only as file paths** in
:class:`~wastech_orchestrator.providers.base.AgentRunRequest` (``task_path``, ``plan_path``, …). No
task field is ever spliced into the CLI argv, the environment, the command path, the working paths,
or any security setting — so task body text can never become a CLI flag. This module is the
belt-and-braces scan **on top of** that contract: it inspects **front-matter values** (never the
body — legitimate tasks embed shell snippets) for argv-shaped tokens.

A value is rejected when it begins with ``-`` (an argv flag), contains an argv-shaped token
(``;`` `` ` `` ``|`` ``$(`` or a newline), or matches a known sandbox/approval-bypass flag shape
(via :func:`~wastech_orchestrator.security.forbidden_args.find_forbidden_args`). Mappings and lists
are scanned recursively.

Normalization is **reject, don't sanitize**: a value that would change under normalization is
rejected rather than silently fixed. The task ``id`` is bound by the strict
``^[a-z0-9][a-z0-9._-]{0,63}$`` regex (:func:`~wastech_orchestrator.task.model.is_valid_task_id`),
and route overrides are bound to the :class:`~wastech_orchestrator.providers.base.ProviderId` enum,
so the only free-text non-path front-matter fields (``title``, ``contacts``) cannot designate a
path. A bare path separator is therefore not a distinct reject here — the "path separator where a
non-path field is expected" case is already covered by those structural bindings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wastech_orchestrator.security.forbidden_args import find_forbidden_args

# Front-matter values must not look like a CLI argument. A value beginning with ``-`` is
# handled separately so the reason is specific.
INJECTION_SUBSTRINGS: tuple[str, ...] = (";", "`", "|", "$(", "\n", "\r")


@dataclass(frozen=True)
class InjectionFinding:
    """The first offending front-matter value found by :func:`scan_frontmatter`."""

    key: str  # the offending key path, e.g. ``agents.review`` or ``contacts[0]``
    reason: str  # a short human-readable cause

    @property
    def detail(self) -> str:
        return f"{self.key}: {self.reason}"


def scan_frontmatter(frontmatter: Mapping[str, Any]) -> InjectionFinding | None:
    """Scan all front-matter values for argv-shaped tokens; return the first finding or ``None``.

    The scan is uniform across **every** front-matter value (deliberately strict, no
    per-field exemptions — the rule for authors is "front-matter values are plain text", documented
    in the task-authoring guides). Keeping display fields in scope means the belt-and-braces scan
    never has to reason about which field could reach an argv, so it cannot regress.
    """
    for key, value in frontmatter.items():
        finding = scan_value(key, value)
        if finding is not None:
            return finding
    return None


def scan_value(key: str, value: Any) -> InjectionFinding | None:
    """Recursively scan one front-matter ``value`` (string / mapping / list). Pure."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("-"):
            return InjectionFinding(key, "value starts with '-'")
        if any(token in value for token in INJECTION_SUBSTRINGS):
            return InjectionFinding(key, "argv-shaped token")
        if find_forbidden_args([stripped]):
            return InjectionFinding(key, "forbidden flag shape")
        return None
    if isinstance(value, Mapping):
        for sub_key, sub_value in value.items():
            finding = scan_value(f"{key}.{sub_key}", sub_value)
            if finding is not None:
                return finding
        return None
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, item in enumerate(value):
            finding = scan_value(f"{key}[{index}]", item)
            if finding is not None:
                return finding
    return None
