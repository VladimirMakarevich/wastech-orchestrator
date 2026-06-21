"""Task data model (spec §5, §19.3).

Defines the normalized task shapes and the front-matter schema constants the Task Parser (P5) will
populate. The actual parsing, the §19 validation gate, and duplicate-id detection are P5 (they need
the State Store + ledger); here we fix only the shapes and the shared id-regex, so both phases share
one source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from wastech_orchestrator.providers.base import Stage

# A task id is strict and normalized (spec §19.3): a lowercase alphanumeric first char, then up to
# 63 of [a-z0-9._-]; no whitespace, no leading dot/separator, 1..64 chars. Invalid ids are rejected,
# never sanitized (.agents/rules/security.md). Shared source of truth for P1 and the P5 parser.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Front-matter schema (spec §5, §19.3). A task is "clean" (flow-contract §10, PRE.3): it carries
# only identity/dispatch fields plus the two sanctioned exceptions — ``stages.<stage>.enabled``
# (per-task stage skip) and ``auto_merge`` (task-wins). Provider/model/reasoning/decomposition are
# the flow's job (a node declares ``provider``/``model``/``reasoning``; ``decomposition:`` and the
# planning gate decide splitting); refinement-skip is deterministic (completeness classification).
# ``task_type`` is the dispatch key — it selects the flow (``implementation`` / ``deep_research`` /
# ``security_audit`` / an operator flow), never anything about *how* a node runs (P0.4).
ALLOWED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "task_type",
        "pr_title",
        "auto_merge",
        "prompt_audit",
        "contacts",
        "stages",
    }
)
REQUIRED_TASK_FIELDS: frozenset[str] = frozenset({"id", "title"})


def is_valid_task_id(task_id: str) -> bool:
    """Return True iff ``task_id`` matches the normalized id format (spec §19.3)."""
    return TASK_ID_PATTERN.fullmatch(task_id) is not None


@dataclass(frozen=True)
class StageParams:
    """Per-stage toggle (task front matter ``stages.<stage>``).

    ``enabled`` is the stage-skip toggle and the only sanctioned per-stage knob (PRE.3): ``None``
    means default (the stage runs), ``False`` skips the stage (only the stages in
    ``SKIPPABLE_STAGES`` may be disabled — enforced by the validation gate). Provider, model, and
    reasoning live on the flow node, never the task.
    """

    enabled: bool | None = None


@dataclass(frozen=True)
class NormalizedTask:
    """A parsed, normalized task manifest: §5 front matter plus the body Description.

    A "clean" task (flow-contract §10, PRE.3): identity/dispatch only, plus the two sanctioned
    exceptions (``stages.<stage>.enabled`` skip and ``auto_merge`` task-wins). The flow node owns
    provider/model/reasoning; the flow owns decomposition and the deterministic refinement-skip.
    """

    id: str
    title: str
    description: str
    # Dispatch key → flow (P0.4): ``None`` defers to the registry default (``implementation``). The
    # task never selects the flow from prose and never patches the graph — it only names the flow.
    task_type: str | None = None
    pr_title: str | None = None
    # Tri-state opt-in to auto-merge (DANGER: bypasses human review). The task value wins outright
    # (PRE.2): True requests it, False always opts out, None defers to config.git.auto_merge.
    auto_merge: bool | None = None
    # Tri-state prompt-audit opt-in: True forces it for this task, False disables it, None defers to
    # the global config.prompt_audit. The task value always wins (no operator gate).
    prompt_audit: bool | None = None
    contacts: list[str] = field(default_factory=list)
    # Per-stage skip toggle (only the stages in SKIPPABLE_STAGES; enforced by the validation gate).
    stage_params: dict[Stage, StageParams] = field(default_factory=dict)

    def disabled_stages(self) -> frozenset[Stage]:
        """The stages this task explicitly disables (``stages.<stage>.enabled: false``)."""
        return frozenset(s for s, sp in self.stage_params.items() if sp.enabled is False)
