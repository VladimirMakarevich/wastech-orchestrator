"""Task data model.

Defines the normalized task shapes and the front-matter schema constants the Task Parser populates.
The actual parsing, the validation gate, and duplicate-id detection live in the parser/gate
(they need the State Store + ledger); this module fixes only the shapes and the shared id-regex as
the one source of truth they all share.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A task id is strict and normalized: a lowercase alphanumeric first char, then up to
# 63 of [a-z0-9._-]; no whitespace, no leading dot/separator, 1..64 chars. Invalid ids are rejected,
# never sanitized (.agents/rules/security.md). Shared source of truth for the model and the parser.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Front-matter schema. A task is "clean" (flow-contract, PRE.3): it carries
# only identity/dispatch fields plus the two sanctioned exceptions — ``nodes.<node-id>.enabled``
# (per-task node disable) and ``auto_merge`` (task-wins). Provider/model/reasoning/decomposition are
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
        "nodes",
    }
)
REQUIRED_TASK_FIELDS: frozenset[str] = frozenset({"id", "title"})


def is_valid_task_id(task_id: str) -> bool:
    """Return True iff ``task_id`` matches the normalized id format."""
    return TASK_ID_PATTERN.fullmatch(task_id) is not None


@dataclass(frozen=True)
class NodeOverride:
    """Per-node toggle (task front matter ``nodes.<node-id>``).

    ``enabled`` is the node-disable toggle and the only sanctioned per-node knob: ``None`` means
    default (the node runs), ``False`` disables the node (any node present in the task's resolved
    flow may be disabled — which nodes are safe to disable is the operator's flow-authoring
    responsibility). Provider, model, and reasoning live on the flow node, never the task.
    """

    enabled: bool | None = None


@dataclass(frozen=True)
class NormalizedTask:
    """A parsed, normalized task manifest: front matter plus the body Description.

    A "clean" task (flow-contract, PRE.3): identity/dispatch only, plus the two sanctioned
    exceptions (``nodes.<node-id>.enabled`` disable and ``auto_merge`` task-wins). The flow node
    owns provider/model/reasoning; the flow owns decomposition and the deterministic refine-skip.
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
    # Per-node disable toggle, keyed by flow node id. The gate validates shape only; node existence
    # against the resolved flow is checked at flow resolution (fail-closed → terminal ``failed``).
    node_overrides: dict[str, NodeOverride] = field(default_factory=dict)

    def disabled_nodes(self) -> frozenset[str]:
        """The flow node ids this task explicitly disables (``nodes.<node-id>.enabled: false``)."""
        return frozenset(n for n, ov in self.node_overrides.items() if ov.enabled is False)
