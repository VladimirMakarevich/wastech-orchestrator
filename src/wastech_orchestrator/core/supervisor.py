"""Constant supervisor layer above any flow.

The supervisor is **not** a graph node — it is an orchestrator-level oversight layer that exists for
every task under any flow shape (even a single implement agent with no checks/review). It starts at
task start, lives the whole cycle, observes completed steps read-only through its **own**
``resume_own_lineage`` session (≈one LLM call per observed step, accumulating context across steps),
and at whole-task close synthesizes the ``summary`` + advisory caveats.

Two things bound that cost. The deterministic ``tool`` / ``checks`` nodes are **not** observed —
their result is already a durable fact (``node_runs`` / ``check_runs``), so an LLM note about it
buys nothing. And the whole-task ``finalize`` does not depend on the warm session at all: it runs
on a **fresh** session seeded by the :mod:`~wastech_orchestrator.core.supervisor_packet`
``SupervisorPacket`` — a small deterministic artifact built from durable state and handed over as a
path. So a normal run and a revive follow one reproducible path, and the finalize call's input does
not grow with the run's rework cycles.

It is **advisory by construction**: it never reworks, reopens, or routes. Each observation is
recorded as an immutable ``evaluations`` row (``supervisor_step`` / ``supervisor_final``,
``verdict='advisory'``) and surfaced to the human (the summary becomes the PR body), but the engine
never consumes it to route. Blocking is the job of in-flow ``review`` / ``test_quality`` evaluators.

Configured in ``config.yaml`` (``supervisor: {model, reasoning, role_file}``) under the same ceiling
as flow nodes: ``permission_profile`` is forced ``read-only`` here, ``reasoning`` ∈ the allowlist
(loader), ``role_file`` is path-contained (validator + the renderer's flow-dir containment). The own
``resume_own_lineage`` session is durable: it is persisted to / hydrated from ``node_lineage`` under
a reserved sentinel node id (``state.db`` only), so a resumed task continues the supervisor's
accumulated cross-step context instead of starting blind — gated by provider match, exactly like the
``resume_own_lineage`` evaluator.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from wastech_orchestrator.config.schema import SupervisorConfig
from wastech_orchestrator.core.flow.engine import Finding
from wastech_orchestrator.core.flow.nodes.base import GitPort, RegisterArtifact, RouterPort
from wastech_orchestrator.core.flow.nodes.exchange_publish import publish_artifact
from wastech_orchestrator.core.flow.observability import (
    record_provider_attempts,
    write_prompt_audit,
    write_rendered_prompt,
)
from wastech_orchestrator.core.flow.prompt import RoleFileError, render_role_prompt
from wastech_orchestrator.core.flow.schema import SupervisorBlock
from wastech_orchestrator.core.flow.usage_accounting import (
    deserialize_usage,
    snapshot_for_lineage,
)
from wastech_orchestrator.core.follow_ups import (
    FINDING_TITLE_MAX,
    FollowUp,
    evaluator_finding_follow_ups,
    follow_up_json,
    merge_follow_ups,
    parse_follow_ups,
    render_follow_ups_section,
    render_gate_digest,
)
from wastech_orchestrator.core.supervisor_packet import (
    bound_step_message,
    build_packet_facts,
    render_packet,
)
from wastech_orchestrator.core.supervisor_usage import SupervisorFunction, summarize_spend
from wastech_orchestrator.git_manager import GitControlState
from wastech_orchestrator.memory.delta import DELTA_OUTPUT_SCHEMA, CandidateDelta, parse_delta
from wastech_orchestrator.providers.artifacts import (
    node_run_dir,
    task_artifact_dir,
)
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    NormalizedUsage,
    ProviderId,
    build_effective_prompt,
)
from wastech_orchestrator.providers.exchange import assert_orchestration_paths_contained
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.runtime_layout import ProviderWriteGuardPolicy
from wastech_orchestrator.state_store import (
    CheckRunRow,
    EvaluationRow,
    NodeLineageRow,
    NodeRunRow,
    ProviderAttemptRow,
)

_LOG = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Default wall-clock for the supervisor's ``provider_attempts`` timestamps (UTC ISO)."""
    return datetime.now(UTC).isoformat()


# The supervisor's read-only requests carry a dedicated ``supervisor`` node identity (audit dir /
# route label); it is not a graph node, so it records ``evaluations`` rows, never ``node_runs``.
_SUPERVISOR_IDENTITY = "supervisor"

# The layer's forced permission profile. Named once because two places need the same value: the
# request it launches with, and the per-attempt "does this turn get a shell" question its detection
# bracket asks — a bracket keyed on a different profile than the launch would watch the wrong thing.
_SUPERVISOR_PROFILE = "read-only"

# The reserved ``node_lineage`` key under which the supervisor's own durable session lives. It is a
# double-underscore sentinel, distinct from the routing identity above, so it can never collide with
# a real flow node id (an operator could legally name an evaluator node ``supervisor``).
_SUPERVISOR_LINEAGE_NODE_ID = "__supervisor__"

# The finalize packet's filename, identical in the private artifact dir and in the exchange
# (``.worc-io/<task-id>/supervisor/packet.json``), so the audit copy and the copy the provider read
# are trivially comparable.
_PACKET_FILENAME = "packet.json"

# Base for the subtask-boundary handoff turns' artifact-dir namespace.
# Each handoff uses ``_HANDOFF_RUN_ID_BASE + subtask_order`` so multiple handoffs in one task (a
# chain, or a diamond) write to DISTINCT dirs — ``create_attempt_dir`` forbids overwriting
# (``exist_ok=False``), so a shared id would make the second boundary's turn raise and silently
# degrade to the floor alone. The base is distinct from finalize's ``0`` and far above any real
# (small autoincrement) node-run id.
_HANDOFF_RUN_ID_BASE = 990_000

# The evidence-gated ``follow_ups`` schema. Hardcoded in code — a flow author reshapes the
# supervisor's *wording* via its prompt files, but never the machine contract the orchestrator
# parses. Each record is minimal and grounded: an unsupported "refactor idea" carries no evidence
# and is dropped by :func:`parse_follow_ups`.
# Nullable array root (``["array", "null"]``) + full ``required`` so this validates under OpenAI
# strict mode when nested as the finalize turn's ``follow_ups``: no follow-ups → ``null``.
# ``paths``/``action_hint`` are nullable rather than optional so the model may still omit them under
# that same strict mode; :func:`parse_follow_ups` treats ``null`` identically to an absent key.
# The root ``description`` is load-bearing, not decoration: without it a model with nothing to
# report omits the key (following the prose "leave the array empty"), is rejected for a missing
# required property, and collapses to a few-byte summary that then ships as a PR body. It states in
# the schema what the prompt states in prose, so the two cannot disagree.
_FOLLOW_UPS_SCHEMA: dict[str, Any] = {
    "type": ["array", "null"],
    "description": (
        "ALWAYS present — emit this key on every response. When nothing qualifies, emit an empty "
        "array (or null); never omit the key, and never drop it to shorten the answer."
    ),
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                # Independently written, because a title that is a truncated prefix of its own
                # ``rationale`` makes the operator's queue untriageable without opening every item.
                "description": (
                    "A short imperative label (aim for 80 characters or fewer) naming the action, "
                    "written to be read on its own in a work queue — NOT a prefix, restatement, or "
                    "truncation of `rationale`."
                ),
            },
            "rationale": {"type": "string"},
            "paths": {"type": ["array", "null"], "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "action_hint": {"type": ["string", "null"]},
        },
        "required": ["title", "rationale", "paths", "evidence", "severity", "action_hint"],
    },
}

