"""Constant supervisor layer above any flow (flow-engine P2.1).

The supervisor is **not** a graph node — it is an orchestrator-level oversight layer that exists for
every task under any flow shape (even a single implement agent with no checks/review). It starts at
task start, lives the whole cycle, observes each completed step read-only through its **own**
``resume_own_lineage`` session (≈one LLM call/step, accumulating context across steps), and at
whole-task close synthesizes the ``summary`` + advisory caveats.

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
from wastech_orchestrator.core.flow.nodes.base import RegisterArtifact, RouterPort
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
from wastech_orchestrator.core.skills import SkillInventory
from wastech_orchestrator.memory.delta import DELTA_OUTPUT_SCHEMA, CandidateDelta, parse_delta
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    NormalizedUsage,
    ProviderId,
    build_effective_prompt,
)
from wastech_orchestrator.providers.exchange import assert_orchestration_paths_contained
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import EvaluationRow, NodeLineageRow, ProviderAttemptRow

_LOG = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Default wall-clock for the supervisor's ``provider_attempts`` timestamps (UTC ISO)."""
    return datetime.now(UTC).isoformat()


# The supervisor's read-only requests carry a dedicated ``supervisor`` node identity (audit dir /
# route label); it is not a graph node, so it records ``evaluations`` rows, never ``node_runs``.
_SUPERVISOR_IDENTITY = "supervisor"

# The reserved ``node_lineage`` key under which the supervisor's own durable session lives. It is a
# double-underscore sentinel, distinct from the routing identity above, so it can never collide with
# a real flow node id (an operator could legally name an evaluator node ``supervisor``).
_SUPERVISOR_LINEAGE_NODE_ID = "__supervisor__"

# The ``node_run_id`` namespacing the upfront skill-map proposal's artifact dir
# (``stages/supervisor/run-NNNNNN/``). Distinct from finalize's ``0`` and from any real (positive,
# small, autoincrement) node-run id, so the three turn kinds never collide on the same path.
_PROPOSAL_RUN_ID = 999_999

# WRI-011: skill descriptions are untrusted ``SKILL.md`` frontmatter, so the inlined proposal
# metadata is bounded to this many characters (name/path are identifier-sized and pass unbounded);
# the full skill package is only read from the frozen exchange after the Core accepts the proposal.
_SKILL_DESCRIPTION_INLINE_CAP = 200


def _bounded_description(description: str) -> str:
    """Truncate an untrusted skill description to the recorded inline cap for the proposal."""
    text = description.strip()
    if len(text) <= _SKILL_DESCRIPTION_INLINE_CAP:
        return text
    return text[: _SKILL_DESCRIPTION_INLINE_CAP - 1].rstrip() + "…"


# Base for the subtask-boundary handoff turns' artifact-dir namespace (subtask-context-handoff ADR).
# Each handoff uses ``_HANDOFF_RUN_ID_BASE + subtask_order`` so multiple handoffs in one task (a
# chain, or a diamond) write to DISTINCT dirs — ``create_attempt_dir`` forbids overwriting
# (``exist_ok=False``), so a shared id would make the second boundary's turn raise and silently
# degrade to the floor alone. The base is distinct from the proposal (999999) and finalize (0), and
# far above any real (small autoincrement) node-run id.
_HANDOFF_RUN_ID_BASE = 990_000

# The strict provider schema for the once-per-task ``node → skills`` proposal. Array-shaped (not an
# arbitrary-key object) so it validates under strict JSON-schema: each assignment names one node and
# its proposed skill tokens. The Core resolves the tokens against the discovered inventory.
_SKILL_MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node": {"type": "string", "minLength": 1},
                    "skills": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 512},
                    },
                },
                "required": ["node", "skills"],
            },
        }
    },
    "required": ["assignments"],
}


# The evidence-gated ``follow_ups`` schema (task 1). Hardcoded in code — a flow author reshapes the
# supervisor's *wording* via its prompt files, but never the machine contract the orchestrator
# parses. Each record is minimal and grounded: an unsupported "refactor idea" carries no evidence
# and is dropped by :func:`parse_follow_ups`.
# Nullable array root (``["array", "null"]``) + full ``required`` so this validates under OpenAI
# strict mode when nested as the finalize turn's ``follow_ups`` (F41): no follow-ups → ``null``.
# Formerly-optional ``paths``/``action_hint`` are nullable so the model may still omit them;
# :func:`parse_follow_ups` treats ``null`` identically to an absent key.
_FOLLOW_UPS_SCHEMA: dict[str, Any] = {
    "type": ["array", "null"],
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "rationale": {"type": "string"},
            "paths": {"type": ["array", "null"], "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "action_hint": {"type": ["string", "null"]},
        },
        "required": ["title", "rationale", "paths", "evidence", "severity", "action_hint"],
    },
}

