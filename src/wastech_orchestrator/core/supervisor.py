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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from wastech_orchestrator.config.schema import SupervisorConfig
from wastech_orchestrator.core.flow.nodes.base import RegisterArtifact, RouterPort
from wastech_orchestrator.core.flow.prompt import RoleFileError, render_role_prompt
from wastech_orchestrator.core.flow.schema import SupervisorBlock
from wastech_orchestrator.core.skills import SkillInventory
from wastech_orchestrator.memory.delta import DELTA_OUTPUT_SCHEMA, CandidateDelta, parse_delta
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import AgentRunRequest, AgentRunResult, ProviderId
from wastech_orchestrator.routing.router import ResolvedRoute
from wastech_orchestrator.state_store import EvaluationRow, NodeLineageRow

_LOG = logging.getLogger(__name__)

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
_FOLLOW_UPS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "rationale": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "action_hint": {"type": "string"},
        },
        "required": ["title", "rationale", "evidence", "severity"],
    },
}

# Built-in supervisor prompt text — the last fallback in each chain, so a flow with no prompt files
# (and no global config prompt, for finalize) still runs exactly as before.
_BUILTIN_OBSERVE = "You are a read-only supervisor observing a software task. Do not edit code."
_BUILTIN_FINALIZE = (
    "You are a read-only supervisor closing out a software task. Do not edit code.\n\n"
    "Synthesize a plain-language summary of the whole task: what was done, how it works, how it "
    "integrates, and why, grounded in the actual committed change. In a closing section list any "
    "advisory caveats or follow-ups you noted across the steps."
)


