"""Fatal load-time flow validator (P0.3).

Two layers run in sequence; the first violation in either layer is not fatal alone — all violations
are collected and reported together so the operator can fix everything in one pass:

  1. **Graph integrity** — edges resolve; outcome ⊆ allowed per kind; every ``rework``/``fail``
     edge has a ``budget`` or ``loop``; named loops declared in ``budgets``; exactly one entry;
     all nodes reachable; at least one terminal; ``lineage_affinity`` target valid; decomposition
     references valid.
  2. **Security ceiling** — evaluator always ``read-only`` and never ``editing_lineage``; every
     agent ``permission_profile`` ≤ ``permission_ceiling``; ``extra_args`` pass
     :func:`~wastech_orchestrator.security.forbidden_args.find_forbidden_args`; ``role_file``
     paths contain no traversal (``..`` or absolute).

Call :func:`validate_flow` immediately after :func:`~.snapshot.load_flow` — before branch
creation and before any provider launch. Both functions together form the fatal gate described in
``docs/backlog/flows/security-ceiling.md §4``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wastech_orchestrator.core.flow.contracts import (
    EvaluationKind,
    PermissionProfile,
    SessionScope,
)
from wastech_orchestrator.core.flow.schema import AgentNode, EvaluatorNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.security.forbidden_args import find_forbidden_args
from wastech_orchestrator.security.profiles import is_same_or_stricter


@dataclass(frozen=True, slots=True)
class Violation:
    """A single validator finding."""

    category: Literal["graph", "ceiling"]
    message: str


class FlowValidationError(Exception):
    """Raised by :func:`validate_flow` when one or more violations are found.

    All violations are collected before raising so the operator sees every problem at once.
    """

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        lines = "\n".join(f"  [{v.category}] {v.message}" for v in violations)
        super().__init__(f"flow validation failed ({len(violations)} violation(s)):\n{lines}")


def validate_flow(snapshot: FlowSnapshot) -> None:
    """Validate a resolved flow snapshot.

    :raises FlowValidationError: if any graph or ceiling violation is found.
    """
    violations = _check_graph(snapshot) + _check_ceiling(snapshot)
    if violations:
        raise FlowValidationError(violations)


# -- allowed outcome sets per node kind ---------------------------------------
#
# Evaluators emit a structured verdict; checks emit pass/fail; all other node
# kinds proceed unconditionally (no outcome on their outgoing edges).
# ``route:<name>`` is always allowed (explicit routing in any flow).

_EVAL_STAGE_OUTCOMES: frozenset[str | None] = frozenset({"accept", "rework"})
_EVAL_FINAL_OUTCOMES: frozenset[str | None] = frozenset({None})  # unconditional only
_CHECKS_OUTCOMES: frozenset[str | None] = frozenset({"pass", "fail"})
_UNCONDITIONAL: frozenset[str | None] = frozenset({None})


# -- graph integrity ----------------------------------------------------------


def _check_graph(snap: FlowSnapshot) -> list[Violation]:
    def g(msg: str) -> Violation:
        return Violation("graph", msg)

    errs: list[Violation] = []
    doc = snap.doc

    # 1. Edge resolution: from/to must reference existing nodes.
    for edge in doc.edges:
        if edge.from_node not in snap.nodes_by_id:
            errs.append(g(f"edge references unknown source node: {edge.from_node!r}"))
        if edge.to not in snap.nodes_by_id:
            errs.append(g(f"edge references unknown target node: {edge.to!r}"))

    # 2. Outcome subset: the outcome on each outgoing edge must be in the allowed set for
    #    that node kind.  ``route:*`` is always permitted (explicit routing override).
    for node_id, edges in snap.adjacency.items():
        node = snap.nodes_by_id.get(node_id)
        if node is None:
            continue
        if node.kind == "evaluator":
            assert isinstance(node, EvaluatorNode)
            allowed: frozenset[str | None] = (
                _EVAL_FINAL_OUTCOMES
                if node.evaluation_kind == EvaluationKind.FINAL_HANDOFF
                else _EVAL_STAGE_OUTCOMES
            )
        elif node.kind == "checks":
            allowed = _CHECKS_OUTCOMES
        else:
            allowed = _UNCONDITIONAL
        for edge in edges:
            oc = edge.outcome
            if oc is not None and oc.startswith("route:"):
                continue
            if oc not in allowed:
                allowed_str = sorted(repr(a) for a in allowed)
                errs.append(g(
                    f"node {node_id!r} ({node.kind}): outcome {oc!r} not in allowed {allowed_str}"
                ))

    # 3. Bounded loops: every rework/fail edge must carry budget or loop.
    for edge in doc.edges:
        if edge.outcome in ("rework", "fail") and edge.budget is None and edge.loop is None:
            errs.append(g(
                f"edge {edge.from_node!r}->{edge.to!r} (outcome={edge.outcome!r}): "
                "unbounded; must declare budget or loop"
            ))

    # 4. Named loops must be declared in budgets.
    for edge in doc.edges:
        if edge.loop is not None and edge.loop not in doc.budgets:
            errs.append(g(f"edge loop {edge.loop!r} not declared in budgets"))

    # 5. Exactly one entry node (zero incoming edges) + full reachability from it.
    incoming: dict[str, int] = {n.id: 0 for n in doc.nodes}
    for edge in doc.edges:
        if edge.to in incoming:
            incoming[edge.to] += 1
    entries = [nid for nid, cnt in incoming.items() if cnt == 0]
    if len(entries) != 1:
        errs.append(g(f"expected exactly one entry node, got {sorted(entries)}"))
    else:
        adj: dict[str, list[str]] = {}
        for edge in doc.edges:
            adj.setdefault(edge.from_node, []).append(edge.to)
        seen: set[str] = set()
        stack = [entries[0]]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(adj.get(x, []))
        unreached = set(snap.nodes_by_id) - seen
        if unreached:
            errs.append(g(f"unreachable nodes: {sorted(unreached)}"))

    # 6. At least one terminal node (no outgoing edges).
    has_outgoing = {edge.from_node for edge in doc.edges}
    if not (set(snap.nodes_by_id) - has_outgoing):
        errs.append(g("no terminal node (every node has at least one outgoing edge)"))

    # 7. lineage_affinity must reference an agent with editing_lineage session scope.
    for node in doc.nodes:
        if not isinstance(node, AgentNode) or node.lineage_affinity is None:
            continue
        target = snap.nodes_by_id.get(node.lineage_affinity)
        if target is None:
            errs.append(g(
                f"node {node.id!r}: lineage_affinity {node.lineage_affinity!r} not found"
            ))
        elif (
            not isinstance(target, AgentNode)
            or target.session_scope != SessionScope.EDITING_LINEAGE
        ):
            errs.append(g(
                f"node {node.id!r}: lineage_affinity {node.lineage_affinity!r} must be "
                "an agent with session_scope=editing_lineage"
            ))

    # 8. Decomposition references must resolve.
    dec = doc.decomposition
    if dec is not None:
        if dec.proposed_by not in snap.nodes_by_id:
            errs.append(g(f"decomposition.proposed_by {dec.proposed_by!r} not found"))
        for nid in dec.sub_flow:
            if nid not in snap.nodes_by_id:
                errs.append(g(f"decomposition.sub_flow {nid!r} not found"))
        if dec.shared_budget is not None and dec.shared_budget not in doc.budgets:
            errs.append(g(f"decomposition.shared_budget {dec.shared_budget!r} not in budgets"))

    return errs


# -- security ceiling ---------------------------------------------------------


def _check_ceiling(snap: FlowSnapshot) -> list[Violation]:
    def c(msg: str) -> Violation:
        return Violation("ceiling", msg)

    errs: list[Violation] = []
    doc = snap.doc
    ceiling = doc.permission_ceiling

    for node in doc.nodes:
        if isinstance(node, EvaluatorNode):
            # Evaluator is always read-only and must never inherit the author's editing context.
            if node.permission_profile != PermissionProfile.READ_ONLY:
                errs.append(c(
                    f"evaluator {node.id!r}: permission_profile must be read-only, "
                    f"got {node.permission_profile.value!r}"
                ))
            if node.session_scope == SessionScope.EDITING_LINEAGE:
                errs.append(c(
                    f"evaluator {node.id!r}: session_scope editing_lineage is forbidden "
                    "(evaluator must not inherit the author workspace context)"
                ))
            _check_path(node.id, node.role_file, errs)

        if isinstance(node, AgentNode):
            if node.permission_profile is not None and not is_same_or_stricter(
                node.permission_profile.value, ceiling.value
            ):
                errs.append(c(
                    f"agent {node.id!r}: permission_profile "
                    f"{node.permission_profile.value!r} exceeds "
                    f"permission_ceiling {ceiling.value!r}"
                ))
            if node.extra_args:
                for reason in find_forbidden_args(list(node.extra_args)):
                    errs.append(c(f"agent {node.id!r}: extra_args {reason}"))
            _check_path(node.id, node.role_file, errs)

    return errs


def _check_path(node_id: str, path: str, errs: list[Violation]) -> None:
    parts = path.replace("\\", "/").split("/")
    if ".." in parts or path.startswith("/"):
        errs.append(Violation(
            "ceiling", f"node {node_id!r}: role_file {path!r} contains path traversal"
        ))
