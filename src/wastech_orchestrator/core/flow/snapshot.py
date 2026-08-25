"""Flow snapshot — loader and resolver for flow YAML files.

``load_flow(path)`` reads a YAML file, applies ``defaults``, builds lookup
tables, and returns an immutable :class:`FlowSnapshot`. The ``flow_fingerprint``
is the SHA-256 over the raw ``flow:`` dict (key-order independent), so identical
content always produces the same fingerprint regardless of YAML key ordering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from wastech_orchestrator.config.schema import ObserveMode
from wastech_orchestrator.core.flow.contracts import (
    NetworkPolicy,
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    SessionScope,
    fingerprint,
)
from wastech_orchestrator.core.flow.schema import (
    DEFAULT_CITATION_MANIFEST,
    DEFAULT_GATE_SEVERITY,
    SEVERITY_ORDER,
    AgentNode,
    ChecksNode,
    DecompositionConfig,
    Edge,
    EvaluatorDefaults,
    EvaluatorNode,
    FlowDefaults,
    FlowDoc,
    FlowNode,
    HitlNode,
    HitlSettings,
    PublishNode,
    SupervisorBlock,
    SupervisorObserveBlock,
    ToolNode,
    WhenPredicate,
)
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security.identifiers import (
    NODE_ID_PATTERN,
    is_portable_path_segment,
    is_valid_node_id,
)


class FlowLoadError(Exception):
    """Raised when a flow YAML cannot be parsed or resolved into a snapshot."""


# -- fail-closed field allowlists ---------------------------------------------
#
# Every mapping in the flow document is checked against an explicit allowlist: an unknown key is a
# fatal load error, never silently ignored. This is the structural ``additionalProperties: false``
# field-allowlist gate — the mechanism that keeps operator YAML a closed allowlist rather
# than an open dict.

_FLOW_FIELDS = frozenset(
    {
        "name",
        "task_type",
        "permission_ceiling",
        "output_policy",
        "publishing",
        "network_policy",
        "defaults",
        "nodes",
        "edges",
        "budgets",
        "decomposition",
        "supervisor",
    }
)
_AGENT_FIELDS = frozenset(
    {
        "id",
        "kind",
        "role_file",
        "session_scope",
        "lineage_affinity",
        "permission_profile",
        "network_access",
        "git_evidence",
        "provider",
        "model",
        "reasoning",
        "timeout_seconds",
        "output_schema",
        "output_artifact",
        "output_file",
        "best_effort",
        "hitl",
        "extra_args",
        "when",
    }
)
_EVALUATOR_FIELDS = frozenset(
    {
        "id",
        "kind",
        "role",
        "role_file",
        "session_scope",
        "permission_profile",
        "network_access",
        "git_evidence",
        "blocking",
        "max_rework_per_stage",
        "gate_severity",
        "provider",
        "model",
        "reasoning",
        "when",
    }
)
_CHECKS_FIELDS = frozenset({"id", "kind", "checker", "manifest", "when"})
_TOOL_FIELDS = frozenset({"id", "kind", "tool", "args", "timeout_seconds", "when"})
_HITL_NODE_FIELDS = frozenset({"id", "kind", "signal", "timeout_s", "when"})
_PUBLISH_FIELDS = frozenset({"id", "kind", "policy", "when"})
_EDGE_FIELDS = frozenset({"from", "to", "outcome", "budget", "loop"})
_WHEN_FIELDS = frozenset({"fact", "equals"})
_HITL_SETTINGS_FIELDS = frozenset({"allow_question", "allow_approval"})
_DECOMPOSITION_FIELDS = frozenset(
    {
        "proposed_by",
        "sub_flow",
        "shared_budget",
    }
)
_SUPERVISOR_FIELDS = frozenset(
    {
        "role_file",
        "finalize_role_file",
        "handoff_role_file",
        "emit_follow_ups",
        "observe",
    }
)
_SUPERVISOR_OBSERVE_FIELDS = frozenset({"mode"})
_DEFAULTS_FIELDS = frozenset({"evaluator"})
_EVALUATOR_DEFAULTS_FIELDS = frozenset(
    {
        "session_scope",
        "permission_profile",
        "max_rework_per_stage",
        "gate_severity",
    }
)

# Core checker set: flow may not invent a checker kind.
_CHECKER_KINDS = frozenset({"command_profile", "citation", "dependency_scan"})

# Output-artifact slots: the well-known names an agent node may persist its output to. The
# slot vocabulary is core-fixed (a flow may not invent a slot — fail-closed at load).
_OUTPUT_ARTIFACT_SLOTS = frozenset({"enriched_spec", "plan", "summary", "report"})

# Reserved core-variable prefixes an **agent or tool** node id may not collide with: both node
# kinds expose ``{<id>_path}``, so an id equal to one of these — or starting
# with ``subtask`` — would shadow a fixed core variable (``{plan_path}``, ``{review_path}``,
# ``{subtask_spec_path}``, …). A collision is a fatal load error. Evaluator/checks/human nodes do
# not get ``{<id>_path}`` (so the packaged ``review`` evaluator and ``testing`` checks node are ok).
_RESERVED_NODE_ID_NAMES = frozenset(
    {"task", "plan", "diff", "checks", "review", "repo", "memory", "stage"}
)
_RESERVED_NODE_ID_PREFIX = "subtask"

# ``when`` fact namespaces. The exact value allowlist per namespace belongs to the engine's fact
# resolver; here we fail-closed on the namespace prefix so a bare/typo'd fact (e.g.
# ``summary_enabled`` with no namespace) is rejected at load time.
_WHEN_FACT_NAMESPACES = ("derived.", "config.")


def _enum[EnumT: StrEnum](enum_cls: type[EnumT], value: object, ctx: str) -> EnumT:
    """Coerce ``value`` into ``enum_cls``, raising :class:`FlowLoadError` on a bad value."""
    try:
        return enum_cls(value)  # type: ignore[arg-type]  # non-str YAML values are caught below
    except (ValueError, TypeError) as exc:
        valid = sorted(e.value for e in enum_cls)
        raise FlowLoadError(
            f"invalid {enum_cls.__name__} {value!r} in {ctx}; valid values: {valid}"
        ) from exc


def _reject_unknown(raw: dict[str, Any], allowed: frozenset[str], ctx: str) -> None:
    """Fail-closed: raise if ``raw`` carries any key outside ``allowed``."""
    extra = sorted(set(raw) - allowed)
    if extra:
        raise FlowLoadError(f"unknown field(s) {extra} in {ctx} (fail-closed)")


def _parse_tristate(raw: dict[str, Any], key: str) -> bool | None:
    """A tri-state per-node capability flag: ``None`` (omitted) vs an explicit ``bool``.

    ``None`` must be preserved rather than coerced — ``bool(None)`` is ``False``, which would turn
    "omitted" into an explicit deny and lose the distinction the caller resolves against its own
    default (``network_access`` inherits the flow's ``network_policy``; ``git_evidence`` does not
    inherit anything today, but is parsed the same way so both fields read alike).
    """
    value = raw.get(key)
    return None if value is None else bool(value)


@dataclass(frozen=True, slots=True)
class FlowSnapshot:
    """Resolved, immutable representation of a loaded flow.

    ``nodes_by_id`` and ``adjacency`` are derived lookup structures; both are
    backed by ``MappingProxyType`` to prevent accidental mutation. The snapshot
    is not hashable — ``flow_fingerprint`` is the stable identity string.
    """

    doc: FlowDoc
    nodes_by_id: MappingProxyType[str, FlowNode]
    adjacency: MappingProxyType[str, tuple[Edge, ...]]  # outgoing edges per node id
    flow_fingerprint: str  # SHA-256 over the raw ``flow:`` YAML dict
    source_path: Path | None = None


def load_flow(path: Path) -> FlowSnapshot:
    """Load a flow YAML file and return an immutable :class:`FlowSnapshot`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FlowLoadError(f"cannot read flow file {path}: {exc}") from exc

    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FlowLoadError(f"YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict) or "flow" not in raw:
        raise FlowLoadError(f"expected top-level 'flow:' key in {path}")

    raw_flow: dict[str, Any] = raw["flow"]
    fp = fingerprint(raw_flow)
    doc = _parse_flow_doc(raw_flow, source=str(path))

    # A duplicate node id would silently collapse into one map entry (last-wins), so the shadowed
    # node — and any edge into it — would vanish without warning. Fail closed instead: each id must
    # be unique (each is also a `{<id>_path}` prompt token and an artifact-dir component).
    seen_ids: set[str] = set()
    for node in doc.nodes:
        if node.id in seen_ids:
            raise FlowLoadError(
                f"duplicate node id {node.id!r} in {path} (node ids must be unique)"
            )
        seen_ids.add(node.id)
    nodes_by_id: dict[str, FlowNode] = {n.id: n for n in doc.nodes}
    adj: dict[str, list[Edge]] = {}
    for edge in doc.edges:
        adj.setdefault(edge.from_node, []).append(edge)

    return FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType(nodes_by_id),
        adjacency=MappingProxyType({k: tuple(v) for k, v in adj.items()}),
        flow_fingerprint=fp,
        source_path=path,
    )


