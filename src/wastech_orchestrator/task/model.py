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
# never sanitized (.agents/rules/security.md). Shared source of truth for P1 and the P5 parser.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Front-matter schema (spec §5, §19.3).
ALLOWED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "pr_title",
        "refined",
        "decompose",
        "auto_merge",
        "agents",
        "contacts",
        "model",
        "reasoning",
        "stages",
    }
)
REQUIRED_TASK_FIELDS: frozenset[str] = frozenset({"id", "title"})


def is_valid_task_id(task_id: str) -> bool:
    """Return True iff ``task_id`` matches the normalized id format (spec §19.3)."""
    return TASK_ID_PATTERN.fullmatch(task_id) is not None


@dataclass(frozen=True)
class StageParams:
    """Per-stage override (task front matter ``stages.<stage>``).

    ``model``/``reasoning`` may each be ``None`` to inherit the task-wide value (which in turn falls
    back to the provider default); the two resolve independently — see
    :meth:`NormalizedTask.model_for`. ``enabled`` is the stage-skip toggle: ``None`` means default
    (the stage runs), ``False`` skips the stage (only the stages in ``SKIPPABLE_STAGES`` may be
    disabled — enforced by the validation gate).
    """

    model: str | None = None
    reasoning: str | None = None
    enabled: bool | None = None


@dataclass(frozen=True)
class NormalizedTask:
    """A parsed, normalized task manifest: §5 front matter plus the body Description.

    Populated by the Task Parser in P5 — this phase only fixes the shape.
    """

    id: str
    title: str
    description: str
    pr_title: str | None = None
    refined: bool = False
    # Tri-state: True forces decomposition, False disables it, None defers to the config default.
    decompose: bool | None = None
    # Tri-state opt-in to auto-merge (DANGER: bypasses human review). True requests it (honored only
    # when config.git.auto_merge_allow_per_task), False always opts out, None defers to config.
    auto_merge: bool | None = None
    # Per-stage provider override (only agent-routed stages; only providers from agents.allowed).
    agents: dict[Stage, ProviderId] = field(default_factory=dict)
    contacts: list[str] = field(default_factory=list)
    model: str | None = None
    reasoning: str | None = None
    # Per-stage model/reasoning override (only the agent-routed stages in ROUTABLE_STAGES). Each
    # field resolves independently: per-stage override → task-wide → provider default.
    stage_params: dict[Stage, StageParams] = field(default_factory=dict)

    def model_for(self, stage: Stage) -> str | None:
        """Effective model: per-stage override → task-wide → None (provider default)."""
        sp = self.stage_params.get(stage)
        if sp is not None and sp.model is not None:
            return sp.model
        return self.model

    def reasoning_for(self, stage: Stage) -> str | None:
        """Effective reasoning: per-stage override → task-wide → None (provider default)."""
        sp = self.stage_params.get(stage)
        if sp is not None and sp.reasoning is not None:
            return sp.reasoning
        return self.reasoning

    def disabled_stages(self) -> frozenset[Stage]:
        """The stages this task explicitly disables (``stages.<stage>.enabled: false``)."""
        return frozenset(s for s, sp in self.stage_params.items() if sp.enabled is False)