# The intra-task subtask handoff brief's structured schema. Three
# sections; hardcoded in code (a flow reshapes wording via ``handoff_role_file``, never the
# contract). All optional — a thin boundary may yield only one useful section.
# OpenAI strict mode requires every ``properties`` key in ``required``; the three sections
# stay optional by being nullable, so a thin boundary can yield ``null`` for the empty ones.
# :func:`_render_handoff_brief` skips a ``null``/empty section exactly as before.
_HANDOFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "new_surface_area": {"type": ["string", "null"]},
        "locked_decisions": {"type": ["string", "null"]},
        "open_edges": {"type": ["string", "null"]},
    },
    "required": ["new_surface_area", "locked_decisions", "open_edges"],
}

# Built-in supervisor prompt text — the last fallback in each chain, so a flow with no prompt files
# (and no global config prompt, for finalize/handoff) still runs exactly as before.
_BUILTIN_OBSERVE = "You are a read-only supervisor observing a software task. Do not edit code."
_BUILTIN_FINALIZE = (
    "You are a read-only supervisor closing out a software task. Do not edit code.\n\n"
    "Synthesize a plain-language summary of the whole task: what was done, how it works, how it "
    "integrates, and why, grounded in the actual committed change. In a closing section list any "
    "advisory caveats or follow-ups you noted across the steps.\n\n"
    # The floor is enforced for every flow, so the last fallback in the chain has to state it: a
    # flow with no finalize lens of its own (and every user-authored one) reads only this text.
    "Answer with real prose. A one-line, placeholder or probe summary is discarded as a failed "
    "generation and replaced by a mechanical report of the run, so it costs the whole synthesis."
)
_BUILTIN_HANDOFF = (
    "You are a read-only supervisor briefing the next subtask in a decomposed task. Do not edit "
    "code. You have observed the predecessor subtask(s); write a focused handoff for the agent "
    "about to implement the successor."
)


# The max reasoning tiers make a structured-output turn fragile — at ``xhigh`` the provider
# spends the turn on thinking and fails to emit a valid tool call under the schema (observed:
# ``error_max_structured_output_retries``), while the same free-text turn passes. A target-only
# summary/proposal does not need max reasoning, so schema turns are capped to ``high`` — the free-
# text observe turns keep the configured tier. Deterministic (no reliance on provider error
# classification), and it keeps the default from being brittle out of the box.
_MAX_REASONING_TIERS = frozenset({"xhigh", "max"})
_SCHEMA_REASONING_CAP = "high"


def _schema_safe_reasoning(
    reasoning: str | None, output_schema: dict[str, Any] | None
) -> str | None:
    """Cap a structured-output turn's reasoning to ``high`` when configured at a max tier.

    Free-text turns (``output_schema is None``) keep the configured value unchanged.
    """
    if output_schema is None or reasoning is None:
        return reasoning
    return _SCHEMA_REASONING_CAP if reasoning in _MAX_REASONING_TIERS else reasoning


def _finalize_schema(*, with_delta: bool, with_follow_ups: bool) -> dict[str, Any]:
    """Build the finalize turn's structured schema for the enabled outputs (all on one turn).

    ``summary`` is always required; ``memory_delta`` is added when memory is enabled and
    ``follow_ups`` when the flow opted in (``supervisor.emit_follow_ups``). When neither is enabled
    the caller runs a free-text turn instead, so this is never called."""
    properties: dict[str, Any] = {
        "summary": {
            "type": "string",
            "minLength": 1,
            # Without guidance the model packs the whole synthesis into one flat line (0 `\n`)
            # and the PR body renders as a headless slab. Ask for structured markdown prose.
            "description": (
                "The whole-task summary as Markdown prose: a short lead paragraph, then 2–4 "
                "sections with `##` subheadings and real line breaks between them. Plain prose "
                "only — do NOT embed follow_ups, memory_delta, or lessons here; return those in "
                "their own fields. Do not wrap the whole thing in a code fence."
            ),
        }
    }
    if with_delta:
        properties["memory_delta"] = DELTA_OUTPUT_SCHEMA
    if with_follow_ups:
        properties["follow_ups"] = _FOLLOW_UPS_SCHEMA
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        # OpenAI strict mode: ``required`` must list every present key. ``memory_delta`` and
        # ``follow_ups`` are nullable at their roots, so requiring them still lets the model emit
        # ``null`` when there is nothing to record.
        "required": list(properties),
    }


def _render_findings_digest(findings: Sequence[Finding]) -> str:
    """Render an evaluator's findings as bounded lines for the observation prompt.

    Severity + reason + paths only: enough for the observer to react to what the step actually said,
    without pulling a full review's prose into every per-step turn. Long reasons are cut at
    :data:`~wastech_orchestrator.core.follow_ups.FINDING_TITLE_MAX`, the same bound the follow-up
    titles use.
    """
    lines = []
    for finding in findings:
        reason = " ".join(finding.reason.split())
        if len(reason) > FINDING_TITLE_MAX:
            reason = reason[:FINDING_TITLE_MAX].rstrip() + "…"
        paths = f" ({', '.join(finding.paths)})" if finding.paths else ""
        lines.append(f"- [{finding.severity}] {reason}{paths}")
    return "\n".join(lines) + "\n"


# Pseudo-tags a model sometimes emits instead of a clean tool call — the whole
# `<summary>…</summary><follow_ups>[JSON]</follow_ups><memory_delta>[JSON]</memory_delta>
# <lessons>[JSON]</lessons>` dump. The machine sections (`follow_ups`/`memory_delta`/`lessons`)
# must NEVER reach `summary.md` (= the PR body): they belong only in the follow-ups section and the
# memory tiers. So the prose is cut at the first such tag, and a leading `<summary>` opener dropped.
_SUMMARY_CUT_TAGS = (
    "</summary>",
    "<summary>",
    "<follow_ups>",
    "</follow_ups>",
    "<follow-ups>",
    "<memory_delta>",
    "</memory_delta>",
    "<lessons>",
    "</lessons>",
)
_SUMMARY_OPEN_TAG = "<summary>"

# Shortest sanitized prose that can pass as a whole-task synthesis. A finalize turn that fights the
# schema can collapse to a minimal probe — an observed run published ``summary: "test"`` as its PR
# body after three schema rejections, and the degradation guard (does ``summary.md`` exist?) could
# not see it. Below this floor the turn counts as having produced nothing, so the deterministic
# report (changes, steps, checks, gate verdicts, follow-ups) becomes the body instead and the run is
# flagged degraded.
#
# Deliberately low, because a false positive is itself a regression: replacing honest short prose
# with a mechanical report makes the operator surface WORSE, and it is a prose flow that pays. The
# packaged content lenses ask for four labelled points and tell the turn to keep it concrete — a
# complete answer to all four on a small revision lands around 170 characters, so a floor set to
# "a real synthesis" length would discard finished work. This catches the collapse signature (a
# probe, a placeholder, one clause) and nothing above it; the lenses carry the qualitative
# expectation, and the ``summary`` schema description already asks for a lead paragraph plus 2–4
# sections on every flow.
_SUMMARY_MIN_CHARS = 120


def _sanitize_summary(summary_text: str) -> str:
    """Strip a leaked structured dump from a finalize ``summary``.

    Returns only the human prose: a leading ``<summary>`` opener is dropped, and the text is cut at
    the first machine tag (``</summary>``, ``<follow_ups>``, ``<memory_delta>``, ``<lessons>``) so a
    raw ``memory_delta``/``lessons``/``follow_ups`` JSON dump can never ride into ``summary.md``
    (the PR body). Clean prose (the common case) passes through unchanged.
    """
    text = summary_text.strip()
    text = text.removeprefix(_SUMMARY_OPEN_TAG)
    cut = len(text)
    for tag in _SUMMARY_CUT_TAGS:
        idx = text.find(tag)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].strip()