def reachable_nodes(snapshot: FlowSnapshot, start: str) -> frozenset[str]:
    """Every node reachable forward from ``start`` (inclusive), walking ``snapshot.adjacency``."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        stack.extend(e.to for e in snapshot.adjacency.get(node_id, ()))
    return frozenset(seen)


# -- internal parsing helpers -------------------------------------------------


def _require(d: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in d:
        raise FlowLoadError(f"missing required field '{key}' in {ctx}")
    return d[key]


def _parse_when(raw: Any) -> WhenPredicate | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FlowLoadError(f"'when' must be a mapping, got {type(raw).__name__}")
    _reject_unknown(raw, _WHEN_FIELDS, "when predicate")
    fact = str(_require(raw, "fact", "when"))
    if not fact.startswith(_WHEN_FACT_NAMESPACES):
        raise FlowLoadError(
            f"when.fact {fact!r} must be namespaced "
            f"(one of {list(_WHEN_FACT_NAMESPACES)}); bare facts are rejected fail-closed"
        )
    return WhenPredicate(fact=fact, equals=bool(raw.get("equals", True)))


def _parse_hitl_settings(raw: Any) -> HitlSettings | None:
    if raw is None:
        return None
    _reject_unknown(raw, _HITL_SETTINGS_FIELDS, "hitl settings")
    return HitlSettings(
        allow_question=bool(raw.get("allow_question", False)),
        allow_approval=bool(raw.get("allow_approval", False)),
    )


def _validate_node_id(nid: str, kind: str) -> None:
    """Fail-closed: a node id must be a portable single path segment / prompt token.

    Every node id becomes an artifact path component (``stages/<id>/…`` in both the private and the
    exchange roots) and — for agent/tool nodes — the ``{<id>_path}`` prompt token, so it must be one
    bounded lowercase segment writable on every OS. Rejected here at load, before any lookup map,
    artifact directory, or DB run row is built. Reject, never sanitize — an incompatible custom id
    gets this precise upgrade error, not a silently rewritten name.
    """
    if not is_valid_node_id(nid):
        raise FlowLoadError(
            f"{kind} node id {nid!r} is not a portable identifier; expected "
            f"{NODE_ID_PATTERN.pattern} and not a Windows device name (con, nul, com1-9, lpt1-9, …)"
        )


def _check_reserved_node_id(nid: str, kind: str) -> None:
    """Fail-closed: a kind that exposes ``{<id>_path}`` (agent, tool) may not shadow a core var.

    Shared by the agent and tool parsers — both node kinds get the generic ``{<id>_path}`` channel,
    so an id equal to a reserved core-variable name (or starting with ``subtask``) is fatal.
    """
    if nid in _RESERVED_NODE_ID_NAMES or nid.startswith(_RESERVED_NODE_ID_PREFIX):
        raise FlowLoadError(
            f"{kind} node id {nid!r} collides with a reserved core-variable prefix "
            f"(its {{{nid}_path}} would shadow a fixed core variable); reserved: "
            f"{sorted(_RESERVED_NODE_ID_NAMES)} and any id starting with "
            f"{_RESERVED_NODE_ID_PREFIX!r}"
        )


def _parse_agent_node(raw: dict[str, Any]) -> AgentNode:
    nid = str(_require(raw, "id", "agent node"))
    _validate_node_id(nid, "agent")
    ctx = f"agent node '{nid}'"
    _reject_unknown(raw, _AGENT_FIELDS, ctx)
    _check_reserved_node_id(nid, "agent")
    role_file = str(_require(raw, "role_file", ctx))

    pp_raw = raw.get("permission_profile")
    permission_profile = _enum(PermissionProfile, pp_raw, ctx) if pp_raw is not None else None

    provider_raw = raw.get("provider")
    provider = _enum(ProviderId, provider_raw, ctx) if provider_raw is not None else None

    ss_raw = raw.get("session_scope", SessionScope.FRESH_DISPOSABLE)

    os_raw = raw.get("output_schema")
    output_schema: str | None = json.dumps(os_raw, sort_keys=True) if os_raw is not None else None

    output_artifact = raw.get("output_artifact") or None
    if output_artifact is not None and output_artifact not in _OUTPUT_ARTIFACT_SLOTS:
        raise FlowLoadError(
            f"invalid output_artifact {output_artifact!r} in {ctx}; "
            f"valid slots: {sorted(_OUTPUT_ARTIFACT_SLOTS)}"
        )

    output_file = _parse_output_file(raw.get("output_file"), ctx, slot=output_artifact)

    return AgentNode(
        id=nid,
        kind="agent",
        role_file=role_file,
        session_scope=_enum(SessionScope, ss_raw, ctx),
        lineage_affinity=raw.get("lineage_affinity") or None,
        permission_profile=permission_profile,
        network_access=_parse_tristate(raw, "network_access"),
        git_evidence=_parse_tristate(raw, "git_evidence"),
        provider=provider,
        model=raw.get("model") or None,
        reasoning=raw.get("reasoning") or None,
        timeout_seconds=raw.get("timeout_seconds"),
        output_schema=output_schema,
        output_artifact=output_artifact,
        output_file=output_file,
        best_effort=bool(raw.get("best_effort", False)),
        hitl=_parse_hitl_settings(raw.get("hitl")),
        extra_args=tuple(str(a) for a in raw.get("extra_args", [])),
        when=_parse_when(raw.get("when")),
    )


def _parse_gate_severity(value: Any, ctx: str) -> str:
    """Validate a ``gate_severity`` token against :data:`SEVERITY_ORDER` (fail-closed)."""
    token = str(value).lower()
    if token not in SEVERITY_ORDER:
        raise FlowLoadError(
            f"invalid 'gate_severity' {value!r} in {ctx}: must be one of {list(SEVERITY_ORDER)}"
        )
    return token


def _parse_evaluator_node(raw: dict[str, Any], defaults: EvaluatorDefaults) -> EvaluatorNode:
    nid = str(_require(raw, "id", "evaluator node"))
    _validate_node_id(nid, "evaluator")
    ctx = f"evaluator node '{nid}'"
    _reject_unknown(raw, _EVALUATOR_FIELDS, ctx)
    role = str(_require(raw, "role", ctx))
    role_file = str(_require(raw, "role_file", ctx))

    ss_raw = raw.get("session_scope", defaults.session_scope)
    pp_raw = raw.get("permission_profile", defaults.permission_profile)

    provider_raw = raw.get("provider")
    provider = _enum(ProviderId, provider_raw, ctx) if provider_raw is not None else None

    return EvaluatorNode(
        id=nid,
        kind="evaluator",
        role=role,
        role_file=role_file,
        session_scope=_enum(SessionScope, ss_raw, ctx),
        permission_profile=_enum(PermissionProfile, pp_raw, ctx),
        network_access=_parse_tristate(raw, "network_access"),
        git_evidence=_parse_tristate(raw, "git_evidence"),
        blocking=bool(raw.get("blocking", True)),
        max_rework_per_stage=int(raw.get("max_rework_per_stage", defaults.max_rework_per_stage)),
        gate_severity=_parse_gate_severity(raw.get("gate_severity", defaults.gate_severity), ctx),
        provider=provider,
        model=raw.get("model") or None,
        reasoning=raw.get("reasoning") or None,
        when=_parse_when(raw.get("when")),
    )


def _parse_checks_node(raw: dict[str, Any]) -> ChecksNode:
    nid = str(_require(raw, "id", "checks node"))
    _validate_node_id(nid, "checks")
    ctx = f"checks node '{nid}'"
    _reject_unknown(raw, _CHECKS_FIELDS, ctx)
    checker = str(_require(raw, "checker", ctx))
    if checker not in _CHECKER_KINDS:
        raise FlowLoadError(
            f"invalid checker {checker!r} in {ctx}; valid values: {sorted(_CHECKER_KINDS)}"
        )
    return ChecksNode(
        id=nid,
        kind="checks",
        checker=checker,  # type: ignore[arg-type]
        manifest=_parse_manifest(raw.get("manifest", DEFAULT_CITATION_MANIFEST), ctx),
        when=_parse_when(raw.get("when")),
    )


def _parse_output_file(value: Any, ctx: str, *, slot: str | None) -> str | None:
    """Validate an agent node's ``output_file`` as one portable filename it produces.

    Resolved against a directory the orchestrator owns, so it goes through the same segment
    validator the exchange and the citation manifest use: no separators, no ``..``, no absolute
    path, no reserved name. Declaring it together with ``output_artifact`` is a fatal
    contradiction — a slot node's channel *is* its slot, so the produced file is never read.
    """
    if value is None:
        return None
    name = str(value)
    if not is_portable_path_segment(name):
        raise FlowLoadError(
            f"invalid 'output_file' {name!r} in {ctx}: must be a single portable filename "
            "(no path separators, no '..', not a reserved name)"
        )
    if slot is not None:
        raise FlowLoadError(
            f"{ctx} declares both 'output_file' and 'output_artifact' {slot!r}: a slot node's "
            "channel is its slot, so the produced file would never be read — choose one"
        )
    return name


def _parse_manifest(value: Any, ctx: str) -> str:
    """Validate a checks node's ``manifest`` as one portable path segment inside the report dir.

    A flow-authored filename resolved against a directory is a traversal surface, so it goes through
    the same segment validator the exchange uses: no separators, no ``..``, no absolute path, no
    Windows-reserved name.
    """
    name = str(value)
    if not is_portable_path_segment(name):
        raise FlowLoadError(
            f"invalid 'manifest' {name!r} in {ctx}: must be a single portable filename "
            "(no path separators, no '..', not a reserved name)"
        )
    return name


_SCALAR_TYPES = (str, int, float, bool)


def _parse_tool_args(raw: Any, ctx: str) -> dict[str, str | int | float | bool]:
    """Parse a tool node's ``args`` as a flat allowlisted scalar mapping (no nesting, no secrets).

    A nested mapping / list / ``None`` / any non-scalar value is a fatal load error — the tool
    contract passes only flat scalars on stdin. ``bool`` is accepted (an ``int`` subclass,
    already covered by the scalar tuple).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FlowLoadError(f"'args' must be a mapping in {ctx}, got {type(raw).__name__}")
    out: dict[str, str | int | float | bool] = {}
    for key, value in raw.items():
        if not isinstance(value, _SCALAR_TYPES):
            raise FlowLoadError(
                f"'args.{key}' in {ctx} must be a scalar (str/int/float/bool), "
                f"got {type(value).__name__}"
            )
        out[str(key)] = value
    return out