def _finalize_schema(*, with_delta: bool, with_follow_ups: bool) -> dict[str, Any]:
    """Build the finalize turn's structured schema for the enabled outputs (all on one turn).

    ``summary`` is always required; ``memory_delta`` is added when memory is enabled (AC-W1) and
    ``follow_ups`` when the flow opted in (``supervisor.emit_follow_ups``). When neither is enabled
    the caller runs a free-text turn instead (today's behavior — AC-S4), so this is never called."""
    properties: dict[str, Any] = {"summary": {"type": "string", "minLength": 1}}
    if with_delta:
        properties["memory_delta"] = DELTA_OUTPUT_SCHEMA
    if with_follow_ups:
        properties["follow_ups"] = _FOLLOW_UPS_SCHEMA
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": ["summary"],
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
    """The slice of the state store the supervisor needs: the immutable evaluation row plus the
    durable own-session lineage (read on first use, written after each turn)."""

    def record_evaluation(self, row: EvaluationRow) -> int: ...

    def get_node_lineage(
        self, task_id: str, node_id: str, subtask_order: int | None = None
    ) -> NodeLineageRow | None: ...

    def upsert_node_lineage(self, row: NodeLineageRow) -> None: ...


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
        flow_supervisor: SupervisorBlock | None = None,
        register_artifact: RegisterArtifact | None = None,
        default_timeout_seconds: int = 7200,
    ) -> None:
        self._settings = settings
        self._router = router
        self._store = store
        self._repo_dir = repo_dir
        self._artifacts_root = artifacts_root
        self._flow_dir = flow_dir
        # Flow-local supervisor prompt overrides + the follow-ups opt-in (prompt-and-supervisor
        # ADR). ``None`` → the global config prompt + built-in finalize, free-text finalize (today).
        self._flow_role_file = flow_supervisor.role_file if flow_supervisor else None
        self._flow_finalize_role_file = (
            flow_supervisor.finalize_role_file if flow_supervisor else None
        )
        self._emit_follow_ups = flow_supervisor.emit_follow_ups if flow_supervisor else False
        self._register_artifact = register_artifact
        self._default_timeout_seconds = default_timeout_seconds
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
        subtask_order: int | None = None,
    ) -> None:
        """Observe one completed step read-only and record an advisory ``supervisor_step`` row.

        Best-effort: a failed observation is logged and swallowed — it is advisory and must never
        fail or reroute the task. Namespaced by ``source_node_run_id`` (the step), so a resumed run
        does not duplicate observations.
        """
        prompt = self._step_prompt(task_id, node_id, outcome_kind, final_message)
        note = self._run(task_id, prompt, node_run_id=node_run_id)
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
        self, *, task_id: str, task_title: str, emit_delta: bool = False
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
        """
        # ``node_run_id=0`` is the once-per-task finalize sentinel; per-step observations use the
        # observed step's id, so each supervisor turn writes a distinct artifact dir (no collision).
        summary_text, delta, follow_ups = self._finalize_turn(task_id, task_title, emit_delta)
        self._record(
            task_id,
            kind="supervisor_final",
            source_node_run_id=None,
            subtask_order=None,
            payload={
                "summary_written": summary_text is not None,
                "memory_delta": delta is not None,
                "follow_ups": len(follow_ups),
            },
        )
        task_dir = Path(task_artifact_dir(self._artifacts_root, task_id))
        self._write_summary_json(task_dir, task_id, task_title, summary_text, follow_ups)
        if not summary_text or not summary_text.strip():
            return FinalizeResult(summary_path=None, candidate_delta=delta, follow_ups=follow_ups)
        md_path = task_dir / "summary.md"
        body = summary_text.rstrip("\n") + "\n"
        if follow_ups:  # surface the evidence-gated debt/follow-ups as a section in the PR body
            body += "\n" + _render_follow_ups_section(follow_ups)
        md_path.write_text(body, encoding="utf-8")
        self._register(task_id, "summary_md", str(md_path))
        return FinalizeResult(summary_path=md_path, candidate_delta=delta, follow_ups=follow_ups)

    def _finalize_turn(
        self, task_id: str, task_title: str, emit_delta: bool
    ) -> tuple[str | None, CandidateDelta | None, tuple[FollowUp, ...]]:
        """Run the single finalize turn. Free-text when neither memory nor follow-ups are enabled
        (today's behavior — AC-S4); otherwise a structured ``{summary, ...}`` turn, so every enabled
        output (``memory_delta`` / ``follow_ups``) rides one turn (AC-W1 — no extra LLM call)."""
        with_follow_ups = self._emit_follow_ups
        if not emit_delta and not with_follow_ups:
            text = self._run(task_id, self._finalize_prompt(task_id, task_title), node_run_id=0)
            return text, None, ()
        result = self._run_result(
            task_id,
            self._finalize_prompt(
                task_id, task_title, with_delta=emit_delta, with_follow_ups=with_follow_ups
            ),
            node_run_id=0,
            output_schema=_finalize_schema(with_delta=emit_delta, with_follow_ups=with_follow_ups),
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

    # -- upfront skill-map proposal --------------------------------------------

    def propose_skill_map(
        self,
        *,
        task_id: str,
        agent_node_ids: Sequence[str],
        inventory: SkillInventory,
        task_spec_text: str,
    ) -> dict[str, tuple[str, ...]]:
        """Propose a ``node → skills`` map once per task (read-only, propose-only — Core decides).

        The supervisor's first turn, on its own durable session, so later per-step observations and
        the whole-task summary inherit its reasoning about which skills it chose. Skipped (``{}``)
        when the inventory is empty — a repo with no skills pays nothing. Best-effort by contract:
        any failure (no provider, infra error, malformed output) is logged and yields ``{}``, and
        the run continues on operator pins alone. The supervisor only *proposes* tokens; the
        orchestrator resolves them against the inventory and merges them with the static pins.
        """
        if not inventory.skills:
            return {}
        prompt = self._proposal_prompt(task_id, agent_node_ids, inventory, task_spec_text)
        result = self._run_result(
            task_id, prompt, node_run_id=_PROPOSAL_RUN_ID, output_schema=_SKILL_MAP_SCHEMA
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

    def _run(self, task_id: str, prompt: str, *, node_run_id: int) -> str | None:
        """Run one read-only supervisor turn and return its final message (``None`` on failure)."""
        result = self._run_result(task_id, prompt, node_run_id=node_run_id)
        return result.final_message if result is not None else None

    def _run_result(
        self,
        task_id: str,
        prompt: str,
        *,
        node_run_id: int,
        output_schema: dict[str, Any] | None = None,
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
        """
        try:
            route = self._router.resolve_route(_SUPERVISOR_IDENTITY, None)
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
                reasoning=self._settings.reasoning,
                output_schema=output_schema,
                session_id=self._resume_session(task_id, route),
            )
            outcome = self._router.run_stage(request, route)
        except Exception as exc:  # noqa: BLE001 — advisory layer must never break the task
            _LOG.warning(
                "supervisor turn failed (advisory, ignored)",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
            )
            return None
        result = outcome.result
        if result is None:
            return None
        if result.session_id:
            self._own_session_id = result.session_id  # resume_own_lineage continuity (in-memory)
            self._persist_session(task_id, result.session_id, outcome.provider_used)
        return result

    def _resume_session(self, task_id: str, route: ResolvedRoute) -> str | None:
        """The own session to resume: the in-memory id if a turn already ran this process, else the
        persisted lineage — but only when produced by the provider now resolved (you cannot resume a
        Claude session on Codex). On the first round there is no lineage yet, so it starts fresh.
        """
        if self._own_session_id is not None:
            return self._own_session_id
        row = self._store.get_node_lineage(task_id, _SUPERVISOR_LINEAGE_NODE_ID, None)
        if row is None or row.provider != route.primary.value:
            return None
        self._own_session_id = row.raw_session_id
        return self._own_session_id

    def _persist_session(
        self, task_id: str, session_id: str, provider_used: ProviderId | None
    ) -> None:
        """Persist the supervisor's own session after a successful turn (``state.db`` only — the raw
        id is redacted everywhere else), keyed by the reserved sentinel so a resumed task resumes
        it.
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
        task_spec_text: str,
    ) -> str:
        nodes = "\n".join(f"- {nid}" for nid in agent_node_ids) or "- (none)"
        skills = "\n".join(f"- {s.name} — {s.description} [{s.path}]" for s in inventory.skills)
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
            + f"### Task\n{task_spec_text}\n"
        )

    def _finalize_prompt(
        self,
        task_id: str,
        task_title: str,
        *,
        with_delta: bool = False,
        with_follow_ups: bool = False,
    ) -> str:
        # The finalize lens (flow ``finalize_role_file`` → built-in) carries the summary emphasis;
        # only the machine-contract additions (task context, follow-ups, memory delta) are appended
        # in code, so a flow author reshapes wording but never the parsed schema.
        prompt = self._finalize_base(task_id) + f"\n\n## Task under review\n{task_title}\n"
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
                "Also propose what is worth REMEMBERING for future tasks on this repo, as the "
                "structured `memory_delta`: durable `lessons` (stable facts/commands/conventions, "
                "fragile areas, recurring reviewer expectations — each with `kind`, `subject`, "
                "`statement`, and `evidence` pointers to repo files/commits/checks), recurring "
                "`failures` (signature + remedy), and important `entities` (files/modules with "
                "their paths). Propose only what repeats, stays true, or saves rediscovery — never "
                "secrets, raw diffs, or one-off details; every lesson needs evidence. Leave a list "
                "empty when nothing qualifies.\n"
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
        """Write the local-only ``summary.json`` metadata (never committed). Always written.

        Carries the evidence-gated ``follow_ups`` (empty unless the flow opted in) so the debt
        signal is machine-readable beside the prose summary."""
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
        path = task_dir / "summary.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError:
            return
        self._register(task_id, "summary_json", str(path))

    def _register(self, task_id: str, kind: str, path: str) -> None:
        if self._register_artifact is not None:
            self._register_artifact(task_id, kind, path)