def _render_handoff_brief(structured: Mapping[str, Any]) -> str | None:
    """Render the interpretive three-section handoff brief; ``None`` when no section has content.

    Defensive: a missing/empty/non-string section is skipped, so a partial structured output still
    yields a usable brief (or ``None`` — the orchestrator then ships the deterministic floor alone).
    """
    sections = (
        ("New surface area", structured.get("new_surface_area")),
        ("Locked decisions", structured.get("locked_decisions")),
        ("Open edges", structured.get("open_edges")),
    )
    parts = [
        f"### {title}\n{body.strip()}"
        for title, body in sections
        if isinstance(body, str) and body.strip()
    ]
    if not parts:
        return None
    return "## Interpretive handoff brief\n\n" + "\n\n".join(parts)


@dataclass(frozen=True)
class FinalizeResult:
    """What ``finalize`` produced: the ``summary.md`` path (or ``None``), the optional candidate
    memory delta (present only when memory is enabled and the turn yielded a parseable one), and the
    evidence-gated ``follow_ups`` (present only when the flow opted in via ``emit_follow_ups``)."""

    summary_path: Path | None
    candidate_delta: CandidateDelta | None = None
    follow_ups: tuple[FollowUp, ...] = ()


class SupervisorTurnSettings(Protocol):
    """The model + effort of one supervisor phase, structurally.

    Satisfied by both ``SupervisorObserveConfig`` (which carries the cadence too) and
    ``SupervisorTurnConfig``, so the shared turn runner takes either without the two config shapes
    having to be related by inheritance.
    """

    @property
    def model(self) -> str | None: ...

    @property
    def reasoning(self) -> str | None: ...


class SupervisorStorePort(Protocol):
    """The slice of the state store the supervisor needs: the immutable evaluation row, the durable
    own-session lineage (read on first use, written after each turn), the per-turn provider-attempt
    audit row (the supervisor's own billable calls) plus the task-wide read-back of those rows for
    the summary's spend report, and the two read-only tables the finalize packet is assembled from
    (``node_runs`` / ``check_runs``)."""

    def record_evaluation(self, row: EvaluationRow) -> int: ...

    def get_evaluations(self, task_id: str) -> list[EvaluationRow]: ...

    def get_node_runs(self, task_id: str) -> list[NodeRunRow]: ...

    def get_check_runs(self, task_id: str) -> list[CheckRunRow]: ...

    def get_node_lineage(
        self, task_id: str, node_id: str, subtask_order: int | None = None
    ) -> NodeLineageRow | None: ...

    def upsert_node_lineage(self, row: NodeLineageRow) -> None: ...

    def record_provider_attempt(self, attempt: ProviderAttemptRow) -> None: ...

    def get_provider_attempts_for_task(self, task_id: str) -> list[ProviderAttemptRow]: ...