def _parse_tool_node(raw: dict[str, Any]) -> ToolNode:
    nid = str(_require(raw, "id", "tool node"))
    _validate_node_id(nid, "tool")
    ctx = f"tool node '{nid}'"
    _reject_unknown(raw, _TOOL_FIELDS, ctx)
    _check_reserved_node_id(nid, "tool")
    return ToolNode(
        id=nid,
        kind="tool",
        tool=str(_require(raw, "tool", ctx)),
        args=_parse_tool_args(raw.get("args"), ctx),
        timeout_seconds=raw.get("timeout_seconds"),
        when=_parse_when(raw.get("when")),
    )


def _parse_hitl_node(raw: dict[str, Any]) -> HitlNode:
    nid = str(_require(raw, "id", "hitl node"))
    _validate_node_id(nid, "hitl")
    ctx = f"hitl node '{nid}'"
    _reject_unknown(raw, _HITL_NODE_FIELDS, ctx)
    signal = str(_require(raw, "signal", ctx))
    if signal not in ("question", "approval"):
        raise FlowLoadError(
            f"invalid signal {signal!r} in {ctx}; valid values: ['approval', 'question']"
        )
    return HitlNode(
        id=nid,
        kind="hitl",
        signal=signal,  # type: ignore[arg-type]
        timeout_s=raw.get("timeout_s"),
        when=_parse_when(raw.get("when")),
    )