# The intra-task subtask handoff brief's structured schema (subtask-context-handoff ADR). Three
# sections; hardcoded in code (a flow reshapes wording via ``handoff_role_file``, never the
# contract). All optional — a thin boundary may yield only one useful section.
# OpenAI strict mode requires every ``properties`` key in ``required`` (F41); the three sections
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
    "advisory caveats or follow-ups you noted across the steps."
)
_BUILTIN_HANDOFF = (
    "You are a read-only supervisor briefing the next subtask in a decomposed task. Do not edit "
    "code. You have observed the predecessor subtask(s); write a focused handoff for the agent "
    "about to implement the successor."
)


# F7b: the max reasoning tiers make a structured-output turn fragile — at ``xhigh`` the provider
# spends the turn on thinking and fails to emit a valid tool call under the schema (observed:
# ``error_max_structured_output_retries``), while the same free-text turn passes. A target-only
# summary/proposal does not need max reasoning, so schema turns are capped to ``high`` — the free-
# text observe turns keep the configured tier. Deterministic (no reliance on provider error
# classification), and it keeps the default from being brittle out of the box (relates to F2).
_MAX_REASONING_TIERS = frozenset({"xhigh", "max"})
_SCHEMA_REASONING_CAP = "high"


def _schema_safe_reasoning(
    reasoning: str | None, output_schema: dict[str, Any] | None
) -> str | None:
    """Cap a structured-output turn's reasoning to ``high`` when configured at a max tier (F7b).

    Free-text turns (``output_schema is None``) keep the configured value unchanged.
    """
    if output_schema is None or reasoning is None:
        return reasoning
    return _SCHEMA_REASONING_CAP if reasoning in _MAX_REASONING_TIERS else reasoning


