"""Flow schema — Python types for the YAML flow document.

The flow-graph document as frozen dataclasses.
Pure: no IO, no YAML parsing, no fingerprinting — only types.

``FlowNode`` is a Union discriminated by ``kind``; use ``isinstance`` to narrow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from wastech_orchestrator.config.schema import ObserveMode
from wastech_orchestrator.core.flow.contracts import (
    NetworkPolicy,
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    SessionScope,
)
from wastech_orchestrator.providers.base import ProviderId

#: Finding severities, most-severe first. Index = rank (lower index = more severe). The single
#: source of truth for the evaluator findings output-schema enum, the ``gate_severity`` comparison,
#: and ``gate_severity`` validation (see ``nodes/evaluator.py`` and ``snapshot.py``).
SEVERITY_ORDER: tuple[str, ...] = ("blocking", "critical", "high", "medium", "low")
#: Default evaluator gate: block on ``high`` and above (``high``/``critical``/``blocking``), leaving
#: ``medium``/``low`` advisory — the historical, hardcoded behavior.
DEFAULT_GATE_SEVERITY = "high"


@dataclass(frozen=True, slots=True)
class WhenPredicate:
    """Deterministic skip predicate: node is skipped when ``fact != equals``."""

    fact: str
    equals: bool = True


@dataclass(frozen=True, slots=True)
class HitlSettings:
    """HITL interaction permissions on an agent node."""

    allow_question: bool = False
    allow_approval: bool = False


@dataclass(frozen=True, slots=True)
class AgentNode:
    id: str
    kind: Literal["agent"]
    role_file: str
    #: optional second role file, used only on a turn that CONTINUES a session this node has
    #: already spoken on — a loop re-entry, a renewed turn grant, a delivered human answer. It is
    #: an ordinary role file (same flow-dir containment, same renderer, same variable set) selected
    #: on a different turn, so the full text is stated once per session instead of every round.
    #: The runner decides *whether* a turn may be a continuation; the provider seam decides whether
    #: the attempt actually is one, from the same field that decides the resume argv — so an
    #: attempt whose session was dropped gets the full text back. ``None`` keeps today's behavior
    #: byte for byte. Requires ``editing_lineage``: no other scope resumes across node runs.
    resume_role_file: str | None = None
    session_scope: SessionScope = SessionScope.FRESH_DISPOSABLE
    lineage_affinity: str | None = None
    permission_profile: PermissionProfile | None = None  # None → resolved from flow ceiling
    #: per-node override of the flow-wide network grant: ``True``/``False`` grant/deny network for
    #: this node alone; ``None`` (default) inherits the flow's ``network_policy`` default. Toggles
    #: only the network dimension — never the filesystem permission ceiling.
    network_access: bool | None = None
    #: ask for the read-only git verbs so this node can inspect delivery history (an audit node
    #: citing a commit rather than grepping a changelog). ``True`` requests the grant; ``False`` and
    #: ``None`` (default) do not. The request is only ever honored when the operator's
    #: ``security.allow_git_evidence`` is on — with it off a declaring node is accepted and inert,
    #: which is what keeps a flow from widening the envelope on its own. Grants reading only: the
    #: verbs cannot mutate, the sandbox write-denies the clone, and publishing stays the
    #: orchestrator's.
    git_evidence: bool | None = None
    #: the target repository's OWN harness skills this node must invoke, by name
    #: (``<repo>/.claude/skills/<name>/SKILL.md``, which must exist — the flow is refused if it
    #: does not). Legal only under ``security.strict_isolation: false``: under strict isolation
    #: ``--tools`` is a hard existence gate carrying the profile baseline, so the ``Skill`` tool
    #: does not exist for the session at all and a declaration there is a validation error rather
    #: than something accepted and inert. Declaring names turns skills ON for the node (see
    #: ``allow_skills``) and appends a Core-built block naming them to the effective prompt; that
    #: block also states that the security preamble and the role prompt win over anything a skill
    #: says. The skills themselves are ordinary repository files the CLI discovers and reads by
    #: itself: they are NOT frozen into the task's control bundle, and a writing node can change
    #: one mid-run.
    skills: tuple[str, ...] = ()
    #: whether this node may invoke skills at all — tri-state like ``network_access``. ``None``
    #: (default) resolves from ``skills``: a node that declares no skill runs with the provider's
    #: own per-attempt off-switch emitted (Claude ``--disable-slash-commands``, Codex
    #: ``--disable skill_search``), so a flow gets what it asked for and nothing else. ``True``
    #: turns skills on without requiring any particular one — an error under
    #: ``security.strict_isolation: true``, where the mode cannot give it; an absent key is not a
    #: request and is never an error. ``False`` refuses every skill and is legal at every value of
    #: that switch, because a flow may always narrow. It cannot be combined with ``skills``: neither
    #: CLI offers "these skills and no others", so the switch is all-or-nothing per node.
    allow_skills: bool | None = None
    #: which provider runs this node; None → the config's global primary. Validated against
    #: ``agents.allowed`` at preflight; never relaxes the security ceiling.
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None
    timeout_seconds: int | None = None
    output_schema: str | None = None  # JSON-encoded when present
    #: optional well-known artifact slot the agent's output is persisted to and threaded downstream
    #: (``enriched_spec`` / ``plan`` / ``summary``); the core writes it after the node runs.
    output_artifact: str | None = None
    #: the file this node *produces*, named by the flow: when set, that file's content — not the
    #: node's closing message — is what the ``{<node_id>_path}`` channel carries downstream. One
    #: portable filename (no separators, no ``..``), resolved inside the flow's ``output_policy``
    #: report directory, or the repository root for a policy without one. A node whose real product
    #: is a written document otherwise publishes only its own summary of it. Absent, or the file is
    #: missing/unreadable after the node runs, the channel keeps carrying the message.
    output_file: str | None = None
    #: a best-effort node tolerates an infrastructure failure (no provider could run it): the engine
    #: continues instead of failing the task (the summary stage — minimal-summary fallback).
    best_effort: bool = False
    hitl: HitlSettings | None = None
    extra_args: tuple[str, ...] = ()
    when: WhenPredicate | None = None


@dataclass(frozen=True, slots=True)
class EvaluatorNode:
    id: str
    kind: Literal["evaluator"]
    role: str
    role_file: str
    #: optional second role file for a continuation turn (see :class:`AgentNode`). Here it requires
    #: ``resume_own_lineage``: the evaluator's session is keyed by its own id and written only by
    #: its own successful pass, so a live session already means this role has spoken on it — no
    #: further check is needed, and none of the affinity ambiguity of an author node applies.
    resume_role_file: str | None = None
    session_scope: SessionScope = SessionScope.FRESH_DISPOSABLE
    permission_profile: PermissionProfile = PermissionProfile.READ_ONLY  # const per schema
    #: per-node override of the flow-wide network grant (see :class:`AgentNode`); ``None`` inherits
    #: the flow's ``network_policy`` default. Toggles only the network dimension.
    network_access: bool | None = None
    #: ask for the read-only git verbs (see :class:`AgentNode`); honored only when the operator's
    #: ``security.allow_git_evidence`` is on. An evaluator stays read-only either way.
    git_evidence: bool | None = None
    blocking: bool = True
    #: Per-instance rework ceiling for a NON-blocking evaluator (e.g. ``test_quality``): after this
    #: many rework verdicts it accepts (→ continue) instead of looping. Ignored when ``blocking`` is
    #: true — a blocking evaluator reworks until the flow's named-loop budget (e.g. ``review_fix``)
    #: is spent, then parks to ``manual`` (see ``EvaluatorRunner._verdict``).
    max_rework_per_stage: int = 1
    #: Minimum finding severity that gates (drives ``rework``): a finding whose severity is at least
    #: this severe blocks; less-severe findings are advisory. One of :data:`SEVERITY_ORDER`. Default
    #: ``high`` = block on high/critical/blocking (historical behavior). Lower it (e.g. ``low``) to
    #: make a content critic block on any finding. Orthogonal to ``blocking`` (which decides whether
    #: the evaluator gates at all): this decides *which* severities count.
    gate_severity: str = DEFAULT_GATE_SEVERITY
    #: which provider runs this evaluator; None → the config's global primary.
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None
    when: WhenPredicate | None = None


#: Filename the ``citation`` checker looks for in the flow's report dir when the node does not name
#: one. It is a named default rather than a literal inside the checker so that a flow whose writing
#: node uses another name can say so — otherwise the gate reports ``uncheckable: missing`` and
#: silently does nothing.
DEFAULT_CITATION_MANIFEST = "sources.json"


@dataclass(frozen=True, slots=True)
class ChecksNode:
    id: str
    kind: Literal["checks"]
    checker: Literal["command_profile", "citation", "dependency_scan"]
    #: ``citation`` only: the manifest filename inside the flow's report dir. A single path segment
    #: (no separators, no ``..``) — it names a file the flow's own writing node produced.
    manifest: str = DEFAULT_CITATION_MANIFEST
    when: WhenPredicate | None = None


@dataclass(frozen=True, slots=True)
class ToolNode:
    """A custom operator tool node: runs an operator executable out-of-process.

    Unlike :class:`ChecksNode` (whose ``checker`` is a closed core-owned ``Literal``), ``tool`` is a
    **free string** naming an operator executable registered under ``<repo>/.worc/tools/`` — the
    open operator set, exactly like a flow name. It is validated against the ``ToolRegistry`` in the
    config-aware validator (never a string ``..`` check): the registry owns path-containment
    (name → a file inside ``tools_dir``). The node runs the tool via the same ``run_process``
    ceiling as an agent (argv-without-shell, mandatory timeout, allowlisted env), gates the graph on
    exit-code/optional-JSON (``pass`` / ``fail`` / ``route:*``), and — like an agent node — exposes
    its stdout artifact downstream as ``{<id>_path}``.
    """

    id: str
    kind: Literal["tool"]
    #: registered tool name (``.worc/tools/<tool>``) — NOT a path; resolved by the ``ToolRegistry``.
    tool: str
    #: flat allowlisted scalar args from the flow (str/int/float/bool), no secrets. Passed to the
    #: tool on stdin verbatim; a non-scalar/nested value is a fatal load error.
    args: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    #: ``None`` → the config ``tools.default_timeout_seconds`` (default 3600s / 1h). Resolved once
    #: in the runner and passed to ``run_process`` as the mandatory ``int`` timeout.
    timeout_seconds: int | None = None
    when: WhenPredicate | None = None


@dataclass(frozen=True, slots=True)
class HitlNode:
    id: str
    kind: Literal["hitl"]
    signal: Literal["question", "approval"]
    timeout_s: int | None = None
    when: WhenPredicate | None = None


@dataclass(frozen=True, slots=True)
class PublishNode:
    id: str
    kind: Literal["publish"]
    policy: PublishingPolicy
    when: WhenPredicate | None = None


FlowNode = AgentNode | EvaluatorNode | ChecksNode | ToolNode | HitlNode | PublishNode


@dataclass(frozen=True, slots=True)
class Edge:
    """Flow graph edge. ``from_node`` maps to the YAML key ``from`` (Python keyword)."""

    from_node: str
    to: str
    outcome: str | None = None
    budget: int | None = None
    loop: str | None = None


#: Outcomes that charge a rework/fail edge's loop or inline budget (shared by the engine's
#: bookkeeping and any code that needs to reason about fix-loop budgets without the engine).
REWORK_OUTCOMES: frozenset[str] = frozenset({"rework", "fail"})


@dataclass(frozen=True, slots=True)
class DecompositionConfig:
    proposed_by: str
    sub_flow: tuple[str, ...]
    shared_budget: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisorObserveBlock:
    """Flow-local observation cadence — the same key path as the global one.

    ``supervisor.observe.mode`` reads identically in ``config.yaml`` and in a flow YAML, so there is
    one name to learn. ``None`` (the key absent) inherits the global mode; a declared mode may only
    *narrow* it (``none < events < selected < all``), which the config-aware validator enforces
    before any node runs. Nested rather than flat so the cadence's other knobs
    (``include_nodes``, ``triggers``) have a home if a flow ever needs them, without a second
    rename.
    """

    mode: ObserveMode | None = None


@dataclass(frozen=True, slots=True)
class SupervisorBlock:
    """Flow-local supervisor prompt overrides, the follow-ups opt-in, and the observation cadence.

    The supervisor is a constant layer above any flow; this block lets a flow reshape *its wording*
    without touching global config. Only wording moves into files — the structured-output schemas
    (``memory_delta``, ``follow_ups``) stay hardcoded in code, so an author can never break the
    machine contract the orchestrator parses.

    * ``role_file`` — the observe lens, overriding the global ``config.supervisor.role_file``.
      Unused when the cadence resolves to ``none``: nothing observes, so no lens is ever loaded.
    * ``finalize_role_file`` — the final-summary emphasis (no global counterpart — YAGNI).
    * ``handoff_role_file`` — the intra-task subtask handoff brief (no global counterpart). A third
      supervisor prompt, same contract: wording in a file, schema in code.
    * ``emit_follow_ups`` — opt the flow's finalize turn into the structured ``{summary,
      follow_ups}`` contract (a per-flow, code-oriented capability; default off). Memory is
      orthogonal (the same turn additionally emits ``memory_delta`` when memory is enabled).
    * ``observe`` — how often this flow's steps are worth an LLM note. This is where a per-flow
      cadence default belongs: the engine never maps a flow name to a mode.

    All prompt paths are validated as flow-dir-contained (fatal on traversal), like a node
    ``role_file``.
    """

    role_file: str | None = None
    finalize_role_file: str | None = None
    handoff_role_file: str | None = None
    emit_follow_ups: bool = False
    observe: SupervisorObserveBlock | None = None


@dataclass(frozen=True, slots=True)
class EvaluatorDefaults:
    """Default field values applied to evaluator nodes that omit the field."""

    session_scope: SessionScope = SessionScope.FRESH_DISPOSABLE
    permission_profile: PermissionProfile = PermissionProfile.READ_ONLY
    max_rework_per_stage: int = 1
    gate_severity: str = DEFAULT_GATE_SEVERITY


@dataclass(frozen=True, slots=True)
class FlowDefaults:
    evaluator: EvaluatorDefaults | None = None


@dataclass(frozen=True, slots=True)
class FlowDoc:
    """Parsed, resolved flow document.

    ``budgets`` is a :class:`~types.MappingProxyType` (read-only view); all other
    collection fields are immutable tuples. The document is not hashable — use
    :attr:`FlowSnapshot.flow_fingerprint` for identity.
    """

    name: str
    task_type: str
    permission_ceiling: PermissionProfile
    output_policy: OutputPolicy
    publishing: PublishingPolicy
    nodes: tuple[FlowNode, ...]
    edges: tuple[Edge, ...]
    budgets: MappingProxyType[str, int]
    network_policy: NetworkPolicy | None = None
    decomposition: DecompositionConfig | None = None
    #: flow-local supervisor prompt overrides + the follow-ups opt-in; ``None`` → the supervisor
    #: uses the global ``config.supervisor`` and its built-in finalize prompt (today's behavior).
    supervisor: SupervisorBlock | None = None