def _parse_publish_node(raw: dict[str, Any]) -> PublishNode:
    nid = str(_require(raw, "id", "publish node"))
    _validate_node_id(nid, "publish")
    ctx = f"publish node '{nid}'"
    _reject_unknown(raw, _PUBLISH_FIELDS, ctx)
    policy = str(_require(raw, "policy", ctx))
    return PublishNode(
        id=nid,
        kind="publish",
        policy=_enum(PublishingPolicy, policy, ctx),
        when=_parse_when(raw.get("when")),
    )


def _parse_node(raw: dict[str, Any], defaults: FlowDefaults) -> FlowNode:
    kind = raw.get("kind")
    ev_defaults = defaults.evaluator if defaults.evaluator is not None else EvaluatorDefaults()
    if kind == "agent":
        return _parse_agent_node(raw)
    elif kind == "evaluator":
        return _parse_evaluator_node(raw, ev_defaults)
    elif kind == "checks":
        return _parse_checks_node(raw)
    elif kind == "tool":
        return _parse_tool_node(raw)
    elif kind == "hitl":
        return _parse_hitl_node(raw)
    elif kind == "publish":
        return _parse_publish_node(raw)
    else:
        raise FlowLoadError(f"unknown node kind {kind!r} in node {raw.get('id', '?')!r}")