def _finalize_schema(*, with_delta: bool, with_follow_ups: bool) -> dict[str, Any]:
    """Build the finalize turn's structured schema for the enabled outputs (all on one turn).

    ``summary`` is always required; ``memory_delta`` is added when memory is enabled (AC-W1) and
    ``follow_ups`` when the flow opted in (``supervisor.emit_follow_ups``). When neither is enabled
    the caller runs a free-text turn instead (today's behavior — AC-S4), so this is never called."""
    properties: dict[str, Any] = {
        "summary": {
            "type": "string",
            "minLength": 1,
            # F10: without guidance the model packs the whole synthesis into one flat line (0 `\n`)
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
        # OpenAI strict mode (F41): ``required`` must list every present key. ``memory_delta`` and
        # ``follow_ups`` are nullable at their roots, so requiring them still lets the model emit
        # ``null`` when there is nothing to record.
        "required": list(properties),
    }


@dataclass(frozen=True)
class FollowUp:
    """One evidence-gated technical-debt / follow-up record (task 1). Minimal and grounded."""

    title: str
    rationale: str
    severity: str
    evidence: tuple[str, ...]
    paths: tuple[str, ...] = ()
    action_hint: str | None = None


def parse_follow_ups(raw: Any) -> tuple[FollowUp, ...]:
    """Parse the finalize turn's ``follow_ups`` array defensively — **evidence-gated**.

    Best-effort, mirroring :func:`_parse_skill_map`: a non-list yields ``()`` and any record without
    a non-empty ``title`` or ``evidence`` is dropped (never raised), so an ungrounded "refactor
    idea" the model invented cannot reach ``summary.{json,md}``.
    """
    if not isinstance(raw, list):
        return ()
    out: list[FollowUp] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        evidence = item.get("evidence")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(evidence, list):
            continue
        ev = tuple(e.strip() for e in evidence if isinstance(e, str) and e.strip())
        if not ev:  # evidence-gated: no evidence → dropped
            continue
        rationale = item.get("rationale")
        severity = item.get("severity")
        paths = item.get("paths")
        action_hint = item.get("action_hint")
        out.append(
            FollowUp(
                title=title.strip(),
                rationale=rationale if isinstance(rationale, str) else "",
                severity=severity if severity in ("low", "medium", "high") else "medium",
                evidence=ev,
                paths=tuple(p.strip() for p in paths if isinstance(p, str) and p.strip())
                if isinstance(paths, list)
                else (),
                action_hint=action_hint.strip()
                if isinstance(action_hint, str) and action_hint.strip()
                else None,
            )
        )
    return tuple(out)


def _render_follow_ups_section(follow_ups: tuple[FollowUp, ...]) -> str:
    """Render the ``## Technical debt / follow-ups`` section appended to ``summary.md``."""
    lines = ["## Technical debt / follow-ups", ""]
    for fu in follow_ups:
        parts = [f"- **[{fu.severity}] {fu.title}**"]
        if fu.rationale:
            parts.append(f" — {fu.rationale}")
        if fu.paths:
            parts.append(f" Paths: {', '.join(fu.paths)}.")
        if fu.action_hint:
            parts.append(f" Suggested: {fu.action_hint}")
        lines.append("".join(parts))
    return "\n".join(lines) + "\n"


# Longest a finding's reason may be before it is used verbatim as a follow-up title; longer reasons
# become a truncated title with the full text carried in the rationale (VF-18).
_FINDING_TITLE_MAX = 120


def _finding_to_follow_up(finding: Any, node_id: str | None) -> FollowUp | None:
    """Map one persisted evaluator finding (``{severity, reason, paths}``) to a :class:`FollowUp`,
    or ``None`` when it carries no usable ``reason`` (VF-18)."""
    if not isinstance(finding, Mapping):
        return None
    reason = str(finding.get("reason") or "").strip()
    if not reason:
        return None
    severity = finding.get("severity")
    paths_raw = finding.get("paths")
    paths = (
        tuple(str(p).strip() for p in paths_raw if str(p).strip())
        if isinstance(paths_raw, list)
        else ()
    )
    if len(reason) <= _FINDING_TITLE_MAX:
        title, rationale = reason, ""
    else:  # keep the bold title a label; the full text still reaches the operator via the rationale
        title, rationale = reason[:_FINDING_TITLE_MAX].rstrip() + "…", reason
    return FollowUp(
        title=title,
        rationale=rationale,
        severity=severity if severity in ("low", "medium", "high") else "medium",
        evidence=(f"{node_id or 'review'} evaluator finding (accepted with findings)",),
        paths=paths,
    )


def _evaluator_finding_follow_ups(evaluations: list[EvaluationRow]) -> tuple[FollowUp, ...]:
    """Derive follow-ups from the LAST in-flow verdict per evaluator node (VF-18).

    An evaluator that accepts *with* findings persists them to the ``evaluations`` table and they
    otherwise reach no operator surface. Take each evaluator node's final verdict only (earlier,
    rework-superseded rounds are ignored) and convert its findings so they land in ``summary.{json,
    md}`` and the PR body. ``get_evaluations`` is insertion-ordered, so the last row seen per
    ``node_id`` is that node's final verdict.
    """
    last_by_node: dict[str | None, EvaluationRow] = {}
    for row in evaluations:
        if row.kind == "in_flow_verdict":
            last_by_node[row.node_id] = row
    out: list[FollowUp] = []
    for node_id, row in last_by_node.items():
        try:
            findings = json.loads(row.findings_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(findings, list):
            continue
        for finding in findings:
            follow_up = _finding_to_follow_up(finding, node_id)
            if follow_up is not None:
                out.append(follow_up)
    return tuple(out)


def _follow_up_key(follow_up: FollowUp) -> tuple[str, tuple[str, ...]]:
    """Exact-match dedup key for a follow-up: its normalized text plus its paths (VF-18)."""
    text = " ".join(f"{follow_up.title} {follow_up.rationale}".lower().split())
    return (text, tuple(sorted(follow_up.paths)))


def _merge_follow_ups(
    primary: tuple[FollowUp, ...], extra: tuple[FollowUp, ...]
) -> tuple[FollowUp, ...]:
    """Append *extra* follow-ups whose exact-match key is not already in *primary* (VF-18).

    *primary* (the supervisor's own list) wins on a collision, so an evaluator finding the
    supervisor already reported is not duplicated; *extra* is also deduped against itself.
    """
    seen = {_follow_up_key(fu) for fu in primary}
    merged = list(primary)
    for follow_up in extra:
        key = _follow_up_key(follow_up)
        if key in seen:
            continue
        seen.add(key)
        merged.append(follow_up)
    return tuple(merged)


# F16: pseudo-tags a model sometimes emits instead of a clean tool call — the whole
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


def _sanitize_summary(summary_text: str) -> str:
    """Strip a leaked structured dump from a finalize ``summary`` (F16).

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


def _parse_skill_map(structured: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """Parse the proposal's structured output into ``{node_id: (token, ...)}`` — defensively.

    Best-effort to match the advisory contract: a malformed payload yields ``{}`` and a malformed
    assignment is skipped, never raised. The Core still validates every token against the inventory,
    so a bad proposal can at worst contribute nothing.
    """
    if not isinstance(structured, Mapping):
        return {}
    assignments = structured.get("assignments")
    if not isinstance(assignments, list):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for item in assignments:
        if not isinstance(item, Mapping):
            continue
        node = item.get("node")
        skills = item.get("skills")
        if not isinstance(node, str) or not node.strip() or not isinstance(skills, list):
            continue
        tokens = tuple(s.strip() for s in skills if isinstance(s, str) and s.strip())
        if tokens:
            out[node.strip()] = tokens
    return out


class SupervisorStorePort(Protocol):
    """The slice of the state store the supervisor needs: the immutable evaluation row, the durable
    own-session lineage (read on first use, written after each turn), and the per-turn
    provider-attempt audit row (VF-8 — the supervisor's own billable calls)."""

    def record_evaluation(self, row: EvaluationRow) -> int: ...

    def get_evaluations(self, task_id: str) -> list[EvaluationRow]: ...

    def get_node_lineage(
        self, task_id: str, node_id: str, subtask_order: int | None = None
    ) -> NodeLineageRow | None: ...

    def upsert_node_lineage(self, row: NodeLineageRow) -> None: ...

    def record_provider_attempt(self, attempt: ProviderAttemptRow) -> None: ...


class Supervisor:
    """The per-task constant oversight layer. One instance per task (it carries its own session)."""

    def __init__(
        self,
        *,
        settings: SupervisorConfig,
        router: RouterPort,
        store: SupervisorStorePort,
        repo_dir: str,
        artifacts_root: str | Path,
        flow_dir: Path,
        exchange_root: str | Path = "",
        flow_supervisor: SupervisorBlock | None = None,
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
        # Wall-clock for the per-turn ``provider_attempts`` timestamps (VF-8); the orchestrator
        # threads its own so a run's audit timestamps share one source.
        self._clock = clock
        self._repo_dir = repo_dir
        self._artifacts_root = artifacts_root
        # The provider-readable exchange root ``<repo>/.worc-io`` (WRI-001); the supervisor's own
        # provider call passes the same pre-launch containment gate as agent/evaluator (Part C).
        self._exchange_root = exchange_root
        self._flow_dir = flow_dir
        # Flow-local supervisor prompt overrides + the follow-ups opt-in (prompt-and-supervisor
        # ADR). ``None`` → the global config prompt + built-in finalize, free-text finalize (today).
        self._flow_role_file = flow_supervisor.role_file if flow_supervisor else None
        self._flow_finalize_role_file = (
            flow_supervisor.finalize_role_file if flow_supervisor else None
        )
        self._flow_handoff_role_file = (
            flow_supervisor.handoff_role_file if flow_supervisor else None
        )
        self._emit_follow_ups = flow_supervisor.emit_follow_ups if flow_supervisor else False
        self._register_artifact = register_artifact
        self._prompt_audit = prompt_audit
        self._prompt_secrets = prompt_secrets
        self._default_timeout_seconds = default_timeout_seconds
        # VF-7 defense-in-depth: the Core-owned orchestrator security contract prepended to the
        # supervisor's own read-only turn (advisory, NOT enforcement). Resolved once by the
        # orchestrator, like the graph-node NodeServices carrier. ``None`` → no preamble.
        self._security_preamble = security_preamble
        # The supervisor's own session (resume_own_lineage). Held in-memory within a process run and
        # persisted to / hydrated from ``node_lineage`` so it survives a restart (independent of the
        # editing-lineage authors). ``None`` until the first turn runs or the persisted row is read.
        self._own_session_id: str | None = None
        # Whether a supervisor turn actually *succeeded this process* (distinct from
        # ``_own_session_id``, which ``_resume_session`` sets to the stale persisted id *before* the
        # turn — so it is non-None even after a failed resume). Gates finalize's session-vs-digest
        # choice: a warm live session synthesizes normally; otherwise finalize reseeds from the
        # recorded ``supervisor_step`` observations rather than resuming a possibly-dead session.
        self._session_live: bool = False

    # -- per-step observation --------------------------------------------------

    def observe(
        self,
        *,
        task_id: str,
        node_id: str,
        node_run_id: int,
        outcome_kind: str,
        final_message: str | None = None,
        subtask_order: int | None = None,
    ) -> None:
        """Observe one completed step read-only and record an advisory ``supervisor_step`` row.

        Best-effort: a failed observation is logged and swallowed — it is advisory and must never
        fail or reroute the task. Namespaced by ``source_node_run_id`` (the step), so a resumed run
        does not duplicate observations.
        """
        prompt = self._step_prompt(task_id, node_id, outcome_kind, final_message)
        # F50: observation is advisory and runs once per node-run, so a deep fix loop drives many
        # observe turns; it never needs a max reasoning tier, so cap it to `high` (the whole-task
        # finalize keeps the configured tier).
        note = self._run(
            task_id, prompt, node_run_id=node_run_id, subtask=subtask_order, cap_reasoning=True
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
        same turn yields the evidence-gated ``follow_ups`` array — zero extra LLM calls (AC-W1).
        When neither is enabled the turn is free-text, exactly today's behavior (AC-S4).

        Best-effort: a turn that cannot run yields no ``summary.md`` (the orchestrator's
        deterministic minimal summary then applies), a ``None`` delta, and no follow-ups.
        ``summary.json`` is always written. Returns the summary path + the delta + the follow-ups.

        On a revived task where no supervisor turn succeeded this process (the durable session is
        gone / unresumable), we do **not** gamble on resuming that dead session: finalize reseeds a
        single fresh turn from the ``supervisor_step`` observations already recorded in
        ``state.db``. Same one finalize turn, different input — the budget contract is unchanged.
        """
        # ``node_run_id=0`` is the once-per-task finalize sentinel; per-step observations use the
        # observed step's id, so each supervisor turn writes a distinct artifact dir (no collision).
        warm = self._session_live
        digest = None if warm else self._finalize_digest(task_id)
        if not warm:
            _LOG.info(
                "finalize re-synthesized from recorded observations (session unavailable)",
                extra={"task_id": task_id, "have_digest": digest is not None},
            )
        # ``task_title`` is used only for the orchestrator-side PR-body H1 / summary.json below (not
        # a provider prompt surface); the finalize *prompt* reads the task from the frozen exchange
        # packet path (WRI-011 — no inline task body/title reaches the provider).
        summary_text, delta, follow_ups = self._finalize_turn(
            task_id,
            emit_delta,
            digest=digest,
            resume=warm,
            task_path=task_path,
        )
        # VF-18: an evaluator that accepts *with* findings (sub-threshold, non-gating) persists them
        # to the evaluations table and they otherwise reach no operator surface. Merge them into the
        # follow-ups — deduped against the supervisor's own list — so they land in summary.{json,md}
        # and the PR body. Runs independent of ``emit_follow_ups`` (a distinct, evidence-bearing
        # source from the supervisor's LLM-authored follow-ups).
        follow_ups = _merge_follow_ups(
            follow_ups, _evaluator_finding_follow_ups(self._store.get_evaluations(task_id))
        )
        self._record(
            task_id,
            kind="supervisor_final",
            source_node_run_id=None,
            subtask_order=None,
            payload={
                "summary_written": summary_text is not None,
                "memory_delta": delta is not None,
                "follow_ups": len(follow_ups),
                "recovered_from_digest": not warm,
            },
        )
        # F16: a model sometimes emits its structured output as a `<summary>…</summary>
        # <follow_ups>[JSON]</follow_ups><memory_delta>…` text dump instead of a clean tool call.
        # Sanitize so only the human prose reaches summary.md (the PR body) — never a raw
        # follow_ups/memory_delta/lessons dump.
        clean_summary = _sanitize_summary(summary_text) if summary_text else ""
        task_dir = Path(task_artifact_dir(self._artifacts_root, task_id))
        self._write_summary_json(task_dir, task_id, task_title, clean_summary or None, follow_ups)
        if not clean_summary:
            return FinalizeResult(summary_path=None, candidate_delta=delta, follow_ups=follow_ups)
        md_path = task_dir / "summary.md"
        # F10: prefix a deterministic H1 so the PR body is never a headless paragraph-slab — unless
        # the model already opened with its own top-level heading.
        if clean_summary.startswith("# "):
            body = clean_summary
        else:
            body = f"# {task_title}\n\n{clean_summary}"
        body = body.rstrip("\n") + "\n"
        if follow_ups:  # surface the evidence-gated debt/follow-ups as a section in the PR body
            body += "\n" + _render_follow_ups_section(follow_ups)
        md_path.write_text(body, encoding="utf-8")
        self._register(task_id, "summary_md", str(md_path))
        return FinalizeResult(summary_path=md_path, candidate_delta=delta, follow_ups=follow_ups)

    def _finalize_turn(
        self,
        task_id: str,
        emit_delta: bool,
        *,
        digest: str | None = None,
        resume: bool = True,
        task_path: str | None = None,
    ) -> tuple[str | None, CandidateDelta | None, tuple[FollowUp, ...]]:
        """Run the single finalize turn. Free-text when neither memory nor follow-ups are enabled
        (today's behavior — AC-S4); otherwise a structured ``{summary, ...}`` turn, so every enabled
        output (``memory_delta`` / ``follow_ups``) rides one turn (AC-W1 — no extra LLM call).

        ``digest`` (recovered ``supervisor_step`` observations) and ``resume=False`` are set
        together on the revive path: the turn synthesizes from the digest on a fresh session rather
        than resuming a dead one."""
        with_follow_ups = self._emit_follow_ups
        if not emit_delta and not with_follow_ups:
            text = self._run(
                task_id,
                self._finalize_prompt(task_id, digest=digest),
                node_run_id=0,
                resume_session=resume,
                task_path=task_path,
            )
            return text, None, ()
        result = self._run_result(
            task_id,
            self._finalize_prompt(
                task_id,
                with_delta=emit_delta,
                with_follow_ups=with_follow_ups,
                digest=digest,
            ),
            node_run_id=0,
            output_schema=_finalize_schema(with_delta=emit_delta, with_follow_ups=with_follow_ups),
            resume_session=resume,
            task_path=task_path,
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

    def _finalize_digest(self, task_id: str) -> str | None:
        """Reconstruct the finalize input from recorded ``supervisor_step`` observations.

        The per-step notes are immutable append-only rows in ``evaluations`` (``state.db``) — always
        present on a revived task, independent of the durable session and of logging config. Renders
        the usable notes as compact ``- [node → outcome] note`` lines, skipping the ones that failed
        to run or added nothing (empty note). Returns ``None`` when nothing usable was recorded (the
        turn then runs unseeded — same as today's fresh-session finalize)."""
        lines: list[str] = []
        for row in self._store.get_evaluations(task_id):
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

    # -- intra-task subtask handoff --------------------------------------------

    def handoff(self, *, task_id: str, subtask_order: int, floor_context: str) -> str | None:
        """Emit the interpretive handoff brief for a subtask boundary (subtask-context-handoff ADR).

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
            subtask=subtask_order,
            output_schema=_HANDOFF_SCHEMA,
        )
        if result is None or result.structured_output is None:
            return None
        return _render_handoff_brief(result.structured_output)

    # -- upfront skill-map proposal --------------------------------------------

    def propose_skill_map(
        self,
        *,
        task_id: str,
        agent_node_ids: Sequence[str],
        inventory: SkillInventory,
        task_path: str | None = None,
    ) -> dict[str, tuple[str, ...]]:
        """Propose a ``node → skills`` map once per task (read-only, propose-only — Core decides).

        The supervisor's first turn, on its own durable session, so later per-step observations and
        the whole-task summary inherit its reasoning about which skills it chose. Skipped (``{}``)
        when the inventory is empty — a repo with no skills pays nothing. Best-effort by contract:
        any failure (no provider, infra error, malformed output) is logged and yields ``{}``, and
        the run continues on operator pins alone. The supervisor only *proposes* tokens; the
        orchestrator resolves them against the inventory and merges them with the static pins.

        WRI-011: the task body is **never** inlined here — the proposal reads it from the frozen
        exchange packet (``task_path``, rendered in the context footer); only bounded, allowlisted
        skill metadata (name/path + a length-bounded description) reaches the prompt.
        """
        if not inventory.skills:
            return {}
        prompt = self._proposal_prompt(task_id, agent_node_ids, inventory)
        result = self._run_result(
            task_id,
            prompt,
            node_run_id=_PROPOSAL_RUN_ID,
            output_schema=_SKILL_MAP_SCHEMA,
            task_path=task_path,
        )
        proposal = _parse_skill_map(result.structured_output) if result is not None else {}
        self._record(
            task_id,
            kind="supervisor_skill_proposal",
            source_node_run_id=None,
            subtask_order=None,
            payload={
                "assignments": {node: list(skills) for node, skills in proposal.items()},
                "proposal_failed": result is None,
            },
        )
        return proposal

    # -- internals -------------------------------------------------------------

    def _run(
        self,
        task_id: str,
        prompt: str,
        *,
        node_run_id: int,
        subtask: int | None = None,
        resume_session: bool = True,
        cap_reasoning: bool = False,
        task_path: str | None = None,
    ) -> str | None:
        """Run one read-only supervisor turn and return its final message (``None`` on failure)."""
        result = self._run_result(
            task_id,
            prompt,
            node_run_id=node_run_id,
            subtask=subtask,
            resume_session=resume_session,
            cap_reasoning=cap_reasoning,
            task_path=task_path,
        )
        return result.final_message if result is not None else None

    def _run_result(
        self,
        task_id: str,
        prompt: str,
        *,
        node_run_id: int,
        subtask: int | None = None,
        output_schema: dict[str, Any] | None = None,
        resume_session: bool = True,
        cap_reasoning: bool = False,
        task_path: str | None = None,
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

        ``resume_session=False`` starts a fresh session (no ``session_id``): finalize uses it on a
        revived task, where resuming the possibly-dead persisted session is the exact failure mode —
        it reseeds from the recorded observations instead (the digest rides in the prompt).
        """
        try:
            route = self._router.resolve_route(_SUPERVISOR_IDENTITY, self._settings.provider)
            # Cap to `high` for a schema turn (F7b, fragile at max) OR an advisory observe turn
            # (F50, cost-heavy in a deep loop); a free-text finalize keeps the configured tier.
            reasoning = _schema_safe_reasoning(self._settings.reasoning, output_schema)
            if cap_reasoning and reasoning in _MAX_REASONING_TIERS:
                reasoning = _SCHEMA_REASONING_CAP
            # The session to resume + its persisted cumulative usage baseline (VF-8): a resumed
            # Codex session counts cumulatively, so the per-turn ``provider_attempts`` usage is a
            # summation-safe delta against the previous cumulative — exactly like a graph-node
            # lineage. ``resume_session=False`` (finalize's revive path) starts fresh, no baseline.
            resume_id, usage_baseline, baseline_session_id = (
                self._resume_context(task_id, route) if resume_session else (None, None, None)
            )
            request = AgentRunRequest(
                task_id=task_id,
                node_id=_SUPERVISOR_IDENTITY,
                working_directory=self._repo_dir,
                prompt=prompt,
                permission_profile="read-only",  # forced — the supervisor never writes
                timeout_seconds=self._default_timeout_seconds,
                attempt=1,
                node_run_id=node_run_id,  # not a graph node; audit lives in ``evaluations``
                model=self._settings.model,
                reasoning=reasoning,
                output_schema=output_schema,
                session_id=resume_id,
                # WRI-011: the task reaches the supervisor as the frozen exchange packet path (never
                # inline title/description). Repository instructions are NOT injected (VF-5) — the
                # supervisor's read-only turn reads the repo's root files itself, like graph nodes.
                task_path=task_path,
                # VF-7 defense-in-depth: the Core-owned orchestrator security contract (advisory).
                security_preamble=self._security_preamble,
            )
            # Same pre-launch containment invariant as agent/evaluator (WRI-001): the supervisor
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
        # VF-8: give the supervisor's own billable provider calls an audit home in
        # ``provider_attempts`` (``node_run_id`` NULL — it is not a graph node), so a task-level
        # cost/usage roll-up is complete. Recorded for every outcome (including a failed turn's
        # attempts) and BEFORE the result-None early return, so no billable call is dropped.
        self._record_provider_attempts(task_id, outcome, usage_baseline, baseline_session_id)
        result = outcome.result
        if result is None:
            return None
        if result.session_id:
            self._own_session_id = result.session_id  # resume_own_lineage continuity (in-memory)
            self._session_live = True  # a turn succeeded this process — the session is usable
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
        :meth:`_record_provider_attempts` with ``node_run_id`` NULL (VF-8), so that synthetic id
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
                    model=self._settings.model,
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
        usage_baseline: NormalizedUsage | None,
        baseline_session_id: str | None,
    ) -> None:
        """Persist the supervisor's own ``provider_attempts`` rows (VF-8) — ``node_run_id`` NULL.

        Reuses the shared node-path recorder, so the supervisor's per-run usage delta is computed
        exactly like a graph node's (against its own resumed-session baseline). Best-effort like the
        rest of this advisory layer: any store error is logged and swallowed so an audit-write
        failure can never surface as a broken turn.
        """
        try:
            record_provider_attempts(
                self._store,
                self._clock,
                task_id=task_id,
                node_run_id=None,  # the supervisor is a constant layer, not a graph node
                outcome=outcome,
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
        The usage baseline is that session's persisted running cumulative snapshot (VF-8): it is
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

        Also carries the session's running cumulative usage snapshot (VF-8), so the next turn's
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
        self, task_id: str, node_id: str, outcome_kind: str, final_message: str | None
    ) -> str:
        observed = f"## Step observed\nNode: {node_id}\nOutcome: {outcome_kind}\n"
        if final_message:
            observed += f"\nThe step reported:\n{final_message}\n"
        return self._base_prompt(task_id) + "\n\n" + observed

    def _proposal_prompt(
        self,
        task_id: str,
        agent_node_ids: Sequence[str],
        inventory: SkillInventory,
    ) -> str:
        nodes = "\n".join(f"- {nid}" for nid in agent_node_ids) or "- (none)"
        # Skill descriptions come from untrusted repository ``SKILL.md`` frontmatter, so they are
        # bounded to a recorded cap here (name/path are identifier-sized); the full skill package is
        # read from the frozen exchange only after the Core accepts the proposal.
        skills = "\n".join(
            f"- {s.name} — {_bounded_description(s.description)} [{s.path}]"
            for s in inventory.skills
        )
        return (
            self._base_prompt(task_id)
            + "\n\n## Skill map proposal\n"
            + "Before any step runs, propose which repo skills (if any) each flow node below "
            + "should receive. This is read-only and propose-only — you do not route or edit; the "
            + "Core decides which proposals it accepts and resolves each against the real "
            + "inventory. Address a skill by its name; if a name is shared by more than one skill, "
            + "use the repo-relative path shown in [brackets]. Propose a skill only when it is "
            + "clearly relevant to that node's job, and do not propose orchestrator gate skills "
            + "(run-checks / test / sync-docs and the like). Return assignments only for the nodes "
            + "that need skills; omit the rest.\n\n"
            + f"### Flow nodes (agent)\n{nodes}\n\n"
            + f"### Available skills\n{skills}\n\n"
            + "### Task\nThe task specification is provided as the task packet referenced in the "
            + "context below — read it there; it is not inlined here.\n"
        )

    def _finalize_prompt(
        self,
        task_id: str,
        *,
        with_delta: bool = False,
        with_follow_ups: bool = False,
        digest: str | None = None,
    ) -> str:
        # The finalize lens (flow ``finalize_role_file`` → built-in) carries the summary emphasis;
        # only the machine-contract additions (task context, follow-ups, memory delta) are appended
        # in code, so a flow author reshapes wording but never the parsed schema. WRI-011: the task
        # reaches the turn as the frozen exchange packet (context footer), never inline title/body.
        prompt = (
            self._finalize_base(task_id)
            + "\n\n## Task under review\nThe task specification is provided as the task packet "
            + "referenced in the context below — read it there; it is not inlined here.\n"
        )
        if digest:
            # Revive path: the working session that accumulated per-step context is gone, so seed
            # the synthesis from the recorded observations instead of relying on session memory.
            prompt += (
                "\n## Recovered step observations\n"
                "Your own working session for this task is unavailable (the task was resumed from "
                "a checkpoint), so synthesize the summary from these recorded per-step "
                "observations rather than from session memory:\n\n"
                f"{digest}\n"
            )
        if with_follow_ups:
            prompt += (
                "\n## Technical debt / follow-ups\n"
                "Also record concrete technical debt and refactor follow-ups you observed, as the "
                "structured `follow_ups` array. Each record is minimal and **evidence-gated**: a "
                "`title`, a short `rationale`, the `paths` it concerns, `evidence` pointers "
                "(files/lines/commits/checks that substantiate it), a `severity` "
                "(low/medium/high), and an optional `action_hint`. Propose only debt grounded in "
                "what actually happened this run — never speculative ideas. Leave the array empty "
                "when nothing qualifies; a record without evidence is dropped.\n"
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
        to the next, so the supervisor's per-step observation and skill proposal never break."""
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
        signal is machine-readable beside the prose summary.

        F16: a *failed* finalize (empty ``summary_text``) must NOT clobber an existing non-empty
        ``summary.json`` with a blank one — symmetric to leaving ``summary.md`` untouched on a
        failure. So an empty summary is a no-op when a prior non-empty one is on disk (e.g. a rerun
        whose finalize turn failed after the first attempt succeeded)."""
        path = task_dir / "summary.json"
        if not summary_text and self._existing_summary_nonempty(path):
            return
        payload: dict[str, Any] = {"what": task_title, "summary": summary_text or ""}
        if follow_ups:
            payload["follow_ups"] = [
                {
                    "title": fu.title,
                    "rationale": fu.rationale,
                    "severity": fu.severity,
                    "paths": list(fu.paths),
                    "evidence": list(fu.evidence),
                    "action_hint": fu.action_hint,
                }
                for fu in follow_ups
            ]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError:
            return
        self._register(task_id, "summary_json", str(path))

    @staticmethod
    def _existing_summary_nonempty(path: Path) -> bool:
        """Whether ``summary.json`` at *path* already has a non-empty ``summary`` (F16 guard)."""
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(isinstance(existing, dict) and str(existing.get("summary", "")).strip())

    def _register(self, task_id: str, kind: str, path: str) -> None:
        if self._register_artifact is not None:
            self._register_artifact(task_id, kind, path)
