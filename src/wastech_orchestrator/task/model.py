"""Task data model.

Defines the normalized task shapes and the front-matter schema constants the Task Parser populates.
The actual parsing, the validation gate, and duplicate-id detection live in the parser/gate
(they need the State Store + ledger); this module fixes only the shapes and the shared id-regex as
the one source of truth they all share.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# A task id is strict and normalized: a lowercase alphanumeric first char, then up to
# 63 of [a-z0-9._-]; no whitespace, no leading dot/separator, 1..64 chars. Invalid ids are rejected,
# never sanitized (.agents/rules/security.md). Shared source of truth for the model and the parser.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BRANCH_NAME_MAX_BYTES = 255
# Soft cap for auto-generated branch names (and operator branch_name overrides): keeps the full
# {prefix}/{epoch}-{task_id}-{slug} within GitHub/CI/`git log` column width. BRANCH_NAME_MAX_BYTES
# above is the hard Git limit; this is the readability budget the slug is truncated to fit.
BRANCH_NAME_MAX_LEN = 50
_BRANCH_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\")

# Front-matter schema. A task is "clean": it carries
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
        "branch_name",
        "auto_merge",
        "prompt_audit",
        "decomposition",
        "contacts",
        "depends_on",
        "subtasks",
        "priority",
        "queue",
        "nodes",
    }
)
REQUIRED_TASK_FIELDS: frozenset[str] = frozenset({"id", "title"})

# The queue tag partitions a git-distributed task pool across several worc instances: an instance
# only picks a pending task when ``task.queue == instance.queue`` (config ``orchestrator.queue``).
# Both sides default to ``"default"``, so an untagged pool with one untagged instance behaves
# exactly as before. Unlike ``priority`` (fail-open), the task field is **fail-closed**: a malformed
# value (non-string, or empty/whitespace) rejects the task. See the multi-instance-task-queues ADR.
DEFAULT_QUEUE = "default"

# Scheduling priority for the eligibility queue. Unlike the other constrained task fields (which
# reject on a bad value), priority is **fail-open**: an unrecognised string, a wrong type, or a
# missing value all fold to ``DEFAULT_PRIORITY`` so a typo in a scheduling hint never blocks an
# otherwise-valid task (see docs/backlog/task-priority.md). This is the one source of truth the
# gate, the parser, and the cli scheduler all share.
TaskPriority = Literal["low", "mid", "high"]
DEFAULT_PRIORITY: TaskPriority = "mid"
_PRIORITY_RANK: dict[TaskPriority, int] = {"high": 0, "mid": 1, "low": 2}


def normalize_priority(value: object) -> TaskPriority:
    """Fold any front-matter value to ``low``/``mid``/``high``; else ``mid`` (fail-open)."""
    if isinstance(value, str):
        folded = value.strip().lower()
        if folded == "low":
            return "low"
        if folded == "high":
            return "high"
    return "mid"


def priority_rank(value: object) -> int:
    """Sort rank (lower runs first) for a raw front-matter priority value."""
    return _PRIORITY_RANK[normalize_priority(value)]


def is_valid_task_id(task_id: str) -> bool:
    """Return True iff ``task_id`` matches the normalized id format."""
    return TASK_ID_PATTERN.fullmatch(task_id) is not None


def is_valid_branch_name(branch_name: str) -> bool:
    """Return True iff ``branch_name`` is a safe Git branch ref name.

    This mirrors the practical ``git check-ref-format --branch`` constraints without invoking Git
    from the IO-free task gate, and adds a few guardrails that keep the value unambiguous in argv:
    no leading dash, no ``refs/`` prefix, and no ``HEAD`` pseudo-ref.
    """
    if not branch_name or len(branch_name.encode("utf-8")) > BRANCH_NAME_MAX_BYTES:
        return False
    if branch_name != branch_name.strip():
        return False
    if branch_name in {"@", "HEAD"} or branch_name.upper() == "HEAD":
        return False
    if branch_name.startswith(("/", "-", "refs/")) or branch_name.endswith(("/", ".")):
        return False
    if ".." in branch_name or "//" in branch_name or "@{" in branch_name:
        return False
    if any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _BRANCH_FORBIDDEN_CHARS for ch in branch_name
    ):
        return False
    for component in branch_name.split("/"):
        if not component or component.startswith((".", "-")) or component.endswith((".lock", ".")):
            return False
    return True


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

    A "clean" task: identity/dispatch only, plus the sanctioned task-wins gates
    (``nodes.<node-id>.enabled`` disable, ``auto_merge``, ``prompt_audit``, and ``decomposition``).
    The flow node owns provider/model/reasoning; the flow + planning still own *whether* a split
    happens (the task only flips the gate); the deterministic refine-skip stays flow-owned.
    """

    id: str
    title: str
    description: str
    # Dispatch key → flow (P0.4): ``None`` defers to the registry default (``implementation``). The
    # task never selects the flow from prose and never patches the graph — it only names the flow.
    task_type: str | None = None
    # Full task branch override. ``None`` uses repo.branch_prefix + task id + slug.
    branch_name: str | None = None
    # Tri-state opt-in to auto-merge (DANGER: bypasses human review). The task value wins outright
    # (PRE.2): True requests it, False always opts out, None defers to config.git.auto_merge.
    auto_merge: bool | None = None
    # Tri-state prompt-audit opt-in: True forces it for this task, False disables it, None defers to
    # the global config.prompt_audit. The task value always wins (no operator gate).
    prompt_audit: bool | None = None
    # Tri-state per-task decomposition gate: True permits a split for this task even when the global
    # ``agents.decomposition.enabled`` is off, False forbids one even when it is on, None defers to
    # the global. The task value wins (no operator gate). This only flips the *gate* — whether a
    # split actually happens is still decided by the flow's ``decomposition:`` block + the planning
    # node's proposal (or an operator ``subtasks:`` manifest); the task never patches the graph.
    decomposition: bool | None = None
    contacts: list[str] = field(default_factory=list)
    # Other task ids this task needs **merged** before it may start (non-blocking merge-gated
    # scheduling): the scheduler skips a dependent while a dependency is unmerged and runs other
    # eligible tasks instead. Empty by default. Distinct from a decomposition's per-subtask
    # ``depends_on`` (subtask orders within one task). Eligibility is computed live from PR/merge
    # state — there is no persisted schema for it.
    depends_on: tuple[str, ...] = ()
    # Scheduling priority for the eligibility queue: the scheduler runs eligible tasks high → mid →
    # low, breaking ties by filename. Fail-open — any unrecognised value normalizes to ``mid`` (the
    # default), so a typo never blocks a task. ``depends_on`` is always stronger (only eligible
    # tasks are ranked); this is a re-ordering, not a concurrency change.
    priority: TaskPriority = DEFAULT_PRIORITY
    # Queue tag for multi-instance partitioning: an instance only picks this task when its selector
    # (``orchestrator.queue``) equals this value — plain string equality, no balancing. Always a
    # non-empty string (the gate normalizes an absent value to ``DEFAULT_QUEUE`` and rejects a
    # malformed one). Decomposition subtasks inherit it implicitly — they run inside the parent's
    # pipeline on the parent's branch and never pass through the pending-file selection.
    queue: str = DEFAULT_QUEUE
    # Operator-authored decomposition: ordered repository-relative references to per-subtask spec
    # files. Presence ⇒ the orchestrator builds the decomposition from this manifest (reason
    # ``operator_authored``) instead of from the planning agent's proposal, and runs the units
    # exactly like an accepted agent split (one branch, one PR). The gate validates only the list
    # shape; path/file/count/linear validation runs at the pre-branch preflight in ``run_task``.
    subtasks: tuple[str, ...] = ()
    # Per-node disable toggle, keyed by flow node id. The gate validates shape only; node existence
    # against the resolved flow is checked at flow resolution (fail-closed → terminal ``failed``).
    node_overrides: dict[str, NodeOverride] = field(default_factory=dict)

    def disabled_nodes(self) -> frozenset[str]:
        """The flow node ids this task explicitly disables (``nodes.<node-id>.enabled: false``)."""
        return frozenset(n for n, ov in self.node_overrides.items() if ov.enabled is False)
