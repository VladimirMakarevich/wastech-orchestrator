"""Flow snapshot — loader and resolver for flow YAML files (P0.2).

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

from wastech_orchestrator.core.flow.contracts import (
    EvaluationKind,
    NetworkPolicy,
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    SessionScope,
    fingerprint,
)
from wastech_orchestrator.core.flow.schema import (
    AgentNode,
    ChecksDiscovery,
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
    WhenPredicate,
)


class FlowLoadError(Exception):
    """Raised when a flow YAML cannot be parsed or resolved into a snapshot."""


# -- fail-closed field allowlists ---------------------------------------------
#
# Every mapping in the flow document is checked against an explicit allowlist: an unknown key is a
# fatal load error, never silently ignored. This is the structural ``additionalProperties: false``
# gate from the co-design ``flow.schema.json`` and the field-allowlist requirement in
# ``security-ceiling.md`` §3-§4 — the mechanism that keeps operator YAML a closed allowlist rather
# than an open dict.

_FLOW_FIELDS = frozenset({
    "name", "task_type", "permission_ceiling", "output_policy", "publishing",
    "network_policy", "defaults", "nodes", "edges", "budgets", "decomposition",
})
_AGENT_FIELDS = frozenset({
    "id", "kind", "role_file", "session_scope", "lineage_affinity", "permission_profile",
    "model", "reasoning", "timeout_seconds", "output_schema", "output_artifact", "hitl",
    "extra_args", "when",
})
_EVALUATOR_FIELDS = frozenset({
    "id", "kind", "role", "role_file", "session_scope", "permission_profile",
    "evaluation_kind", "blocking", "max_rework_per_stage", "model", "reasoning", "when",
})
_CHECKS_FIELDS = frozenset({"id", "kind", "checker", "discovery", "when"})
_HITL_NODE_FIELDS = frozenset({"id", "kind", "signal", "timeout_s", "when"})
_PUBLISH_FIELDS = frozenset({"id", "kind", "policy", "when"})
_EDGE_FIELDS = frozenset({"from", "to", "outcome", "budget", "loop"})
_WHEN_FIELDS = frozenset({"fact", "equals"})
_HITL_SETTINGS_FIELDS = frozenset({"allow_question", "allow_approval"})
_DISCOVERY_FIELDS = frozenset({"mode", "approve_command_changes"})
_DECOMPOSITION_FIELDS = frozenset({
    "proposed_by", "sub_flow", "gate", "commit_each_subtask", "shared_budget",
})
_GATE_FIELDS = frozenset({"min", "max", "linear_depends_on"})
_DEFAULTS_FIELDS = frozenset({"evaluator"})
_EVALUATOR_DEFAULTS_FIELDS = frozenset({
    "session_scope", "permission_profile", "max_rework_per_stage",
})

# Core checker set (security-ceiling §3): flow may not invent a checker kind.
_CHECKER_KINDS = frozenset({"command_profile", "citation", "dependency_scan"})

# Output-artifact slots (P1.4): the well-known names an agent node may persist its output to. The
# slot vocabulary is core-fixed (a flow may not invent a slot — fail-closed at load).
_OUTPUT_ARTIFACT_SLOTS = frozenset({"enriched_spec", "plan", "summary"})

# ``when`` fact namespaces (co-design notes #2/#63). The exact value allowlist per namespace is
# finalized when the P1 engine fact resolver lands; here we fail-closed on the namespace prefix so
# a bare/typo'd fact (e.g. ``summary_enabled`` with no namespace) is rejected at load time.
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


def _parse_checks_discovery(raw: Any) -> ChecksDiscovery | None:
    if raw is None:
        return None
    _reject_unknown(raw, _DISCOVERY_FIELDS, "checks discovery")
    return ChecksDiscovery(
        mode=str(raw.get("mode", "auto")),  # type: ignore[arg-type]
        approve_command_changes=bool(raw.get("approve_command_changes", False)),
    )


def _parse_agent_node(raw: dict[str, Any]) -> AgentNode:
    nid = str(_require(raw, "id", "agent node"))
    ctx = f"agent node '{nid}'"
    _reject_unknown(raw, _AGENT_FIELDS, ctx)
    role_file = str(_require(raw, "role_file", ctx))

    pp_raw = raw.get("permission_profile")
    permission_profile = _enum(PermissionProfile, pp_raw, ctx) if pp_raw is not None else None

    ss_raw = raw.get("session_scope", SessionScope.FRESH_DISPOSABLE)

    os_raw = raw.get("output_schema")
    output_schema: str | None = (
        json.dumps(os_raw, sort_keys=True) if os_raw is not None else None
    )

    output_artifact = raw.get("output_artifact") or None
    if output_artifact is not None and output_artifact not in _OUTPUT_ARTIFACT_SLOTS:
        raise FlowLoadError(
            f"invalid output_artifact {output_artifact!r} in {ctx}; "
            f"valid slots: {sorted(_OUTPUT_ARTIFACT_SLOTS)}"
        )

    return AgentNode(
        id=nid,
        kind="agent",
        role_file=role_file,
        session_scope=_enum(SessionScope, ss_raw, ctx),
        lineage_affinity=raw.get("lineage_affinity") or None,
        permission_profile=permission_profile,
        model=raw.get("model") or None,
        reasoning=raw.get("reasoning") or None,
        timeout_seconds=raw.get("timeout_seconds"),
        output_schema=output_schema,
        output_artifact=output_artifact,
        hitl=_parse_hitl_settings(raw.get("hitl")),
        extra_args=tuple(str(a) for a in raw.get("extra_args", [])),
        when=_parse_when(raw.get("when")),
    )


def _parse_evaluator_node(raw: dict[str, Any], defaults: EvaluatorDefaults) -> EvaluatorNode:
    nid = str(_require(raw, "id", "evaluator node"))
    ctx = f"evaluator node '{nid}'"
    _reject_unknown(raw, _EVALUATOR_FIELDS, ctx)
    role = str(_require(raw, "role", ctx))
    role_file = str(_require(raw, "role_file", ctx))

    ss_raw = raw.get("session_scope", defaults.session_scope)
    pp_raw = raw.get("permission_profile", defaults.permission_profile)
    ek_raw = raw.get("evaluation_kind", EvaluationKind.STAGE_OUTPUT)

    return EvaluatorNode(
        id=nid,
        kind="evaluator",
        role=role,
        role_file=role_file,
        session_scope=_enum(SessionScope, ss_raw, ctx),
        permission_profile=_enum(PermissionProfile, pp_raw, ctx),
        evaluation_kind=_enum(EvaluationKind, ek_raw, ctx),
        blocking=bool(raw.get("blocking", True)),
        max_rework_per_stage=int(raw.get("max_rework_per_stage", defaults.max_rework_per_stage)),
        model=raw.get("model") or None,
        reasoning=raw.get("reasoning") or None,
        when=_parse_when(raw.get("when")),
    )


def _parse_checks_node(raw: dict[str, Any]) -> ChecksNode:
    nid = str(_require(raw, "id", "checks node"))
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
        discovery=_parse_checks_discovery(raw.get("discovery")),
        when=_parse_when(raw.get("when")),
    )


def _parse_hitl_node(raw: dict[str, Any]) -> HitlNode:
    nid = str(_require(raw, "id", "hitl node"))
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
    gate: dict[str, Any] = raw.get("gate") or {}
    _reject_unknown(gate, _GATE_FIELDS, "decomposition.gate")
    return DecompositionConfig(
        proposed_by=proposed_by,
        sub_flow=tuple(str(s) for s in sub_flow_raw),
        gate_min=gate.get("min"),
        gate_max=gate.get("max"),
        linear_depends_on=bool(gate.get("linear_depends_on", False)),
        commit_each_subtask=bool(raw.get("commit_each_subtask", False)),
        shared_budget=raw.get("shared_budget") or None,
    )


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
    )
