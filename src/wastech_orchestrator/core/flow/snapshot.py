"""Flow snapshot — loader and resolver for flow YAML files (P0.2).

``load_flow(path)`` reads a YAML file, applies ``defaults``, builds lookup
tables, and returns an immutable :class:`FlowSnapshot`. The ``flow_fingerprint``
is the SHA-256 over the raw ``flow:`` dict (key-order independent), so identical
content always produces the same fingerprint regardless of YAML key ordering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from wastech_orchestrator.core.flow.contracts import (
    EvaluationKind,
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
    fact = _require(raw, "fact", "when")
    return WhenPredicate(fact=str(fact), equals=bool(raw.get("equals", True)))


def _parse_hitl_settings(raw: Any) -> HitlSettings | None:
    if raw is None:
        return None
    return HitlSettings(
        allow_question=bool(raw.get("allow_question", False)),
        allow_approval=bool(raw.get("allow_approval", False)),
    )


def _parse_checks_discovery(raw: Any) -> ChecksDiscovery | None:
    if raw is None:
        return None
    return ChecksDiscovery(
        mode=str(raw.get("mode", "auto")),  # type: ignore[arg-type]
        approve_command_changes=bool(raw.get("approve_command_changes", False)),
    )


def _parse_agent_node(raw: dict[str, Any]) -> AgentNode:
    nid = str(_require(raw, "id", "agent node"))
    ctx = f"agent node '{nid}'"
    role_file = str(_require(raw, "role_file", ctx))

    pp_raw = raw.get("permission_profile")
    permission_profile = PermissionProfile(pp_raw) if pp_raw is not None else None

    ss_raw = raw.get("session_scope", SessionScope.FRESH_DISPOSABLE)

    os_raw = raw.get("output_schema")
    output_schema: str | None = (
        json.dumps(os_raw, sort_keys=True) if os_raw is not None else None
    )

    return AgentNode(
        id=nid,
        kind="agent",
        role_file=role_file,
        session_scope=SessionScope(ss_raw),
        lineage_affinity=raw.get("lineage_affinity") or None,
        permission_profile=permission_profile,
        model=raw.get("model") or None,
        reasoning=raw.get("reasoning") or None,
        timeout_seconds=raw.get("timeout_seconds"),
        output_schema=output_schema,
        hitl=_parse_hitl_settings(raw.get("hitl")),
        extra_args=tuple(str(a) for a in raw.get("extra_args", [])),
        when=_parse_when(raw.get("when")),
    )


def _parse_evaluator_node(raw: dict[str, Any], defaults: EvaluatorDefaults) -> EvaluatorNode:
    nid = str(_require(raw, "id", "evaluator node"))
    ctx = f"evaluator node '{nid}'"
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
        session_scope=SessionScope(ss_raw),
        permission_profile=PermissionProfile(pp_raw),
        evaluation_kind=EvaluationKind(ek_raw),
        blocking=bool(raw.get("blocking", True)),
        max_rework_per_stage=int(raw.get("max_rework_per_stage", defaults.max_rework_per_stage)),
        model=raw.get("model") or None,
        reasoning=raw.get("reasoning") or None,
        when=_parse_when(raw.get("when")),
    )


def _parse_checks_node(raw: dict[str, Any]) -> ChecksNode:
    nid = str(_require(raw, "id", "checks node"))
    checker = str(_require(raw, "checker", f"checks node '{nid}'"))
    return ChecksNode(
        id=nid,
        kind="checks",
        checker=checker,  # type: ignore[arg-type]
        discovery=_parse_checks_discovery(raw.get("discovery")),
        when=_parse_when(raw.get("when")),
    )


def _parse_hitl_node(raw: dict[str, Any]) -> HitlNode:
    nid = str(_require(raw, "id", "hitl node"))
    signal = str(_require(raw, "signal", f"hitl node '{nid}'"))
    return HitlNode(
        id=nid,
        kind="hitl",
        signal=signal,  # type: ignore[arg-type]
        timeout_s=raw.get("timeout_s"),
        when=_parse_when(raw.get("when")),
    )


def _parse_publish_node(raw: dict[str, Any]) -> PublishNode:
    nid = str(_require(raw, "id", "publish node"))
    policy = str(_require(raw, "policy", f"publish node '{nid}'"))
    return PublishNode(
        id=nid,
        kind="publish",
        policy=PublishingPolicy(policy),
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
    proposed_by = str(_require(raw, "proposed_by", "decomposition"))
    sub_flow_raw = _require(raw, "sub_flow", "decomposition")
    gate: dict[str, Any] = raw.get("gate") or {}
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
    if not isinstance(raw, dict):
        return FlowDefaults()
    ev_raw = raw.get("evaluator")
    if not isinstance(ev_raw, dict):
        return FlowDefaults()
    return FlowDefaults(
        evaluator=EvaluatorDefaults(
            session_scope=SessionScope(ev_raw.get("session_scope", SessionScope.FRESH_DISPOSABLE)),
            permission_profile=PermissionProfile(
                ev_raw.get("permission_profile", PermissionProfile.READ_ONLY)
            ),
            max_rework_per_stage=int(ev_raw.get("max_rework_per_stage", 1)),
        )
    )


def _parse_flow_doc(raw: dict[str, Any], source: str) -> FlowDoc:
    ctx = f"flow in {source}"
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

    return FlowDoc(
        name=name,
        task_type=task_type,
        permission_ceiling=PermissionProfile(ceiling),
        output_policy=OutputPolicy(out_pol),
        publishing=PublishingPolicy(publishing),
        nodes=nodes,
        edges=edges,
        budgets=MappingProxyType({str(k): int(v) for k, v in budgets_raw.items()}),
        network_policy=raw.get("network_policy") or None,
        decomposition=_parse_decomposition(raw.get("decomposition")),
    )
