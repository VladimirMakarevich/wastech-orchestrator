"""Data-driven per-node post-processing (P1.4) — mechanics the engine post-node hook runs.

Two mechanisms, both triggered by **declared data**, never a stage name:

* :func:`apply_output_artifact` — when an agent node declares ``output_artifact: <slot>``, write
  the agent's output to that slot file, register it, and thread the path into the downstream
  :class:`NodeInputs` (so the next node gets ``{plan_path}`` / ``{summary_body_path}``). The slot
  vocabulary is core-fixed (validated at load); the flow only picks *which* node fills it.
* :func:`read_decomposition` — read the ``decompose`` / ``subtasks`` contract off the
  ``decomposition.proposed_by`` node's output and apply the deterministic gate. The agent
  *recommends*; the core decides (no flow weakens ``max_subtasks`` or the linear-dependency rule).

Materializing the decision (persisting it, writing subtask specs, the fan-out) belongs with the
decomposition driver (slice 5), which calls :func:`read_decomposition` then orchestrates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.core.decomposition import DecompositionDecision, decide_decomposition
from wastech_orchestrator.core.flow.engine import NodeOutcome
from wastech_orchestrator.core.flow.nodes.base import NodeInputs, RegisterArtifact
from wastech_orchestrator.core.flow.schema import AgentNode
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.redaction import redact_text


@dataclass(frozen=True)
class _Slot:
    """A well-known output slot: where the agent output lands and how it flows downstream."""

    filename: str
    artifact_kind: str
    #: the :class:`NodeInputs` attribute to point at the slot file, or ``None`` for an audit-only
    #: slot not fed to any prompt (the enriched spec has no ``{...}`` variable).
    inputs_field: str | None


#: Core-fixed slot table: where each node's ``output_artifact`` lands. The set of keys equals the
#: loader allowlist ``snapshot._OUTPUT_ARTIFACT_SLOTS``.
OUTPUT_SLOTS: dict[str, _Slot] = {
    "enriched_spec": _Slot("task.enriched.md", "enriched", None),
    "plan": _Slot("plan.md", "plan", "plan_path"),
    "summary": _Slot("summary.md", "summary_md", "summary_body_path"),
}


def apply_output_artifact(
    node: AgentNode,
    outcome: NodeOutcome,
    *,
    artifacts_root: str | Path,
    task_id: str,
    inputs: NodeInputs,
    register: RegisterArtifact,
) -> str | None:
    """Persist an ``output_artifact`` slot from the agent output; return its path or ``None``.

    No-op (returns ``None``) when the node declares no slot. The content is the agent's
    ``structured_output["content"]`` (refinement/planning typed output) or, absent that, its
    ``final_message`` (the free-form summary agent) — see :func:`_slot_content`.
    """
    slot_name = node.output_artifact
    if slot_name is None:
        return None
    slot = OUTPUT_SLOTS[slot_name]  # key validated at load (snapshot._OUTPUT_ARTIFACT_SLOTS)
    content = _slot_content(outcome)
    if not content:
        return None  # nothing to persist (e.g. a best-effort node that failed) — fallback applies
    task_dir = task_artifact_dir(artifacts_root, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / slot.filename
    path.write_text(content, encoding="utf-8")
    register(task_id, slot.artifact_kind, str(path))
    if slot.inputs_field is not None:
        setattr(inputs, slot.inputs_field, str(path))
    return str(path)


def write_node_output(
    node: AgentNode,
    outcome: NodeOutcome,
    *,
    artifacts_root: str | Path,
    task_id: str,
    register: RegisterArtifact,
    extra_secrets: Iterable[str] = (),
) -> str | None:
    """Persist a node's output to ``<artifacts>/<node_id>.out.md``; return the path or ``None``.

    The generic node-output channel (node-output ADR): every agent node's output is written and
    exposed downstream as ``{<node_id>_path}`` (a *path*, never inlined content). The content is the
    same as a slot (``structured_output["content"]`` or ``final_message``, via ``_slot_content``);
    it is **redaction-scrubbed** before writing — a node's raw output can echo a secret, and unlike
    ``final_message`` (redacted by the adapter) ``structured_output`` is not. Local/uncommitted, and
    registered as an artifact so it doubles as a per-node audit record.

    No-op (returns ``None``) when:

    * the node fills one of the three special slots via ``output_artifact`` — that slot *is* its
      output channel (``{plan_path}`` etc.), so no duplicate ``.out.md`` is written (one node = one
      output); or
    * there is no content to persist (a best-effort node that produced nothing).
    """
    if node.output_artifact is not None:
        return None  # special-slot node: its slot is the channel, no duplicate generic output
    content = _slot_content(outcome)
    if not content:
        return None
    task_dir = task_artifact_dir(artifacts_root, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / f"{node.id}.out.md"
    path.write_text(redact_text(content, extra_secrets=extra_secrets), encoding="utf-8")
    register(task_id, "node_output", str(path))
    return str(path)


def read_decomposition(
    outcome: NodeOutcome, *, gate_on: bool, max_subtasks: int
) -> DecompositionDecision:
    """Decide decomposition from the proposed_by node's contract (``decompose``/``subtasks``).

    A thin, flow-neutral wrapper over :func:`decide_decomposition` reading the contract off the
    node's ``structured_output`` — the engine never inspects these fields; the driver calls this.
    """
    return decide_decomposition(
        outcome.structured_output, gate_on=gate_on, max_subtasks=max_subtasks
    )


def _slot_content(outcome: NodeOutcome) -> str:
    structured = outcome.structured_output
    if isinstance(structured, Mapping):
        content = structured.get("content")
        if isinstance(content, str):
            return content
    return outcome.final_message or ""
