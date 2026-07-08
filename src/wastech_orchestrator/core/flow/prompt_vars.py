"""The flow-derived prompt-variable valid-set (prompt-and-supervisor authoring contract).

The set of ``{name}`` variables an author may reference in a flow's role prompts is **not** a static
frozenset: it is the fixed core allowlist (``ALLOWED_PROMPT_VARS``) *plus* names derived from the
flow graph. This module computes that set once, so two callers agree:

* the validate-time anti-drift lint (``core.flow.validator``) — a token outside the valid-set
  renders verbatim, which it warns about;
* the renderer's effective allowlist (``core.flow.nodes.agent``) — the caller passes this set to
  ``render_prompt``, which stays the fixed security core and substitutes only names in the set it is
  given (every value still a path).

Keeping both on one helper is the whole point of building the lint flow-aware from the start: the
node-output ADR extends :func:`node_output_vars` and both the lint and the renderer follow with no
further change.
"""

from __future__ import annotations

from wastech_orchestrator.core.flow.schema import AgentNode, ToolNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.prompts import ALLOWED_PROMPT_VARS


def node_output_vars(snapshot: FlowSnapshot) -> frozenset[str]:
    """The ``{<node_id>_path}`` names the flow's agent + tool nodes expose (node-output channel).

    Every **agent** node's output is persisted to ``<node_id>.out.md`` and every **tool** node's
    stdout to ``tools/<node_id>/stdout.txt`` (P5); both are addressable downstream as
    ``{<node_id>_path}`` (a path to a Core-written, redacted artifact, never inlined content). Only
    these two kinds get the generic channel — evaluator / checks / human nodes keep their dedicated
    variables (``review_path`` / ``checks_path``).
    """
    return frozenset(
        f"{node.id}_path" for node in snapshot.doc.nodes if isinstance(node, (AgentNode, ToolNode))
    )


def valid_prompt_vars(snapshot: FlowSnapshot) -> frozenset[str]:
    """The full set of prompt-variable names valid for *snapshot*: core allowlist ∪ node-derived.

    This is both the renderer's effective allowlist for a node in this flow and the lint's
    valid-set, so an author who references any name in it is guaranteed a real substitution (or a
    documented may-be-empty value), and anything else is flagged as a verbatim render.
    """
    return ALLOWED_PROMPT_VARS | node_output_vars(snapshot)
