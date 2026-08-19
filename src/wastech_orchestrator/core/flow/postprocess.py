"""Data-driven per-node post-processing — mechanics the engine post-node hook runs.

Two mechanisms, both triggered by **declared data**, never a stage name:

* :func:`apply_output_artifact` — when an agent node declares ``output_artifact: <slot>``, write
  the agent's output to that slot file, register it, and thread the path into the downstream
  :class:`NodeInputs` field the slot names (``plan_path``, read by the next node's ``{plan_path}``;
  ``summary_body_path``, read by the ``publish`` node for the PR body — not a prompt variable). The
  slot vocabulary is core-fixed (validated at load); the flow only picks *which* node fills it.
* :func:`read_decomposition` — read the ``decompose`` / ``subtasks`` contract off the
  ``decomposition.proposed_by`` node's output and apply the deterministic gate. The agent
  *recommends*; the core decides (no flow weakens ``max_subtasks`` or the linear-dependency rule).

Materializing the decision (persisting it, writing subtask specs, the fan-out) belongs with the
decomposition driver (slice 5), which calls :func:`read_decomposition` then orchestrates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.core.decomposition import DecompositionDecision, decide_decomposition
from wastech_orchestrator.core.flow.engine import NodeOutcome
from wastech_orchestrator.core.flow.nodes.base import NodeInputs, RegisterArtifact
from wastech_orchestrator.core.flow.nodes.exchange_publish import (
    publish_artifact,
    publish_node_run_file,
)
from wastech_orchestrator.core.flow.output_policy import is_within
from wastech_orchestrator.core.flow.schema import AgentNode
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
from wastech_orchestrator.providers.redaction import redact_text


@dataclass(frozen=True)
class _Slot:
    """A well-known output slot: where the agent output lands and how it flows downstream."""

    filename: str
    artifact_kind: str
    #: the :class:`NodeInputs` attribute to point at the slot file, or ``None`` for an audit-only
    #: slot not fed to any prompt (the enriched spec has no ``{...}`` variable).
    inputs_field: str | None
    #: whether this slot is agent-facing and its ``inputs_field`` must resolve to the redacted
    #: exchange copy. Only ``plan`` feeds a downstream provider path; ``enriched_spec`` is
    #: audit-only and ``summary`` is an orchestrator publish input — both stay private.
    exchange: bool = False
    #: whether this slot is written into the flow's private ``output_policy`` report directory
    #: instead of the task artifact dir. The ``report`` slot migrates the security_audit
    #: node off any agent-written report contract: the agent now
    #: returns the report as structured output and the orchestrator writes it here privately.
    report: bool = False
    #: whether the slot file itself is redaction-scrubbed. Deliberately separate from ``report``,
    #: which only says WHERE the file goes: conflating the two is what left ``summary`` raw — the
    #: one slot whose content leaves the machine, read verbatim into the pull-request body and
    #: committed as an audit artifact. The exchange copy a slot may publish is redacted by
    #: :func:`publish_artifact` regardless, which is why ``plan`` needs no flag here: its private
    #: file is the audit record and never leaves.
    redact: bool = False


#: Core-fixed slot table: where each node's ``output_artifact`` lands. The set of keys equals the
#: loader allowlist ``snapshot._OUTPUT_ARTIFACT_SLOTS``.
OUTPUT_SLOTS: dict[str, _Slot] = {
    "enriched_spec": _Slot("task.enriched.md", "enriched", None),
    "plan": _Slot("plan.md", "plan", "plan_path", exchange=True),
    "summary": _Slot("summary.md", "summary_md", "summary_body_path", redact=True),
    "report": _Slot("report.md", "report", None, report=True, redact=True),
}


def apply_output_artifact(
    node: AgentNode,
    outcome: NodeOutcome,
    *,
    artifacts_root: str | Path,
    task_id: str,
    inputs: NodeInputs,
    register: RegisterArtifact,
    exchange_root: str = "",
    extra_secrets: Iterable[str] = (),
    report_dir: Path | None = None,
) -> str | None:
    """Persist an ``output_artifact`` slot from the agent output; return its private path or None.

    No-op (returns ``None``) when the node declares no slot. The content is the agent's
    ``structured_output["content"]`` (refinement/planning typed output) or, absent that, its
    ``final_message`` (the free-form summary agent), via :func:`_slot_content`. An agent-facing slot
    (``plan``) also publishes a redacted copy to the exchange and points its ``inputs_field`` at it;
    a ``report`` slot is written into the flow's private ``report_dir`` instead of the task artifact
    dir. The private slot file stays the audit record.

    A slot whose :attr:`_Slot.redact` is set is scrubbed on the way to disk — ``report`` and
    ``summary``. ``summary`` is the one that leaves the machine: it is read verbatim into the
    pull-request body and committed as an audit artifact, and it used to be written raw, so an agent
    could place arbitrary text — including a secret that had reached its own output — into a
    published document without going near anything the sandbox guards. Advanced mode, where the
    whole parent environment reaches the agent, only widens what could land there.
    """
    slot_name = node.output_artifact
    if slot_name is None:
        return None
    slot = OUTPUT_SLOTS[slot_name]  # key validated at load (snapshot._OUTPUT_ARTIFACT_SLOTS)
    content = _slot_content(outcome)
    if not content:
        return None  # nothing to persist (e.g. a best-effort node that failed) — fallback applies
    if slot.report:
        if report_dir is None:  # defensive: a report slot with no report output_policy
            return None
        target_dir = report_dir
    else:
        target_dir = task_artifact_dir(artifacts_root, task_id)
    body = redact_text(content, extra_secrets=extra_secrets) if slot.redact else content
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / slot.filename
    path.write_text(body, encoding="utf-8")
    register(task_id, slot.artifact_kind, str(path))
    if slot.inputs_field is not None:
        field_path = str(path)
        if slot.exchange:
            field_path = publish_artifact(
                exchange_root,
                task_id,
                slot.filename,
                content,
                extra_secrets=extra_secrets,
                private_path=str(path),
            )
        setattr(inputs, slot.inputs_field, field_path)
    return str(path)


def write_node_output(
    node: AgentNode,
    outcome: NodeOutcome,
    *,
    artifacts_root: str | Path,
    task_id: str,
    node_run_id: int,
    register: RegisterArtifact,
    extra_secrets: Iterable[str] = (),
    exchange_root: str = "",
    produced_dir: Path | None = None,
    warn: Callable[[str], None] | None = None,
) -> str | None:
    """Persist a node's output to ``<artifacts>/<node_id>.out.md``; return the path or ``None``.

    The generic node-output channel: every agent node's output is written and
    exposed downstream as ``{<node_id>_path}`` (a *path*, never inlined content). The content is the
    same as a slot (``structured_output["content"]`` or ``final_message``, via ``_slot_content``) —
    unless the node declares ``output_file``, in which case the file it produced under
    ``produced_dir`` is the content (:func:`_produced_content`). Either way it is
    **redaction-scrubbed** before writing — a node's raw output can echo a secret, and unlike
    ``final_message`` (redacted by the adapter) ``structured_output`` is not. Local/uncommitted, and
    registered as an artifact so it doubles as a per-node audit record.

    No-op (returns ``None``) when:

    * the node fills one of the :data:`OUTPUT_SLOTS` via ``output_artifact`` — that slot *is* its
      output channel (``{plan_path}`` etc.), so no duplicate ``.out.md`` is written (one node = one
      output); or
    * there is no content to persist (a best-effort node that produced nothing).
    """
    if node.output_artifact is not None:
        return None  # special-slot node: its slot is the channel, no duplicate generic output
    content = _produced_content(node, produced_dir, warn) or _slot_content(outcome)
    if not content:
        return None
    # Per-run dir keyed by the reserved node_run_id: a node that re-runs in a loop keeps every
    # pass's output on disk. The downstream {<node_id>_path} channel resolves the latest run.
    run_dir = node_run_dir(artifacts_root, task_id, node.id, node_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{node.id}.out.md"
    redacted = redact_text(content, extra_secrets=extra_secrets)
    path.write_text(redacted, encoding="utf-8")
    register(task_id, "node_output", str(path))
    # The private .out.md stays the audit record; the redacted exchange copy is what the downstream
    # {<node_id>_path} fan-in resolves.
    publish_node_run_file(
        exchange_root,
        task_id,
        node.id,
        node_run_id,
        f"{node.id}.out.md",
        redacted,
        extra_secrets=extra_secrets,
        private_path=str(path),
    )
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


def _produced_content(
    node: AgentNode, produced_dir: Path | None, warn: Callable[[str], None] | None
) -> str:
    """The declared produced file's text, or ``""`` to fall back to the node's own message.

    A node whose real product is a written document used to publish only its closing summary of it,
    so the artifact stopped at the node and the next one worked from a pointer thinner than the
    thing it pointed at. ``output_file`` names that document; here it is read back so the file
    itself is what crosses the edge.

    Falls back (and says so, once, on the operator log) when the declared file did not appear, is
    not a regular file, is empty, or is not text: losing the channel entirely would be worse than
    carrying the message. The filename is validated at load and joined onto a directory the
    orchestrator resolved, and the join is re-checked here — the agent supplies no part of the path.
    """
    if node.output_file is None or produced_dir is None:
        return ""
    path = produced_dir / node.output_file
    reason = ""
    if not is_within(produced_dir, path) or not path.is_file():
        reason = "it was not produced"
    else:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            reason = f"it could not be read as text ({type(exc).__name__})"
        else:
            # An empty file is "not produced" as far as the handoff goes, and earns the same
            # warning: a declared channel that silently carries nothing is the worst outcome here.
            reason = "" if content else "it is empty"
            if content:
                return content
    if warn is not None:
        warn(
            f"node {node.id!r} declares output_file {node.output_file!r} but {reason} — "
            "downstream nodes get this node's closing message instead of the file"
        )
    return ""
