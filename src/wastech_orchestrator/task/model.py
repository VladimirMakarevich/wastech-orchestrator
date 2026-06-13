"""Task data model (spec §5, §19.3).

Defines the normalized task shapes and the front-matter schema constants the Task Parser (P5) will
populate. The actual parsing, the §19 validation gate, and duplicate-id detection are P5 (they need
the State Store + ledger); here we fix only the shapes and the shared id-regex, so both phases share
one source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from wastech_orchestrator.providers.base import ProviderId, Stage

# A task id is strict and normalized (spec §19.3): a lowercase alphanumeric first char, then up to
# 63 of [a-z0-9._-]; no whitespace, no leading dot/separator, 1..64 chars. Invalid ids are rejected,
# never sanitized (docs/rules/security.md). Shared source of truth for P1 and the P5 parser.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Front-matter schema (spec §5, §19.3).
ALLOWED_TASK_KEYS: frozenset[str] = frozenset(
    {"id", "title", "refined", "decompose", "agents", "contacts", "model", "reasoning"}
)
REQUIRED_TASK_FIELDS: frozenset[str] = frozenset({"id", "title"})


def is_valid_task_id(task_id: str) -> bool:
    """Return True iff ``task_id`` matches the normalized id format (spec §19.3)."""
    return TASK_ID_PATTERN.fullmatch(task_id) is not None


@dataclass(frozen=True)
class NormalizedTask:
    """A parsed, normalized task manifest: §5 front matter plus the body Description.

    Populated by the Task Parser in P5 — this phase only fixes the shape.
    """

    id: str
    title: str
    description: str
    refined: bool = False
    # Tri-state: True forces decomposition, False disables it, None defers to the config default.
    decompose: bool | None = None
    # Per-stage provider override (only agent-routed stages; only providers from agents.allowed).
    agents: dict[Stage, ProviderId] = field(default_factory=dict)
    contacts: list[str] = field(default_factory=list)
    model: str | None = None
    reasoning: str | None = None