def _parse_edge(raw: dict[str, Any]) -> Edge:
    _reject_unknown(raw, _EDGE_FIELDS, "edge")
    from_node = str(_require(raw, "from", "edge"))
    to = str(_require(raw, "to", "edge"))
    return Edge(
        from_node=from_node,
        to=to,
        outcome=raw.get("outcome") or None,
        budget=raw.get("budget"),
        loop=raw.get("loop") or None,
    )


def _parse_decomposition(raw: Any) -> DecompositionConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FlowLoadError(f"'decomposition' must be a mapping, got {type(raw).__name__}")
    _reject_unknown(raw, _DECOMPOSITION_FIELDS, "decomposition")
    proposed_by = str(_require(raw, "proposed_by", "decomposition"))
    sub_flow_raw = _require(raw, "sub_flow", "decomposition")
    return DecompositionConfig(
        proposed_by=proposed_by,
        sub_flow=tuple(str(s) for s in sub_flow_raw),
        shared_budget=raw.get("shared_budget") or None,
    )


def _parse_supervisor(raw: Any) -> SupervisorBlock | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FlowLoadError(f"'supervisor' must be a mapping, got {type(raw).__name__}")
    _reject_unknown(raw, _SUPERVISOR_FIELDS, "supervisor")
    return SupervisorBlock(
        role_file=raw.get("role_file") or None,
        finalize_role_file=raw.get("finalize_role_file") or None,
        handoff_role_file=raw.get("handoff_role_file") or None,
        emit_follow_ups=bool(raw.get("emit_follow_ups", False)),
        observe=_parse_supervisor_observe(raw.get("observe")),
    )