class Supervisor:
    """The per-task constant oversight layer. One instance per task (it carries its own session)."""

    def __init__(
        self,
        *,
        settings: SupervisorConfig,
        router: RouterPort,
        store: SupervisorStorePort,
        repo_dir: str,
        git: GitPort | None = None,
        artifacts_root: str | Path,
        flow_dir: Path,
        exchange_root: str | Path = "",
        flow_supervisor: SupervisorBlock | None = None,
        flow_name: str | None = None,
        task_type: str | None = None,
        register_artifact: RegisterArtifact | None = None,
        prompt_audit: bool = False,
        prompt_secrets: tuple[str, ...] = (),
        default_timeout_seconds: int = 7200,
        security_preamble: str | None = None,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._settings = settings
        self._router = router
        self._store = store
        # Wall-clock for the per-turn ``provider_attempts`` timestamps; the orchestrator
        # threads its own so a run's audit timestamps share one source.
        self._clock = clock
        self._repo_dir = repo_dir
        # The Git Manager slice the layer's own provider attempt is bracketed with. The supervisor
        # turn is read-only by mandate, but on Codex — and on every provider in the advanced mode —
        # it still gets a shell, and a shell is what makes a ``.git`` mutation reachable. ``None``
        # (a harness without a clone) skips both the write guard and the comparison.
        self._git = git
        self._artifacts_root = artifacts_root
        # The provider-readable exchange root ``<repo>/.worc-io``; the supervisor's own
        # provider call passes the same pre-launch containment gate as agent/evaluator (Part C).
        self._exchange_root = exchange_root
        self._flow_dir = flow_dir
        # Flow-local supervisor prompt overrides + the follow-ups opt-in (prompt-and-supervisor
        # ``None`` → the global config prompt + built-in finalize, free-text finalize.
        self._flow_role_file = flow_supervisor.role_file if flow_supervisor else None
        self._flow_finalize_role_file = (
            flow_supervisor.finalize_role_file if flow_supervisor else None
        )
        self._flow_handoff_role_file = (
            flow_supervisor.handoff_role_file if flow_supervisor else None
        )
        self._emit_follow_ups = flow_supervisor.emit_follow_ups if flow_supervisor else False
        # Flow name + task type, recorded in the finalize packet's header so the synthesis knows
        # what shape of work it closes out. Both are per-task constants the orchestrator resolves.
        self._flow_name = flow_name
        self._task_type = task_type
        self._register_artifact = register_artifact
        self._prompt_audit = prompt_audit
        self._prompt_secrets = prompt_secrets
        self._default_timeout_seconds = default_timeout_seconds
        # Defense-in-depth: the Core-owned orchestrator security contract prepended to the
        # supervisor's own read-only turn (advisory, NOT enforcement). Resolved once by the
        # orchestrator, like the graph-node NodeServices carrier. ``None`` → no preamble.
        self._security_preamble = security_preamble
        # The supervisor's own session (resume_own_lineage). Held in-memory within a process run and
        # persisted to / hydrated from ``node_lineage`` so it survives a restart (independent of the
        # editing-lineage authors). ``None`` until the first turn runs or the persisted row is read.
        self._own_session_id: str | None = None

    # -- per-step observation --------------------------------------------------

    def observe(
        self,
        *,
        task_id: str,
        node_id: str,
        node_run_id: int,
        outcome_kind: str,
        final_message: str | None = None,
        findings: Sequence[Finding] = (),
        subtask_order: int | None = None,
    ) -> None:
        """Observe one completed step read-only and record an advisory ``supervisor_step`` row.

        Called for the executed nodes the orchestrator's post-node hook selects — every kind
        except the deterministic ``tool`` / ``checks`` nodes and the terminal ``publish`` node.
        Best-effort: a failed observation is logged and swallowed — it is advisory and must never
        fail or reroute the task. Namespaced by ``source_node_run_id`` (the step), so a resumed run
        does not duplicate observations.

        ``findings`` are an evaluator's typed findings for this step: without them the
        observation is a bare outcome label with nothing to react to, which is why the observer made
        no tool calls on any evaluator step of the run this came from.
        """
        prompt = self._step_prompt(task_id, node_id, outcome_kind, final_message, findings)
        # Observation is advisory and runs once per node-run, so a deep fix loop drives many
        # observe turns; it never needs a max reasoning tier, so cap it to `high` (the whole-task
        # finalize keeps the configured tier).
        note = self._run(
            task_id,
            prompt,
            node_run_id=node_run_id,
            turn=self._settings.observe,
            function=SupervisorFunction.OBSERVE,
            subtask=subtask_order,
            cap_reasoning=True,
        )
        self._record(
            task_id,
            kind="supervisor_step",
            source_node_run_id=node_run_id,
            subtask_order=subtask_order,
            # ``note is None`` means the observation could not run (infra/setup), distinct from an
            # empty advisory note ("nothing to add") — so a silent advisory layer is diagnosable.
            payload={
                "node": node_id,
                "outcome": outcome_kind,
                "note": note or "",
                "observation_failed": note is None,
            },
        )

    # -- whole-task finalize ---------------------------------------------------

    def finalize(
        self,
        *,
        task_id: str,
        task_title: str,
        task_path: str | None = None,
        emit_delta: bool = False,
    ) -> FinalizeResult:
        """Synthesize the whole-task summary (once, at task close) and record ``supervisor_final``.

        Writes the working ``summary.{md,json}`` under the task artifact dir (the ``summary.md`` is
        the PR body). When ``emit_delta`` (memory enabled) the SAME finalize turn also yields a
        structured ``candidate_memory_delta``, and when the flow opted in (``emit_follow_ups``) that
        same turn yields the evidence-gated ``follow_ups`` array — zero extra LLM calls.
        When neither is enabled the turn is free-text.

        Best-effort: a turn that cannot run — or one whose prose collapses below
        :data:`_SUMMARY_MIN_CHARS` — yields no ``summary.md`` (the orchestrator's deterministic
        report then applies, flagged degraded), a ``None`` delta, and no follow-ups.
        ``summary.json`` is always written. Returns the summary path + the delta + the follow-ups.

        The turn **always** runs on a fresh session seeded by the ``SupervisorPacket``, on a normal
        run exactly as on a revive: resuming a warm session instead would give a revived task a
        thinner summary and grow this call's input with every rework cycle. There is no warm
        auto-fallback if the packet cannot be built — that would put non-determinism back into the
        one path this makes reproducible and hide the build failure. The fallback is the
        orchestrator's deterministic minimal summary, the same one a turn that produced nothing
        gets.
        """
        # ``node_run_id=0`` is the once-per-task finalize sentinel; per-step observations use the
        # observed step's id, so each supervisor turn writes a distinct artifact dir (no collision).
        # Read the evaluation rows ONCE: the same list feeds the gate digest the turn is grounded in
        # and the finding-derived follow-ups merged after it.
        evaluations = self._store.get_evaluations(task_id)
        packet_path = self._publish_packet(task_id, task_title, evaluations)
        # ``task_title`` is used only for the orchestrator-side PR-body H1 / summary.json below (not
        # a provider prompt surface); the finalize *prompt* reads the task from the frozen exchange
        # packet path (no inline task body/title reaches the provider).
        summary_text, delta, follow_ups = self._finalize_turn(
            task_id,
            emit_delta,
            packet_path=packet_path,
            task_path=task_path,
            gates=render_gate_digest(evaluations),
        )
        # An evaluator that accepts *with* findings (sub-threshold, non-gating) persists them
        # to the evaluations table and they otherwise reach no operator surface. Merge them into the
        # follow-ups — deduped against the supervisor's own list — so they land in summary.{json,md}
        # and the PR body. Runs independent of ``emit_follow_ups`` (a distinct, evidence-bearing
        # source from the supervisor's LLM-authored follow-ups).
        follow_ups = merge_follow_ups(follow_ups, evaluator_finding_follow_ups(evaluations))
        # A model sometimes emits its structured output as a `<summary>…</summary>
        # <follow_ups>[JSON]</follow_ups><memory_delta>…` text dump instead of a clean tool call.
        # Sanitize so only the human prose reaches summary.md (the PR body) — never a raw
        # follow_ups/memory_delta/lessons dump.
        clean_summary = _sanitize_summary(summary_text) if summary_text else ""
        if clean_summary and len(clean_summary) < _SUMMARY_MIN_CHARS:
            # A collapse, not a synthesis (see _SUMMARY_MIN_CHARS): drop it so the deterministic
            # report becomes the body and the orchestrator's degradation warning fires. Logged with
            # the observed length, because the prose itself is what a diagnosis needs to see.
            _LOG.warning(
                "supervisor finalize: summary below the %d-char floor (%d chars) — discarded as a "
                "collapsed generation; the deterministic report will be the summary: %r",
                _SUMMARY_MIN_CHARS,
                len(clean_summary),
                clean_summary,
            )
            clean_summary = ""
        # Recorded AFTER sanitize + the floor, so ``summary_written`` states what actually reached
        # disk. Derived from the raw turn output it disagreed with the file whenever the model
        # returned a pure tag dump (sanitizes to empty) or a collapsed one.
        self._record(
            task_id,
            kind="supervisor_final",
            source_node_run_id=None,
            subtask_order=None,
            payload={
                "summary_written": bool(clean_summary),
                "memory_delta": delta is not None,
                "follow_ups": len(follow_ups),
                # Whether the turn was actually seeded by the packet. Always true on a healthy run;
                # false records a build/publish failure, which would otherwise be invisible in a
                # thin-but-present summary.
                "packet_built": packet_path is not None,
            },
        )
        task_dir = Path(task_artifact_dir(self._artifacts_root, task_id))
        self._write_summary_json(task_dir, task_id, task_title, clean_summary or None, follow_ups)
        if not clean_summary:
            return FinalizeResult(summary_path=None, candidate_delta=delta, follow_ups=follow_ups)
        md_path = task_dir / "summary.md"
        # Prefix a deterministic H1 so the PR body is never a headless paragraph-slab — unless
        # the model already opened with its own top-level heading.
        if clean_summary.startswith("# "):
            body = clean_summary
        else:
            body = f"# {task_title}\n\n{clean_summary}"
        body = body.rstrip("\n") + "\n"
        if follow_ups:  # surface the evidence-gated debt/follow-ups as a section in the PR body
            body += "\n" + render_follow_ups_section(follow_ups)
        # ``newline=""``: this body is committed as the pull-request description, and the
        # deterministic report writes the same file, so both must land as LF on every host.
        md_path.write_text(body, encoding="utf-8", newline="")
        self._register(task_id, "summary_md", str(md_path))
        return FinalizeResult(summary_path=md_path, candidate_delta=delta, follow_ups=follow_ups)

    def _finalize_turn(
        self,
        task_id: str,
        emit_delta: bool,
        *,
        packet_path: str | None = None,
        task_path: str | None = None,
        gates: str | None = None,
    ) -> tuple[str | None, CandidateDelta | None, tuple[FollowUp, ...]]:
        """Run the single finalize turn. Free-text when neither memory nor follow-ups are enabled
        (the default); otherwise a structured ``{summary, ...}`` turn, so every enabled
        output (``memory_delta`` / ``follow_ups``) rides one turn (no extra LLM call).

        Always ``resume_session=False``: the turn is grounded in the ``SupervisorPacket`` at
        ``packet_path``, not in session memory. ``gates`` is the rendered evaluator-verdict digest,
        which stays inline in the prompt — it is bounded by the number of evaluator nodes (not by
        the rework count) and it is the guard that keeps "the gates passed" from being written over
        findings that are actually open."""
        with_follow_ups = self._emit_follow_ups
        if not emit_delta and not with_follow_ups:
            text = self._run(
                task_id,
                self._finalize_prompt(task_id, packet=packet_path is not None, gates=gates),
                node_run_id=0,
                turn=self._settings.finalize,
                function=SupervisorFunction.FINALIZE,
                resume_session=False,
                task_path=task_path,
                supervisor_packet_path=packet_path,
            )
            return text, None, ()
        result = self._run_result(
            task_id,
            self._finalize_prompt(
                task_id,
                with_delta=emit_delta,
                with_follow_ups=with_follow_ups,
                packet=packet_path is not None,
                gates=gates,
            ),
            node_run_id=0,
            turn=self._settings.finalize,
            function=SupervisorFunction.FINALIZE,
            output_schema=_finalize_schema(with_delta=emit_delta, with_follow_ups=with_follow_ups),
            resume_session=False,
            task_path=task_path,
            supervisor_packet_path=packet_path,
        )
        if result is None or result.structured_output is None:
            return None, None, ()
        summary = result.structured_output.get("summary")
        summary_text = summary if isinstance(summary, str) and summary.strip() else None
        delta = parse_delta(result.structured_output.get("memory_delta")) if emit_delta else None
        follow_ups = (
            parse_follow_ups(result.structured_output.get("follow_ups")) if with_follow_ups else ()
        )
        return summary_text, delta, follow_ups

    @staticmethod
    def _finalize_digest(evaluations: Sequence[EvaluationRow]) -> str | None:
        """Render the recorded ``supervisor_step`` observations as the packet's material digest.

        The per-step notes are immutable append-only rows in ``evaluations`` (``state.db``) — always
        present on a revived task, independent of the durable session and of logging config. Renders
        the usable notes as compact ``- [node → outcome] note`` lines, skipping the ones that
        failed to run or added nothing (empty note). Returns ``None`` when nothing usable was
        recorded, in which case the packet's other blocks (changes / steps / checks) carry it
        alone."""
        lines: list[str] = []
        for row in evaluations:
            if row.kind != "supervisor_step":
                continue
            try:
                payload = json.loads(row.findings_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            note = str(payload.get("note") or "").strip()
            if not note:  # observation_failed or "nothing to add" — no material to synthesize from
                continue
            node = str(payload.get("node") or "?")
            outcome = str(payload.get("outcome") or "?")
            lines.append(f"- [{node} → {outcome}] {note}")
        return "\n".join(lines) if lines else None

    # -- the finalize packet ---------------------------------------------------

    def _publish_packet(
        self, task_id: str, task_title: str, evaluations: Sequence[EvaluationRow]
    ) -> str | None:
        """Write the packet privately, publish a redacted copy, return the provider-readable path.

        The private ``packet.json`` under the finalize sentinel's artifact dir is the authoritative
        audit copy; the provider only ever sees the copy published through the exchange seam, which
        redacts on the way in — so the packet needs no redaction mechanism of its own, only the
        per-attempt secret literals passed here.

        Best-effort like every other part of this advisory layer: a failure is logged and yields
        ``None``, and finalize runs unseeded rather than raising. It deliberately does **not** fall
        back to resuming the warm session — that would restore the non-determinism this replaced and
        mask the build failure.
        """
        try:
            content = self._build_packet(task_id, task_title, evaluations)
            private = (
                node_run_dir(self._artifacts_root, task_id, _SUPERVISOR_IDENTITY, 0)
                / _PACKET_FILENAME
            )
            private.parent.mkdir(parents=True, exist_ok=True)
            # ``newline=""`` keeps the canonical LF bytes on Windows too — the same house pattern
            # the exchange manifests are written with; a CRLF rewrite would break byte-identity.
            private.write_text(content, encoding="utf-8", newline="")
            self._register(task_id, "supervisor_packet", str(private))
            published = publish_artifact(
                str(self._exchange_root),
                task_id,
                f"{_SUPERVISOR_IDENTITY}/{_PACKET_FILENAME}",
                content,
                extra_secrets=self._prompt_secrets,
                private_path=str(private),
            )
            return Path(published).as_posix()
        except Exception as exc:
            _LOG.warning(
                "supervisor packet could not be built (finalize runs unseeded)",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
            )
            return None

    def _build_packet(
        self, task_id: str, task_title: str, evaluations: Sequence[EvaluationRow]
    ) -> str:
        """Render the packet from the run's durable facts — no live inputs.

        The assembly itself deliberately lives outside this class: nothing in it depends on this
        layer, so it stays reachable when the layer does not run at all. The observation digest is
        the one field this layer contributes.
        """
        return render_packet(
            build_packet_facts(
                self._store,
                task_id=task_id,
                task_title=task_title,
                task_type=self._task_type,
                flow_name=self._flow_name,
                evaluations=evaluations,
                artifacts_root=self._artifacts_root,
                exchange_root=self._exchange_root,
                repo_dir=self._repo_dir,
                material_observations=self._finalize_digest(evaluations),
            )
        )

    # -- intra-task subtask handoff --------------------------------------------

    def handoff(self, *, task_id: str, subtask_order: int, floor_context: str) -> str | None:
        """Emit the interpretive handoff brief for a subtask boundary.

        Resumes the warm durable ``__supervisor__`` session — it already observed the predecessor
        subtask(s), so this is a small incremental turn, **not** a new turn budget. Returns a
        rendered three-section brief (New surface area / Locked decisions / Open edges) or ``None``.

        Best-effort by contract, exactly like :meth:`finalize`: any failure (no provider, infra
        error, unreadable role file, malformed/empty structured output) is logged and yields
        ``None``, and the orchestrator ships the deterministic factual floor alone. The returned
        brief is redacted by the caller (with the floor) before it is written — no secret reaches
        the ``.handoff.md`` artifact.
        """
        result = self._run_result(
            task_id,
            self._handoff_prompt(task_id, subtask_order, floor_context),
            node_run_id=_HANDOFF_RUN_ID_BASE + subtask_order,
            turn=self._settings.handoff,
            function=SupervisorFunction.HANDOFF,
            subtask=subtask_order,
            output_schema=_HANDOFF_SCHEMA,
        )
        if result is None or result.structured_output is None:
            return None
        return _render_handoff_brief(result.structured_output)

    # -- internals -------------------------------------------------------------

    def _run(
        self,
        task_id: str,
        prompt: str,
        *,
        node_run_id: int,
        turn: SupervisorTurnSettings,
        function: SupervisorFunction,
        subtask: int | None = None,
        resume_session: bool = True,
        cap_reasoning: bool = False,
        task_path: str | None = None,
        supervisor_packet_path: str | None = None,
    ) -> str | None:
        """Run one read-only supervisor turn and return its final message (``None`` on failure)."""
        result = self._run_result(
            task_id,
            prompt,
            node_run_id=node_run_id,
            turn=turn,
            function=function,
            subtask=subtask,
            resume_session=resume_session,
            cap_reasoning=cap_reasoning,
            task_path=task_path,
            supervisor_packet_path=supervisor_packet_path,
        )
        return result.final_message if result is not None else None

    def _run_result(
        self,
        task_id: str,
        prompt: str,
        *,
        node_run_id: int,
        turn: SupervisorTurnSettings,
        function: SupervisorFunction,
        subtask: int | None = None,
        output_schema: dict[str, Any] | None = None,
        resume_session: bool = True,
        cap_reasoning: bool = False,
        task_path: str | None = None,
        supervisor_packet_path: str | None = None,
    ) -> AgentRunResult | None:
        """Run one read-only supervisor LLM turn on its own session; return the full result.

        Continues the supervisor's own ``resume_own_lineage`` session: it resumes the in-memory id,
        or — on a fresh process after a restart — the persisted ``node_lineage`` session, and writes
        the new session id back after the turn. Best-effort by contract: any failure (no provider,
        infra error, role file unreadable) is logged and yields ``None`` — never raised.

        ``node_run_id`` namespaces the on-disk artifact dir (``stages/supervisor/run-NNNNNN/``): the
        upfront proposal, per-step observation, and finalize each pass a distinct id, so successive
        turns never collide on the same path (the artifact writer never overwrites). A non-None
        ``output_schema`` forces structured output (the proposal); ``None`` leaves it free-text.

        ``resume_session=False`` starts a fresh session (no ``session_id``): finalize always uses
        it, because it is grounded in the ``SupervisorPacket`` at ``supervisor_packet_path`` rather
        than in session memory — which also removes the failure mode of resuming a dead session.

        ``turn`` is the calling phase's own model + effort (``config.supervisor.observe`` /
        ``.finalize`` / ``.handoff``). Required, not defaulted: a cheap note and the whole-task
        synthesis want opposite tiers, so a phase that forgot to say which it is should not compile
        rather than silently inherit the wrong one. The provider stays one per layer.

        ``function`` is what that phase *is*, recorded on the attempt rows so the layer's spend is
        readable per job. It cannot be inferred from ``turn``: two different jobs deliberately share
        the observe phase's cheap model + effort, so the settings object does not identify either.
        """
        control_before: GitControlState | None = None
        try:
            route = self._router.resolve_route(_SUPERVISOR_IDENTITY, self._settings.provider)
            # Cap to `high` for a schema turn (fragile at max) OR an advisory observe turn
            # (cost-heavy in a deep loop); a free-text finalize keeps its configured tier.
            reasoning = _schema_safe_reasoning(turn.reasoning, output_schema)
            if cap_reasoning and reasoning in _MAX_REASONING_TIERS:
                reasoning = _SCHEMA_REASONING_CAP
            # The session to resume + its persisted cumulative usage baseline: a resumed
            # Codex session counts cumulatively, so the per-turn ``provider_attempts`` usage is a
            # summation-safe delta against the previous cumulative — exactly like a graph-node
            # lineage. ``resume_session=False`` (finalize's revive path) starts fresh, no baseline.
            resume_id, usage_baseline, baseline_session_id = (
                self._resume_context(task_id, route) if resume_session else (None, None, None)
            )
            # The same per-attempt bracket the graph nodes take, keyed on the same question: does
            # this attempt get a shell? The layer is read-only by mandate, but the mandate is not a
            # mechanism — Codex runs commands on ``read-only``, and in the advanced mode so does
            # Claude. With the write guard on the request the provider's pre-launch canary re-proves
            # the ``.git``/``.worc`` denies here too, which is what makes floor 1's "before every
            # provider attempt" literally true; drift after the turn is reported, never parked,
            # because an advisory layer must not be able to stop a reviewed, passing change.
            write_guard, control_before = self._control_bracket(route)
            request = AgentRunRequest(
                task_id=task_id,
                node_id=_SUPERVISOR_IDENTITY,
                working_directory=self._repo_dir,
                prompt=prompt,
                permission_profile=_SUPERVISOR_PROFILE,  # forced — the supervisor never writes
                timeout_seconds=self._default_timeout_seconds,
                attempt=1,
                node_run_id=node_run_id,  # not a graph node; audit lives in ``evaluations``
                model=turn.model,
                reasoning=reasoning,
                output_schema=output_schema,
                session_id=resume_id,
                # The task reaches the supervisor as the frozen exchange packet path (never
                # inline title/description). Repository instructions are NOT injected — the
                # supervisor's read-only turn reads the repo's root files itself, like graph nodes.
                task_path=task_path,
                # The whole-task facts for a finalize turn, likewise by path — inlining the JSON
                # would put back exactly the bytes this replaced, and bypass the redaction seam.
                supervisor_packet_path=supervisor_packet_path,
                # Defense-in-depth: the Core-owned orchestrator security contract (advisory).
                security_preamble=self._security_preamble,
                # The Git-control / lifecycle roots this attempt must not be able to write. Resolved
                # only when the attempt has a shell — a profile that can run nothing needs no
                # carve-out from it.
                write_guard=write_guard,
            )
            # Same pre-launch containment invariant as agent/evaluator: the supervisor
            # carries the frozen exchange ``task_path``, so this asserts it resolves under the
            # current-task exchange before the read-only call.
            if self._exchange_root:
                assert_orchestration_paths_contained(request, str(self._exchange_root))
            outcome = self._router.run_stage(request, route)
        except Exception as exc:
            _LOG.warning(
                "supervisor turn failed (advisory, ignored)",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
            )
            return None
        # Give the supervisor's own billable provider calls an audit home in
        # ``provider_attempts`` (``node_run_id`` NULL — it is not a graph node), so a task-level
        # cost/usage roll-up is complete. Recorded for every outcome (including a failed turn's
        # attempts) and BEFORE the result-None early return, so no billable call is dropped.
        self._record_provider_attempts(
            task_id, outcome, function, usage_baseline, baseline_session_id
        )
        self._report_control_drift(task_id, control_before)
        result = outcome.result
        if result is None:
            return None
        if result.session_id:
            self._own_session_id = result.session_id  # resume_own_lineage continuity (in-memory)
            self._persist_session(
                task_id, result.session_id, outcome.provider_used, result.normalized_usage
            )
        self._record_turn_observability(
            task_id=task_id,
            node_run_id=node_run_id,
            subtask=subtask,
            request=request,
            route=route,
            outcome=outcome,
            reasoning=reasoning,
        )
        return result

    def _control_bracket(
        self, route: ResolvedRoute
    ) -> tuple[ProviderWriteGuardPolicy | None, GitControlState | None]:
        """``(write guard, control fingerprint)`` for a supervisor turn that gets a shell.

        ``(None, None)`` when there is no Git Manager or when neither end of the route would give
        this turn a shell — the signal to skip both, so a turn that can run nothing pays for
        neither. The shell answer comes from the adapters through the Router (the layer's profile is
        forced ``read-only``, and it declares no git-evidence grant), fail-closed like everywhere
        else: an unclassifiable attempt is bracketed rather than left unwatched.
        """
        if self._git is None:
            return None, None
        if not self._router.route_grants_shell(
            route, permission_profile=_SUPERVISOR_PROFILE, git_evidence=False
        ):
            return None, None
        exchange_root = str(self._exchange_root) if self._exchange_root else None
        return (
            self._git.resolve_control_paths(exchange_root),
            self._git.capture_git_control_state(),
        )

    def _report_control_drift(self, task_id: str, before: GitControlState | None) -> None:
        """Warn when the Git control state moved across a supervisor turn; never park.

        The layer is advisory by contract — it can flag but cannot rework, and a reviewed, passing
        change is never blocked by it — so this is the same verdict every non-writing node class
        gets: a loud line carrying the drift's aspect-level summary, and the run continues. The
        summary comes from the redacted formatter, so no configuration value reaches the log.
        """
        if before is None or self._git is None:
            return
        drift = self._git.compare_git_control_state(before)
        if drift is None:
            return
        _LOG.warning(
            "git control state changed during a supervisor turn (advisory, run continues): %s",
            drift.summary(),
            extra={"task_id": task_id},
        )

    def _record_turn_observability(
        self,
        *,
        task_id: str,
        node_run_id: int,
        subtask: int | None,
        request: AgentRunRequest,
        route: ResolvedRoute,
        outcome: StageOutcome,
        reasoning: str | None,
    ) -> None:
        """Best-effort: persist rendered-prompt + (gated) prompt-audit for one supervisor turn.

        Calls the standalone artifact writers directly — never ``record_run_observability``: the
        supervisor's ``node_run_id`` here is a synthetic per-call-site artifact-dir namespacing
        sentinel (``0`` / ``_PROPOSAL_RUN_ID`` / ``_HANDOFF_RUN_ID_BASE + n`` / a reused real step
        id), NOT a ``node_runs`` id. Its ``provider_attempts`` rows are written separately by
        :meth:`_record_provider_attempts` with ``node_run_id`` NULL, so that synthetic id
        never lands in the audit table and misattributes the row. Wrapped in its own try/except
        (distinct from the caller's, which does not cover this code) so an audit-write failure can
        never surface as a broken turn — this layer is advisory by contract.
        """
        if self._register_artifact is None or outcome.result is None:
            return
        effective_prompt = build_effective_prompt(request)
        try:
            write_rendered_prompt(
                artifacts_root=str(self._artifacts_root),
                task_id=task_id,
                node_id=_SUPERVISOR_IDENTITY,
                run_id=node_run_id,
                prompt=effective_prompt,
                secrets=self._prompt_secrets,
                register=self._register_artifact,
            )
            if self._prompt_audit:
                write_prompt_audit(
                    artifacts_root=str(self._artifacts_root),
                    task_id=task_id,
                    node_id=_SUPERVISOR_IDENTITY,
                    subtask=subtask,
                    run_id=node_run_id,
                    prompt=effective_prompt,
                    route=route,
                    outcome=outcome,
                    # The phase's model as actually sent — read off the request rather than
                    # re-resolving it, so the audit cannot disagree with the launch.
                    model=request.model,
                    reasoning=reasoning,
                    started_at=outcome.result.started_at,
                    secrets=self._prompt_secrets,
                    register=self._register_artifact,
                )
        except Exception as exc:
            _LOG.warning(
                "supervisor prompt-audit write failed (advisory, ignored)",
                extra={
                    "task_id": task_id,
                    "node_run_id": node_run_id,
                    "error_type": type(exc).__name__,
                },
            )

    def _record_provider_attempts(
        self,
        task_id: str,
        outcome: StageOutcome,
        function: SupervisorFunction,
        usage_baseline: NormalizedUsage | None,
        baseline_session_id: str | None,
    ) -> None:
        """Persist the supervisor's own ``provider_attempts`` rows — ``node_run_id`` NULL.

        Reuses the shared node-path recorder, so the supervisor's per-run usage delta is computed
        exactly like a graph node's (against its own resumed-session baseline). ``function`` is what
        makes the layer's spend readable per job rather than as one lump. Best-effort like the rest
        of this advisory layer: any store error is logged and swallowed so an audit-write failure
        can never surface as a broken turn.
        """
        try:
            record_provider_attempts(
                self._store,
                self._clock,
                task_id=task_id,
                node_run_id=None,  # the supervisor is a constant layer, not a graph node
                outcome=outcome,
                supervisor_function=function.value,
                usage_baseline=usage_baseline,
                baseline_session_id=baseline_session_id,
            )
        except Exception as exc:
            _LOG.warning(
                "supervisor provider-attempt record failed (advisory, ignored)",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
            )

    def _resume_context(
        self, task_id: str, route: ResolvedRoute
    ) -> tuple[str | None, NormalizedUsage | None, str | None]:
        """``(session to resume, its usage baseline, that session's id)`` for the next turn.

        The own session to resume is the in-memory id if a turn already ran this process, else the
        persisted lineage — but only when produced by the provider now resolved (you cannot resume
        a Claude session on Codex). On the first round there is no lineage yet, so it starts fresh.
        The usage baseline is that session's persisted running cumulative snapshot: it is
        updated after every turn (:meth:`_persist_session`), so turn N reduces against turn N-1's
        cumulative, making the recorded per-run usage summation-safe on a cumulative provider
        (Codex). A per-invocation provider (Claude) ignores the baseline in ``compute_usage_delta``.
        """
        row = self._store.get_node_lineage(task_id, _SUPERVISOR_LINEAGE_NODE_ID, None)
        baseline = deserialize_usage(row.usage_snapshot) if row else None
        if self._own_session_id is not None:
            # A turn already ran this process: continue that live session; its cumulative was
            # persisted onto the same lineage row, so ``baseline`` is the previous turn's snapshot.
            return self._own_session_id, baseline, row.raw_session_id if row else None
        if row is None or row.provider != route.primary.value:
            return None, None, None
        self._own_session_id = row.raw_session_id
        return self._own_session_id, baseline, row.raw_session_id

    def _persist_session(
        self,
        task_id: str,
        session_id: str,
        provider_used: ProviderId | None,
        usage: NormalizedUsage | None,
    ) -> None:
        """Persist the supervisor's own session after a successful turn (``state.db`` only — the raw
        id is redacted everywhere else), keyed by the reserved sentinel so a resumed task resumes
        it.

        Also carries the session's running cumulative usage snapshot, so the next turn's
        ``provider_attempts`` usage can be reduced to a summation-safe per-run delta — the same
        contract a graph node's lineage keeps. ``None`` for a per-invocation provider (Claude).
        """
        if provider_used is None:
            return
        self._store.upsert_node_lineage(
            NodeLineageRow(
                task_id=task_id,
                node_id=_SUPERVISOR_LINEAGE_NODE_ID,
                provider=provider_used.value,
                raw_session_id=session_id,
                subtask_order=None,
                usage_snapshot=snapshot_for_lineage(usage),
            )
        )

    def _record(
        self,
        task_id: str,
        *,
        kind: str,
        source_node_run_id: int | None,
        subtask_order: int | None,
        payload: dict[str, object],
    ) -> None:
        self._store.record_evaluation(
            EvaluationRow(
                task_id=task_id,
                node_id=None,  # the supervisor is a layer, not a node
                source_node_run_id=source_node_run_id,
                subtask_order=subtask_order,
                kind=kind,
                verdict="advisory",  # the supervisor never routes
                findings_json=json.dumps(payload, ensure_ascii=False),
            )
        )

    def _step_prompt(
        self,
        task_id: str,
        node_id: str,
        outcome_kind: str,
        final_message: str | None,
        findings: Sequence[Finding] = (),
    ) -> str:
        observed = f"## Step observed\nNode: {node_id}\nOutcome: {outcome_kind}\n"
        if findings:
            observed += "\nFindings it recorded:\n" + _render_findings_digest(findings)
        if final_message:
            # Bounded by the same per-step cap the packet uses: unbounded, a chatty node's closing
            # message inflated every observation turn, and each rework round paid for it again.
            observed += f"\nThe step reported:\n{bound_step_message(final_message)}\n"
        return self._base_prompt(task_id) + "\n\n" + observed

    def _finalize_prompt(
        self,
        task_id: str,
        *,
        with_delta: bool = False,
        with_follow_ups: bool = False,
        packet: bool = False,
        gates: str | None = None,
    ) -> str:
        # The finalize lens (flow ``finalize_role_file`` → built-in) carries the summary emphasis;
        # only the machine-contract additions (task context, follow-ups, memory delta) are appended
        # in code, so a flow author reshapes wording but never the parsed schema. The task
        # reaches the turn as the frozen exchange packet (context footer), never inline title/body.
        prompt = (
            self._finalize_base(task_id)
            + "\n\n## Task under review\nThe task specification is provided as the task packet "
            + "referenced in the context below — read it there; it is not inlined here.\n"
        )
        if gates:
            # The finalize turn describing the gates from session memory alone wrote
            # "three independent verification gates … all of which passed" while four critic
            # findings sat in state.db. These are the recorded verdicts, so "passed" is not
            # writable about a gate that emitted findings.
            prompt += (
                "\n## Gate verdicts recorded for this task\n"
                "Every in-flow evaluator's final verdict, with the findings it recorded. Ground "
                "each statement you make about verification in this list: a gate that recorded "
                "findings did **not** simply pass — say what it found and that the flow accepted "
                "it with those findings open. Do not name a gate that is absent from this list, "
                "and do not describe a check as something you performed yourself.\n\n"
                f"{gates}\n"
            )
        if packet:
            # The turn runs on a fresh session by design, so there IS no session memory to lean on:
            # say where the facts are and that they are the ground truth. Pointing at the packet
            # instead of inlining it is the whole point — the JSON is read once as a file rather
            # than re-sent as prompt input on every turn of the run.
            prompt += (
                "\n## Run facts (the packet)\n"
                "This is a fresh session: you are NOT continuing an earlier conversation about "
                "this task, so do not write from memory of one. Read the `packet` file referenced "
                "in the context below — it is the deterministic record of this run (the changed "
                "paths and diff stat with a pointer to the full diff, every executed step with its "
                "outcome and what it reported, the checks that ran, and your own recorded per-step "
                "observations) — and ground every statement you make in it. Open the artifacts it "
                "points at when you need more detail than it carries. If something is absent from "
                "the packet, say so plainly rather than inferring it.\n"
            )
        if with_follow_ups:
            prompt += (
                "\n## Technical debt / follow-ups\n"
                "Also record concrete technical debt and refactor follow-ups you observed, as the "
                "structured `follow_ups` array. Each record is minimal and **evidence-gated**: a "
                "`title`, a short `rationale`, the `paths` it concerns, `evidence` pointers "
                "(files/lines/commits/checks that substantiate it), a `severity` "
                "(low/medium/high), and an optional `action_hint`. The `title` is an independent "
                "imperative label (aim for 80 characters or fewer) that reads on its own in a work "
                "queue — never a prefix or restatement of `rationale`. Propose only debt grounded "
                "in what actually happened this run — never speculative ideas; a record without "
                "evidence is dropped.\n"
                "**Always emit the `follow_ups` key** — an empty array when nothing qualifies. "
                "Omitting it fails the response schema and costs the whole synthesis.\n"
            )
            if gates:
                # Both sources land in one list, and the merge dedups on exact text only, so a
                # paraphrase of an accepted finding survives as a second bullet — measured at 10
                # bullets for ~6 issues, two pairs disagreeing on severity. Cheapest sound fix is
                # to stop the restatement rather than to guess which near-duplicates are the same.
                prompt += (
                    "Do **not** restate the evaluator findings listed under the gate verdicts "
                    "above: every finding a gate accepted is carried into this list "
                    "deterministically, so repeating one in your own words produces a duplicate "
                    "entry (often at a contradictory severity). Record only debt that is NOT "
                    "already in that list.\n"
                )
        if with_delta:
            prompt += (
                "\n## Candidate memory delta\n"
                "Also propose what is worth REMEMBERING for future tasks on this repo, as "
                "the structured `memory_delta`: durable `lessons` — repeatable PATTERNS and "
                "PRINCIPLES worth internalizing (recurring reviewer expectations, procedural "
                "gotchas, stable conventions/commands, architecture invariants, fragile "
                "areas), each with `kind`, `subject`, `statement`, and `evidence` pointers to "
                "repo files, docs, or named checks; recurring `failures` (signature + remedy); "
                "and important `entities` (files/modules with their paths). Put WHAT a file or "
                "module is or does in an `entity` card (with `risk_notes`), NOT in a lesson — a "
                "lesson captures a repeatable practice or principle, not a description. Anchor "
                "every `evidence` ref on something durable and resolvable — a repo path, a doc, "
                "a named check — NOT a commit SHA or a task id, which rot after merge. Do NOT "
                "narrate which task did what; capture durable knowledge, not this run's "
                "history. Propose only what repeats, stays true, or saves rediscovery — never "
                "secrets, raw diffs, or one-off details; every lesson needs evidence. Leave a "
                "list empty when nothing qualifies.\n"
            )
        return prompt

    def _base_prompt(self, task_id: str) -> str:
        """The observe lens: flow ``role_file`` → global ``config.supervisor.role_file`` → built-in.

        Best-effort: each candidate that is missing/bad/traversing (``RoleFileError``) falls through
        to the next, so the supervisor's per-step observation never breaks."""
        return self._render_chain(
            task_id, (self._flow_role_file, self._settings.role_file), _BUILTIN_OBSERVE
        )

    def _finalize_base(self, task_id: str) -> str:
        """The finalize lens: flow ``finalize_role_file`` → built-in (no global one — YAGNI)."""
        return self._render_chain(task_id, (self._flow_finalize_role_file,), _BUILTIN_FINALIZE)

    def _handoff_prompt(self, task_id: str, subtask_order: int, floor_context: str) -> str:
        # The handoff lens (flow ``handoff_role_file`` → built-in) carries the wording; the machine
        # contract (the three-section schema) is appended by code, not the file.
        return (
            self._handoff_base(task_id)
            + f"\n\n## Handoff to subtask {subtask_order}\n"
            + "The predecessor subtask(s) it depends on just completed and committed:\n\n"
            + floor_context
            + "\n\nWrite a focused brief for the agent implementing this next subtask, as the "
            "structured output's three sections: `new_surface_area` (what the predecessor(s) built "
            "that the successor should use), `locked_decisions` (contracts not to revisit, with "
            "brief rationale), and `open_edges` (what was deferred or must not be touched). Ground "
            "every claim in the facts above; be concise. Do not edit code."
        )

    def _handoff_base(self, task_id: str) -> str:
        """The handoff lens: flow ``handoff_role_file`` → built-in (no global one — YAGNI)."""
        return self._render_chain(task_id, (self._flow_handoff_role_file,), _BUILTIN_HANDOFF)

    def _render_chain(self, task_id: str, candidates: tuple[str | None, ...], fallback: str) -> str:
        """Render the first readable role file in *candidates*, else *fallback* (best-effort)."""
        variables: dict[str, object | None] = {
            "task_id": task_id,
            "repo": self._repo_dir,
            "repo_path": self._repo_dir,
        }
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return render_role_prompt(self._flow_dir, candidate, variables)
            except RoleFileError:
                continue  # missing/bad/traversing → next candidate (validator rejects traversal)
        return fallback

    def _write_summary_json(
        self,
        task_dir: Path,
        task_id: str,
        task_title: str,
        summary_text: str | None,
        follow_ups: tuple[FollowUp, ...] = (),
    ) -> None:
        """Write the local-only ``summary.json`` metadata (never committed).

        Carries the evidence-gated ``follow_ups`` (empty unless the flow opted in) so the debt
        signal is machine-readable beside the prose summary, and ``supervisor_usage`` — what this
        oversight layer cost, in total and per job. The spend belongs here rather than in
        ``summary.md`` because that file becomes the pull-request body: telemetry is for the
        operator who owns the bill, not for the reviewer reading the change, and it should not
        travel to the remote alongside it.

        Written late in the finalize sequence, so the report already includes the finalize turn's
        own attempt rows rather than every call but the most expensive one.

        A *failed* finalize (empty ``summary_text``) must NOT clobber an existing non-empty
        ``summary.json`` with a blank one — symmetric to leaving ``summary.md`` untouched on a
        failure. So an empty summary is a no-op when a prior non-empty one is on disk (e.g. a rerun
        whose finalize turn failed after the first attempt succeeded)."""
        path = task_dir / "summary.json"
        if not summary_text and self._existing_summary_nonempty(path):
            return
        payload: dict[str, Any] = {"what": task_title, "summary": summary_text or ""}
        usage = self._supervisor_usage(task_id)
        if usage is not None:
            payload["supervisor_usage"] = usage
        if follow_ups:
            payload["follow_ups"] = [follow_up_json(fu) for fu in follow_ups]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError:
            return
        self._register(task_id, "summary_json", str(path))

    def _supervisor_usage(self, task_id: str) -> dict[str, Any] | None:
        """This layer's own spend for the task, or ``None`` when it made no provider calls.

        Best-effort like the rest of the layer: a store error costs the report, never the summary.
        """
        try:
            return summarize_spend(self._store.get_provider_attempts_for_task(task_id))
        except Exception as exc:
            _LOG.warning(
                "supervisor usage report could not be built (advisory, ignored)",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
            )
            return None

    @staticmethod
    def _existing_summary_nonempty(path: Path) -> bool:
        """Whether ``summary.json`` at *path* already has a non-empty ``summary``."""
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(isinstance(existing, dict) and str(existing.get("summary", "")).strip())

    def _register(self, task_id: str, kind: str, path: str) -> None:
        if self._register_artifact is not None:
            self._register_artifact(task_id, kind, path)
