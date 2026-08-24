"""Task data model.

Defines the normalized task shapes and the front-matter schema constants the Task Parser populates.
The actual parsing, the validation gate, and duplicate-id detection live in the parser/gate
(they need the State Store + ledger); this module fixes only the shapes and the shared id-regex as
the one source of truth they all share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from wastech_orchestrator.config.schema import BranchMode, PublishScope

# The task-id grammar + validator are the single source of truth for every layer that turns the id
# into an artifact path component. They live in the ``security`` leaf (not here) because
# ``providers`` — which must validate the same id in its exchange/artifact path builders — may not
# import ``task`` (import-linter ``providers-are-leaf``). Re-exported here so the long-standing
# ``task.model`` import site keeps working; the redundant ``as`` alias marks the intentional
# re-export.
from wastech_orchestrator.security.identifiers import (
    TASK_ID_PATTERN as TASK_ID_PATTERN,
)
from wastech_orchestrator.security.identifiers import (
    is_valid_task_id as is_valid_task_id,
)

BRANCH_NAME_MAX_BYTES = 255
# Soft cap for auto-generated branch names (and operator branch_name overrides): keeps the full
# {prefix}/{epoch}-{task_id}-{slug} within GitHub/CI/`git log` column width. BRANCH_NAME_MAX_BYTES
# above is the hard Git limit; this is the readability budget the slug is truncated to fit.
BRANCH_NAME_MAX_LEN = 50
_BRANCH_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\")

# Front-matter schema. A task is "clean": it carries
# only identity/dispatch fields plus the sanctioned exceptions — the per-node ``nodes.<node-id>``
# block (``enabled`` disable + the best-effort ``model``/``reasoning``/``provider`` overrides) and
# ``auto_merge`` (task-wins). The flow node still *declares* the defaults for provider/model/
# reasoning; the ``nodes:`` overrides only overlay them per run, best-effort (an invalid override is
# warned + skipped, never fatal — see :class:`NodeOverride`). ``decomposition:`` and the planning
# gate still decide splitting; refinement-skip is deterministic (completeness classification).
# ``task_type`` is the dispatch key — it selects the flow (``implementation`` / ``deep_research`` /
# ``security_audit`` / an operator flow), never anything about *how* a node runs.
ALLOWED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "task_type",
        "branch_name",
        "branch_mode",
        "branch_ref",
        "publish",
        "auto_merge",
        "prompt_audit",
        "decomposition",
        "trust_level",
        "contacts",
        "depends_on",
        "subtasks",
        "priority",
        "queue",
        "nodes",
    }
)
# The front-matter keys a task must carry, enforced by ``task.validation_gate`` (which additionally
# rejects a blank ``title`` and an empty ``Description`` section). Not operator-configurable: ``id``
# becomes a branch fragment, a run directory, and a state-store key, so a config able to drop it
# would break identity rather than relax a policy (config v35 removed the key that pretended to).
REQUIRED_TASK_FIELDS: frozenset[str] = frozenset({"id", "title"})

# The queue tag partitions a git-distributed task pool across several worc instances: an instance
# only picks a pending task when ``task.queue == instance.queue`` (config ``orchestrator.queue``).
# Both sides default to ``"default"``, so an untagged pool with one untagged instance behaves
# exactly as before. Unlike ``priority`` (fail-open), the task field is **fail-closed**: a malformed
# value (non-string, or empty/whitespace) rejects the task.
DEFAULT_QUEUE = "default"

# Scheduling priority for the eligibility queue. Unlike the other constrained task fields (which
# reject on a bad value), priority is **fail-open**: an unrecognised string, a wrong type, or a
# missing value all fold to ``DEFAULT_PRIORITY`` so a typo in a scheduling hint never blocks an
# otherwise-valid task. This is the one source of truth the gate, the parser, and the cli
# scheduler all share.
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
    """Per-node task front matter (``nodes.<node-id>``): a disable toggle plus best-effort override.

    ``enabled`` is the node-disable toggle: ``None`` means default (the node runs), ``False``
    disables the node (any node present in the task's resolved flow may be disabled — which nodes
    are safe to disable is the operator's flow-authoring responsibility).

    ``model`` / ``reasoning`` / ``provider`` overlay the flow node's declared executor for this run
    only, letting one default flow cover several model/effort/provider variants without a separate
    flow file. They are **best-effort**: the gate validates only their shape (a non-empty string),
    and an override invalid for the resolved flow/config (provider not in ``agents.allowed``, or a
    reasoning level the provider does not support) is warned + skipped at run time, falling back to
    the flow's declared value — the task is never aborted (watch-mode compat). ``model`` is passed
    through unchecked (model names have no reliable tier ordering). The override chain is: task node
    override → flow node declaration → provider config default.
    """

    enabled: bool | None = None
    model: str | None = None
    reasoning: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class NormalizedTask:
    """A parsed, normalized task manifest: front matter plus the body Description.

    A "clean" task: identity/dispatch only, plus the sanctioned task-wins gates
    (the ``nodes.<node-id>`` block, ``auto_merge``, ``prompt_audit``, and ``decomposition``).
    The flow node *declares* provider/model/reasoning defaults; a task may overlay them per run via
    best-effort ``nodes.<node-id>.{model,reasoning,provider}`` overrides (:class:`NodeOverride`).
    The flow + planning still own *whether* a split happens (the task only flips the gate); the
    deterministic refine-skip stays flow-owned.
    """

    id: str
    title: str
    description: str
    # Dispatch key → flow: ``None`` defers to the registry default (``implementation``). The
    # task never selects the flow from prose and never patches the graph — it only names the flow.
    task_type: str | None = None
    # Full task branch override. ``None`` uses repo.branch_prefix + task id + slug. Ignored (a
    # validation warning) outside ``new`` branch mode — nothing to name in existing/current.
    branch_name: str | None = None
    # Where this task's git operations point. ``None`` defers to the instance
    # default ``repo.branch_mode`` (itself defaulting to ``new``); a task value wins outright.
    branch_mode: BranchMode | None = None
    # The existing branch to work in — required iff the resolved mode is ``existing``, a validation
    # reject otherwise. Must already exist locally or on the remote (checked at the preflight).
    branch_ref: str | None = None
    # Downgrade-only cap on the publish node: commit/push/pull_request. ``None``
    # defers to the flow's policy. A cap, never an escalation — effective scope is
    # ``min(flow_policy, publish)``; a no-op on a flow with no PR-publishing node.
    publish: PublishScope | None = None
    # Tri-state opt-in to auto-merge (DANGER: bypasses human review). The task value wins outright
    # True requests it, False always opts out, None defers to config.git.auto_merge.
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
    # Per-task approval policy for the dangerous-diff gate: ``"strict"`` or ``"auto"``. ``None``
    # defers to the global ``config.security.trust_level``. The task value wins (no operator gate).
    # Does not affect the ``security.protected_paths`` floor (global-only).
    trust_level: str | None = None
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
    # Per-node front matter, keyed by flow node id: the disable toggle plus the best-effort
    # model/reasoning/provider overrides. The gate validates shape only; for the disable toggle node
    # existence against the resolved flow is checked at flow resolution (fail-closed → terminal
    # ``failed``), while the model/reasoning/provider overrides are resolved best-effort at run time
    # (invalid fields warned + skipped, never fatal).
    node_overrides: dict[str, NodeOverride] = field(default_factory=dict)

    def disabled_nodes(self) -> frozenset[str]:
        """The flow node ids this task explicitly disables (``nodes.<node-id>.enabled: false``)."""
        return frozenset(n for n, ov in self.node_overrides.items() if ov.enabled is False)