def _parse_supervisor_observe(raw: Any) -> SupervisorObserveBlock | None:
    """Parse the flow-local ``supervisor.observe`` sub-block (cadence narrowing only).

    Whether the declared mode is *allowed* under the operator's global mode is the config-aware
    validator's call — this layer is config-free and only proves the value is a real mode.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FlowLoadError(f"'supervisor.observe' must be a mapping, got {type(raw).__name__}")
    _reject_unknown(raw, _SUPERVISOR_OBSERVE_FIELDS, "supervisor.observe")
    mode = raw.get("mode")
    if mode is None:
        return SupervisorObserveBlock()
    return SupervisorObserveBlock(mode=_enum(ObserveMode, mode, "supervisor.observe"))


def _parse_defaults(raw: Any) -> FlowDefaults:
    if raw is None:
        return FlowDefaults()
    if not isinstance(raw, dict):
        raise FlowLoadError(f"'defaults' must be a mapping, got {type(raw).__name__}")
    _reject_unknown(raw, _DEFAULTS_FIELDS, "defaults")
    ev_raw = raw.get("evaluator")
    if ev_raw is None:
        return FlowDefaults()
    if not isinstance(ev_raw, dict):
        raise FlowLoadError(f"'defaults.evaluator' must be a mapping, got {type(ev_raw).__name__}")
    _reject_unknown(ev_raw, _EVALUATOR_DEFAULTS_FIELDS, "defaults.evaluator")
    return FlowDefaults(
        evaluator=EvaluatorDefaults(
            session_scope=_enum(
                SessionScope,
                ev_raw.get("session_scope", SessionScope.FRESH_DISPOSABLE),
                "defaults.evaluator",
            ),
            permission_profile=_enum(
                PermissionProfile,
                ev_raw.get("permission_profile", PermissionProfile.READ_ONLY),
                "defaults.evaluator",
            ),
            max_rework_per_stage=int(ev_raw.get("max_rework_per_stage", 1)),
            gate_severity=_parse_gate_severity(
                ev_raw.get("gate_severity", DEFAULT_GATE_SEVERITY), "defaults.evaluator"
            ),
        )
    )


def _parse_flow_doc(raw: dict[str, Any], source: str) -> FlowDoc:
    ctx = f"flow in {source}"
    _reject_unknown(raw, _FLOW_FIELDS, ctx)
    name = str(_require(raw, "name", ctx))
    task_type = str(_require(raw, "task_type", ctx))
    ceiling = str(_require(raw, "permission_ceiling", ctx))
    out_pol = str(_require(raw, "output_policy", ctx))
    publishing = str(_require(raw, "publishing", ctx))

    defaults = _parse_defaults(raw.get("defaults"))

    nodes_raw = _require(raw, "nodes", ctx)
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise FlowLoadError(f"'nodes' must be a non-empty list in {ctx}")
    nodes = tuple(_parse_node(n, defaults) for n in nodes_raw)

    edges_raw = raw.get("edges") or []
    edges = tuple(_parse_edge(e) for e in edges_raw)

    budgets_raw: dict[str, Any] = raw.get("budgets") or {}

    np_raw = raw.get("network_policy")
    network_policy = _enum(NetworkPolicy, np_raw, ctx) if np_raw is not None else None

    return FlowDoc(
        name=name,
        task_type=task_type,
        permission_ceiling=_enum(PermissionProfile, ceiling, ctx),
        output_policy=_enum(OutputPolicy, out_pol, ctx),
        publishing=_enum(PublishingPolicy, publishing, ctx),
        nodes=nodes,
        edges=edges,
        budgets=MappingProxyType({str(k): int(v) for k, v in budgets_raw.items()}),
        network_policy=network_policy,
        decomposition=_parse_decomposition(raw.get("decomposition")),
        supervisor=_parse_supervisor(raw.get("supervisor")),
    )
