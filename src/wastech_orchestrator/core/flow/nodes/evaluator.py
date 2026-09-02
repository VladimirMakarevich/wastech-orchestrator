"""Evaluator node runner — the shared in-flow evaluator primitive.

Runs the evaluator's ``role_file`` prompt (read-only) through the router and maps its structured
verdict to an engine outcome: a gating finding -> ``rework``, an otherwise-clean verdict ->
``accept``. A finding gates when its severity is at least as severe as the node's ``gate_severity``
(built-in default ``high`` — blocks ``high``/``critical``/``blocking``, leaving ``medium``/``low``
advisory; the packaged flows whose evaluators are *quality* lenses set ``medium``, since "is this
good enough" has no natural way to emit ``high``). A finding that does not gate is not discarded:
it rides ``NodeOutcome.findings`` to the operator surface via the supervisor's follow-ups. The
findings schema
(``output_schema``) is mandatory: a run whose ``structured_output`` does not carry a parseable
``findings`` array never silently accepts — it degrades straight to ``manual`` (fail-closed), the
same as a provider that could not run the node at all. A **blocking** evaluator gates
every time it finds a blocking issue; the engine's named-loop budget bounds the rework cycles
(exhaustion -> manual). A **non-blocking**
evaluator (e.g. ``test_quality``) self-caps: it reworks until its own per-instance budget
(``max_rework_per_stage``) is spent, then takes the ``accept`` edge (-> continue), **never** manual
That budget-exhausted accept sets ``NodeOutcome.rework_exhausted`` so the orchestrator warns
the operator (console + Telegram trace) the stage moved on with findings still open. Each pass
writes an immutable ``evaluations`` row (``in_flow_verdict``) namespaced by the
source ``node_run`` id — the per-instance rework limit is derived by COUNTing those verdicts, not a
mutable counter, so the core stays domain-free (the cap is the node's declared
budget, not knowledge of the role). One mechanism serves every in-flow role (review / test_quality /
critic / verifier / operator-defined); only the prompt, blocking flag, and budget differ.

Supervision is **not** an evaluator: the constant orchestrator layer above the flow owns
per-step + final advisory observation (see ``core/supervisor.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from wastech_orchestrator.core.flow.context_paths import (
    build_node_output_paths,
    build_path_context,
)
from wastech_orchestrator.core.flow.contracts import (
    SessionScope,
    resolve_git_evidence,
    resolve_network_access,
)
from wastech_orchestrator.core.flow.engine import Finding, NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    EvaluatorInfraError,
    NodeInputs,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.exchange_publish import (
    assert_exchange_unchanged,
    assert_request_contained,
    capture_exchange_manifest,
    publish_file,
    publish_node_run_file,
)
from wastech_orchestrator.core.flow.observability import record_run_observability
from wastech_orchestrator.core.flow.prompt import RoleFileError, read_role_file, render_role_prompt
from wastech_orchestrator.core.flow.prompt_vars import valid_prompt_vars
from wastech_orchestrator.core.flow.schema import SEVERITY_ORDER, EvaluatorNode, FlowNode
from wastech_orchestrator.core.flow.usage_accounting import (
    deserialize_usage,
    guard_output_baseline,
    snapshot_for_lineage,
)
from wastech_orchestrator.git_manager import ChangedPath, GitControlState
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers.artifacts import (
    exchange_latest_run_file,
    node_run_dir,
    task_artifact_dir,
)
from wastech_orchestrator.providers.base import AgentRunRequest, build_effective_prompt
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import EvaluationRow, NodeLineageRow, NodeRunRow

_LOG = logging.getLogger(__name__)

#: Raw severity tokens that normalize to ``high`` / ``medium`` on the typed ``Finding`` (the
#: audit-trail projection in ``_to_finding``). This is the severity-*naming* map, NOT the routing
#: gate: whether a finding drives ``rework`` is decided by ``_is_blocking`` comparing its rank
#: against the node's configurable ``gate_severity`` (ranked by :data:`SEVERITY_ORDER`).
_BLOCKING_SEVERITIES = frozenset({"blocking", "critical", "high"})
_MEDIUM_SEVERITIES = frozenset({"medium", "moderate"})


def _severity_rank(token: str) -> int:
    """Rank a raw severity token by :data:`SEVERITY_ORDER` (lower = more severe).

    An unknown or absent token ranks as the least-severe floor (``len(SEVERITY_ORDER)``), so a
    malformed severity never accidentally gates.
    """
    t = token.lower()
    return SEVERITY_ORDER.index(t) if t in SEVERITY_ORDER else len(SEVERITY_ORDER)


#: The mandatory structured findings schema every in-flow evaluator role (review / verifier /
#: critic / operator-defined) is prompted to return. A role-prompt asking for findings "in prose"
#: was unenforceable: extraction reads only ``structured_output``, which no provider filled without
#: an ``output_schema`` — the gate silently fail-**opened** to ``accept`` on every real run. An
#: empty ``findings`` array is well-formed and genuinely clean; a missing/malformed one means the
#: provider did not honor the schema and fails **closed** (see ``_findings_or_none``/``run``).
_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    # OpenAI Structured Outputs (how codex CLI enforces ``--output-schema``) rejects a
    # schema with a 400 unless every object node BOTH carries ``additionalProperties: false`` AND
    # lists every ``properties`` key in ``required`` — the same convention followed in
    # ``hitl.py``/``supervisor.py``/``memory/delta.py``. ``path``/``fix`` stay optional by being
    # nullable (``_to_finding``/``fixing`` tolerate a ``null`` field like an absent one).
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": list(SEVERITY_ORDER),
                    },
                    "path": {"type": ["string", "null"]},
                    "what": {"type": "string"},
                    "fix": {"type": ["string", "null"]},
                },
                "required": ["severity", "path", "what", "fix"],
            },
        },
    },
    "required": ["findings"],
}


@dataclass(frozen=True, slots=True)
class _EvaluatorControlBefore:
    """What a shell-bearing evaluator attempt is judged against: control state + tree change set."""

    control: GitControlState
    tree: tuple[ChangedPath, ...]


class EvaluatorNodeRunner:
    """Run an ``evaluator`` node through the router and map its verdict to accept/rework/done."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, EvaluatorNode)
        route = self._s.router.resolve_route(node.id, node.provider)
        started_at = self._s.clock()
        run_id = self._s.store.record_node_run(
            NodeRunRow(
                task_id=ctx.task_id,
                node_id=node.id,
                node_kind="evaluator",
                subtask_order=ctx.subtask_order,
                status="running",
                route_primary=route.primary.value,
                route_fallback=route.fallback.value if route.fallback else None,
                route_source=route.source.value,
                started_at=started_at,
            )
        )
        lineage = self._resume_node_lineage(node, ctx, route)
        baseline = deserialize_usage(lineage.usage_snapshot) if lineage else None
        session_id = lineage.raw_session_id if lineage else None
        request = self._build_request(
            node, ctx, route, run_id, session_id, guard_output_baseline(baseline)
        )
        assert_request_contained(request, self._s.exchange_root)
        # A shell is what makes a `.git` mutation reachable, so the Write/Edit-deny roots ride every
        # attempt that has one — an evaluator included. Without them the provider's pre-launch
        # canary had no write-deny probe to run here, and the loud floor-1 line's "re-proved before
        # every provider attempt" held for the agent node alone.
        has_shell = self._attempt_has_shell(node, route)
        git = self._s.git
        if has_shell and git is not None:
            request = replace(request, write_guard=git.resolve_control_paths(self._s.exchange_root))
        # Detection-in-depth: fingerprint the curated exchange before the (read-only)
        # evaluator attempt so a provider mutation of the immutable surface is caught from
        # parent-held state after quiescence, before its findings are trusted downstream.
        exchange_before = capture_exchange_manifest(self._s.exchange_root, ctx.task_id)
        # The same bracket the agent runner takes around an attempt that has a shell but no write
        # access: an evaluator is read-only by construction, yet whether it can run commands is the
        # provider's answer, not ours — a Codex reviewer has a shell. Reported, never parked, like
        # every other node class. Skipped when the attempt has no shell, so no node pays for a check
        # that cannot apply to it.
        control_before = self._control_before(has_shell, ctx.task_id)
        outcome = self._s.router.run_stage(request, route, snapshot=self._s.snapshot)
        raw_findings = (
            self._findings_or_none(outcome.result.structured_output)
            if outcome.result is not None
            else None
        )
        gating = self._gating_flags(node, raw_findings)
        kind, rework_exhausted = self._verdict(node, ctx, outcome, raw_findings, gating)
        self._record_completion(run_id, outcome, kind)
        record_run_observability(
            self._s,
            task_id=ctx.task_id,
            node_id=node.id,
            subtask=ctx.subtask_order,
            run_id=run_id,
            prompt=build_effective_prompt(request),
            route=route,
            outcome=outcome,
            model=node.model,
            reasoning=node.reasoning,
            started_at=started_at,
            usage_baseline=baseline,
            baseline_session_id=session_id,
        )
        drift, wrote = self._control_drift(control_before, ctx.task_id)
        if outcome.result is None:
            self._log_lost_drift(node, ctx, drift)
            error_class = outcome.terminal_error.error_class if outcome.terminal_error else None
            err = error_class.value if error_class else "no_provider_available"
            raise EvaluatorInfraError(
                f"evaluator node {node.id!r}: no provider could run it ({err})",
                error_class=error_class,
                # Every attempt's class, not just the settled one: a fallback that fails worse than
                # the primary must not be able to mask a resumable primary failure. Only the rows
                # that RAISED count — a row carrying a status returned a verdict, and a quality
                # verdict must never be able to reach a park or manual decision.
                error_classes=tuple(
                    a.error_class
                    for a in outcome.attempts
                    if a.status is None and a.error_class is not None
                ),
                # The provider's own claim about when a retry could succeed, for the Core to
                # validate and clamp; absent for a provider that reports no reset instant.
                resets_at=outcome.terminal_error.resets_at if outcome.terminal_error else None,
            )
        assert_exchange_unchanged(
            exchange_before, self._s.exchange_root, ctx.task_id, node_id=node.id
        )
        if raw_findings is None:
            self._log_lost_drift(node, ctx, drift)
            # Fail closed: the provider did not honor the mandatory findings schema (missing
            # or malformed ``findings`` array) — never silently accept. There is nothing for
            # `fixing` to act on (no parsed findings), so this degrades straight to manual (like
            # the no-provider case above) rather than spending a rework cycle on an empty verdict.
            raise EvaluatorInfraError(
                f"evaluator node {node.id!r}: verdict fail-closed — structured_output did not "
                "include a parseable 'findings' array (schema not honored)"
            )
        self._persist_own_lineage(node, ctx, outcome)
        findings = tuple(_to_finding(f) for f in raw_findings)
        # Persist the findings artifact and expose it to downstream fixing as {review_path}.
        self._write_findings(node, ctx, run_id, raw_findings, outcome.result.final_message)
        # Immutable in-flow verdict (append-only, namespaced by the source node_run id). The
        # per-instance rework limit derives from COUNT(rework) — there is no mutable counter.
        self._s.store.record_evaluation(
            EvaluationRow(
                task_id=ctx.task_id,
                node_id=node.id,
                source_node_run_id=run_id,
                subtask_order=ctx.subtask_order,
                kind="in_flow_verdict",
                verdict=kind,
                findings_json=_findings_json(findings, gating),
            )
        )
        return NodeResult(
            node_id=node.id,
            outcome=NodeOutcome(
                kind,
                findings=findings,
                rework_exhausted=rework_exhausted,
                unexpected_write=wrote,
                git_control_drift=drift,
                # Carry the provider's own prose, not just the typed findings. Without it the
                # supervisor observed a bare `Outcome: accept` and its whole-task summary described
                # an evaluator that emitted findings as a gate that "passed". The agent runner has
                # always passed this; the evaluator runner dropped it one layer up.
                final_message=outcome.result.final_message,
                gating_findings_name_no_path=_no_gating_finding_names_a_path(findings, gating),
            ),
            node_run_id=run_id,
        )

    def _write_findings(
        self,
        node: FlowNode,
        ctx: NodeContext,
        run_id: int,
        findings: list[dict[str, Any]],
        summary: str | None,
    ) -> None:
        # Per-run dir keyed by node.id + run_id, so a second evaluator (e.g. test_quality) cannot
        # clobber review and every pass of a fix→review loop keeps its own findings on disk.
        run_dir = node_run_dir(self._s.artifacts_root, ctx.task_id, node.id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        findings_path = run_dir / "findings.json"
        findings_json = json.dumps({"findings": findings}, indent=2, ensure_ascii=False) + "\n"
        findings_path.write_text(findings_json, encoding="utf-8")
        (run_dir / "summary.md").write_text(
            (summary or "(no review summary)") + "\n", encoding="utf-8"
        )
        # Private findings.json stays the audit record; the redacted exchange copy is {review_path}.
        self._in.review_path = publish_node_run_file(
            self._s.exchange_root,
            ctx.task_id,
            node.id,
            run_id,
            "findings.json",
            findings_json,
            extra_secrets=self._s.prompt_secrets,
            private_path=str(findings_path),
        )

    def _log_lost_drift(self, node: FlowNode, ctx: NodeContext, drift: str | None) -> None:
        """Say the Git-control drift out loud on the paths that leave this node through a raise.

        The ordinary path carries it on ``NodeOutcome``, where the orchestrator's post-node hook
        warns and traces it. A node that raised has no outcome, so the comparison — already computed
        — was simply dropped: a node that planted a hook and then failed to emit parseable findings
        went to ``manual_action_required`` with no word about the clone. That is the one signal by
        which an operator knows to discard it rather than read on through the findings.
        """
        if drift is None:
            return
        bind(_LOG, task_id=ctx.task_id, node_id=node.id).warning(
            "git control state changed during this evaluator attempt, and the node is stopping "
            "for another reason — discard the clone before it is committed or pushed: %s",
            drift,
        )

    def _attempt_has_shell(self, node: EvaluatorNode, route: ResolvedRoute) -> bool:
        """Whether this evaluator attempt actually gets a shell on a provider it may land on.

        The answer comes from the adapters through the Router, because it is provider- and
        host-specific: a Codex evaluator runs commands on its ``read-only`` profile, a Claude one
        only with the git-evidence grant — and in the advanced mode every attempt has one.
        Fail-closed (an unclassifiable attempt counts as having a shell), and asked once per run so
        the detection bracket and the write-guard the request carries can never disagree.
        """
        return self._s.router.route_grants_shell(
            route,
            permission_profile=node.permission_profile.value,
            git_evidence=resolve_git_evidence(node.git_evidence, self._s.allow_git_evidence),
        )

    def _control_before(self, has_shell: bool, task_id: str) -> _EvaluatorControlBefore | None:
        """The Git-control + working-tree fingerprint taken before a shell-bearing evaluator runs.

        ``None`` when there is no Git Manager or when the attempt gets no shell — the signal to skip
        both comparisons entirely, so no node pays for a check that cannot apply to it.
        """
        git = self._s.git
        if git is None or not has_shell:
            return None
        return _EvaluatorControlBefore(
            control=git.capture_git_control_state(), tree=git.changed_code_entries(task_id)
        )

    def _control_drift(
        self, before: _EvaluatorControlBefore | None, task_id: str
    ) -> tuple[str | None, bool]:
        """``(redacted drift summary, wrote to the tree)`` for the attempt that just finished.

        Before-vs-after rather than "is the tree dirty", so an earlier writing node's diff is never
        blamed on this evaluator. Control state is compared first, so the ``git status`` behind the
        tree comparison cannot land between the attempt and the fingerprint that judges it.
        """
        git = self._s.git
        if before is None or git is None:
            return None, False
        control_drift = git.compare_git_control_state(before.control)
        summary = control_drift.summary() if control_drift is not None else None
        return summary, git.changed_code_entries(task_id) != before.tree

    def _verdict(
        self,
        node: EvaluatorNode,
        ctx: NodeContext,
        outcome: StageOutcome,
        raw_findings: list[dict[str, Any]] | None,
        gating: tuple[bool, ...],
    ) -> tuple[str, bool]:
        """Map the verdict to ``(edge_kind, rework_exhausted)``.

        ``rework_exhausted`` is True only on the single ``accept`` where a NON-blocking evaluator
        gave up: it still found a gating issue but its per-instance ``max_rework_per_stage`` budget
        was spent, so it continues with findings still open. The orchestrator surfaces that as an
        operator warning + Telegram trace so a human knows the stage may need follow-up; the flag is
        never routing state (``accept`` is ``accept``).

        ``gating`` is the caller's already-computed per-finding gate decision, passed in rather than
        recomputed so the routing verdict and the flag persisted beside it are literally the same
        values (see :meth:`_gating_flags`).
        """
        if outcome.result is None:
            return "accept", False  # dead for routing: run() raises EvaluatorInfraError first
        if raw_findings is None:
            return "manual", False  # dead for routing (run() raises); accurate audit-trail label
        if not any(gating):
            return "accept", False
        if node.blocking:
            # A blocking evaluator gates every time it finds a blocking issue; the engine's
            # named-loop budget bounds the rework cycles (exhaustion → manual).
            # `max_rework_per_stage` is a non-blocking-only knob, intentionally NOT consulted here.
            return "rework", False
        # A non-blocking evaluator (e.g. test_quality) self-caps: rework until its own per-instance
        # budget (max_rework_per_stage) is spent — counted from the immutable in_flow_verdict rows,
        # not a mutable counter — then accept (→ continue), never manual. The
        # core stays domain-free: the cap is the node's declared budget, not knowledge of the role.
        prior_rework = self._s.store.count_rework_verdicts(
            ctx.task_id, node_id=node.id, subtask_order=ctx.subtask_order
        )
        if prior_rework < node.max_rework_per_stage:
            return "rework", False
        # Budget spent with a gating finding still open: accept and continue, but flag it so the
        # orchestrator warns the operator the stage moved on with work possibly left to do.
        return "accept", True

    def _build_request(
        self,
        node: EvaluatorNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        run_id: int,
        session_id: str | None = None,
        resume_baseline_output_tokens: int | None = None,
    ) -> AgentRunRequest:
        # The renderer stays the fixed security core; the caller widens *which names* it may
        # substitute to the flow-derived set (core allowlist ∪ each agent/tool node's {<id>_path}),
        # exactly as the agent runner does, and only ever places path values in the dict.
        prompt = render_role_prompt(
            self._in.flow_dir,
            node.role_file,
            self._prompt_variables(ctx, node),
            allowed=valid_prompt_vars(ctx.snapshot),
        )
        return AgentRunRequest(
            task_id=ctx.task_id,
            node_id=node.id,
            working_directory=self._s.repo_dir,
            prompt=prompt,
            permission_profile=node.permission_profile.value,
            timeout_seconds=self._s.default_timeout_seconds,
            attempt=1,
            node_run_id=run_id,
            task_path=self._in.task_path,
            plan_path=self._in.plan_path,
            diff_path=self._in.diff_path,
            check_artifacts_path=self._in.checks_path,
            review_artifacts_path=self._in.review_path,
            # On a rework re-entry, hand the reviewer the previous author node's report so it
            # judges "was the finding addressed" with the implementer's account (including a stated
            # blocker) in hand, not the diff alone. ``None`` on the first pass (no prior report).
            rework_report_path=self._prior_rework_report_path(node, ctx),
            # Mandatory, not advisory: extraction reads only ``structured_output``, so without a
            # schema no provider fills it and ``run()`` fails **closed** to manual rather than
            # silently accepting a verdict nobody produced.
            output_schema=_FINDINGS_SCHEMA,
            model=node.model,
            reasoning=node.reasoning,
            # Evaluators never inherit an author's editing lineage (validator-enforced read-only).
            # A ``fresh_disposable`` evaluator starts clean each pass; a ``resume_own_lineage`` one
            # (the research critic) resumes its OWN durable session so it remembers what it flagged
            # across rework rounds. The resumed session (and its usage baseline) is resolved
            # by the caller so the row is read once.
            session_id=session_id,
            resume_baseline_output_tokens=resume_baseline_output_tokens,
            # Network is a per-node override on top of the flow-wide default (a research verifier
            # may need it): the node's ``network_access`` wins, else it inherits the flow's
            # ``network_policy`` default. It only toggles network — evaluators stay read-only
            # on the filesystem.
            network_access=resolve_network_access(
                node.network_access, ctx.snapshot.doc.network_policy
            ),
            # The read-only git verbs, when this evaluator asked for them AND the operator enabled
            # the grant. Reading only: the evaluator stays read-only on the filesystem either way.
            git_evidence=resolve_git_evidence(node.git_evidence, self._s.allow_git_evidence),
            # Defense-in-depth: the Core-owned advisory security contract, threaded via
            # NodeServices; the neutral seam prepends it to the effective prompt.
            security_preamble=self._s.security_preamble,
        )

    def _prior_rework_report_path(self, node: EvaluatorNode, ctx: NodeContext) -> str | None:
        """The latest exchange report of this evaluator's rework-target author node, or ``None``.

        The evaluator's own ``rework`` edge names the author it sends work back to (``review →
        fixing`` in the implementation flow). On a re-entry that author has already published its
        ``<node>.out.md`` to the exchange; surface the newest one so the reviewer reads the
        implementer's account. ``None`` when there is no rework edge, no exchange is wired,
        or the author has not run yet (the first review pass) — the footer then omits the slot.
        """
        if not self._s.exchange_root:
            return None
        target = next(
            (e.to for e in ctx.snapshot.adjacency.get(node.id, ()) if e.outcome == "rework"),
            None,
        )
        if target is None:
            return None
        path = exchange_latest_run_file(
            self._s.exchange_root, ctx.task_id, target, f"{target}.out.md"
        )
        return str(path) if path is not None else None

    def _resume_node_lineage(
        self, node: EvaluatorNode, ctx: NodeContext, route: ResolvedRoute
    ) -> NodeLineageRow | None:
        """The durable own session this evaluator resumes, or ``None`` for a fresh session.

        A ``fresh_disposable`` evaluator always starts clean (``None``). A ``resume_own_lineage``
        one (the research critic) resumes the session it stored on its previous pass — but only when
        the stored session was produced by the same provider it now resolves to (no resuming a
        Claude session on Codex). On the first round there is no lineage yet, so it starts fresh.
        The row carries the usage snapshot the caller subtracts to get a per-run delta.
        """
        if node.session_scope is not SessionScope.RESUME_OWN_LINEAGE:
            return None
        row = self._s.store.get_node_lineage(ctx.task_id, node.id, ctx.subtask_order)
        if row is None or row.provider != route.primary.value:
            return None
        return row

    def _persist_own_lineage(
        self, node: EvaluatorNode, ctx: NodeContext, outcome: StageOutcome
    ) -> None:
        """Persist a ``resume_own_lineage`` evaluator's session after a successful pass.

        A ``fresh_disposable`` evaluator never writes a lineage. The raw session id is stored ONLY
        in ``state.db`` (redacted everywhere else), keyed by ``(task_id, node_id, subtask_order)``
        so the node's next round resumes exactly its own session.
        """
        if node.session_scope is not SessionScope.RESUME_OWN_LINEAGE:
            return
        result = outcome.result
        if result is None or not result.session_id or outcome.provider_used is None:
            return
        self._s.store.upsert_node_lineage(
            NodeLineageRow(
                task_id=ctx.task_id,
                node_id=node.id,
                provider=outcome.provider_used.value,
                raw_session_id=result.session_id,
                subtask_order=ctx.subtask_order,
                updated_at=self._s.clock(),
                usage_snapshot=snapshot_for_lineage(result.normalized_usage),
            )
        )

    def _prompt_variables(self, ctx: NodeContext, node: EvaluatorNode) -> dict[str, object | None]:
        """The variables this evaluator's role prompt may substitute (paths and ids only).

        Kept in step with the agent runner's set by name, because the two have now diverged on one
        channel twice: the memory packet was wired for agent nodes only, leaving ``review.md``'s
        ``{?memory_path}`` block dead (see :meth:`_memory_path`), and the decomposition variables
        the same way. The one deliberate difference is ``predecessor_context`` — the *author's*
        handoff brief, assembled for the node that writes a subtask, which an evaluator is not the
        reader of. ``test_the_agent_and_evaluator_runners_publish_the_same_variable_names`` compares
        the two key sets so the next omission fails a test rather than a run.
        """
        paths = build_path_context(self._in, self._s.repo_dir)
        variables: dict[str, object | None] = {
            "task_id": ctx.task_id,
            "stage": node.id,
            "repo_path": paths["repo"],
            **paths,
            "memory_path": self._memory_path(node, ctx),
        }
        # An evaluator inside a decompose region runs once PER SUBTASK, and without these it judged
        # each subtask's diff against the ROOT task file and the shared plan — the only two things
        # it was given. So it could hold neither the subtask's own acceptance criteria nor its
        # "out of scope for this subtask" boundary, and charged every not-yet-implemented part of
        # the whole task against whichever subtask was under review. The runner already used
        # ``ctx.subtask_order`` for its own artifact namespacing; it simply never passed it on.
        if ctx.subtask_order is not None:
            variables["subtask_order"] = ctx.subtask_order
            variables["subtask_count"] = self._in.subtask_count
            variables["subtask_spec_path"] = self._in.subtask_spec_path
        # An evaluator judging the *work* (a coverage gate, a critic) needs the upstream
        # node's output, not only the report a later node wrote from it. Same channel the agent
        # runner reads, same rule — a path to a Core-written redacted artifact, never inlined
        # content, and empty (block drops) for a node that has not run.
        variables.update(
            build_node_output_paths(
                ctx.snapshot.doc.nodes,
                ctx.task_id,
                exchange_root=self._s.exchange_root,
                artifacts_root=self._s.artifacts_root,
            )
        )
        return variables

    def _memory_path(self, node: EvaluatorNode, ctx: NodeContext) -> str | None:
        """Build this evaluator's memory packet and return its path — node-driven.

        Mirrors the agent runner: the per-node packet path is returned only when memory is enabled
        AND the node's (operator-editable) role prompt references ``{memory_path}``; otherwise
        ``None`` (the conditional block drops). ``review``/``fixing`` are the reviewer-preference
        nodes in ``packet.py``, so review most wants recurring reviewer expectations — but the
        evaluator runner never wired the packet, leaving ``review.md``'s ``{?memory_path}`` block
        dead. Best-effort: any memory failure degrades to no packet."""
        builder = self._s.packet_builder
        if builder is None:  # memory disabled — no store, empty variable, unchanged behavior
            return None
        try:
            template = read_role_file(self._in.flow_dir, node.role_file)
        except RoleFileError:
            return None  # render_role_prompt surfaces the real read error
        if "{memory_path}" not in template and "{?memory_path}" not in template:
            return None
        # This task's changed paths (per-task chain base), not the whole shared branch's, so
        # the packet's path-overlap ranking stays relevant on a chain branch.
        touched = (
            self._s.git.changed_code_paths_since_task_base() if self._s.git is not None else []
        )
        dest = task_artifact_dir(self._s.artifacts_root, ctx.task_id) / "memory" / f"{node.id}.md"
        written = builder.write_packet(
            node_id=node.id, task_type=self._in.task_type, touched_paths=touched, dest=dest
        )
        if written is None:
            return None
        # The store + private packet stay private; only the redacted packet crosses.
        return publish_file(
            self._s.exchange_root,
            ctx.task_id,
            f"memory/{node.id}.md",
            str(written),
            extra_secrets=self._s.prompt_secrets,
        )

    def _record_completion(self, run_id: int, outcome: StageOutcome, kind: str) -> None:
        result = outcome.result
        status = result.status.value if result is not None else "failed"
        error_class = None
        if result is not None and result.error is not None:
            error_class = result.error.error_class.value
        elif result is None and outcome.terminal_error is not None:
            error_class = outcome.terminal_error.error_class.value
        self._s.store.complete_node_run(
            run_id,
            status=status,
            outcome=kind,
            provider_used=outcome.provider_used.value if outcome.provider_used else None,
            error_class=error_class,
            stage_attempts=outcome.stage_attempts,
            finished_at=self._s.clock(),
        )

    @staticmethod
    def _findings_or_none(structured: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
        """The parsed findings list, or ``None`` when the schema was not honored (fails closed).

        ``None`` covers a missing ``structured_output``, a non-mapping one, or one whose
        ``findings`` key is absent/not a list — all mean the provider ignored ``output_schema``.
        An empty list (``{"findings": []}``) is well-formed and genuinely clean.
        """
        if not isinstance(structured, Mapping):
            return None
        raw = structured.get("findings")
        if not isinstance(raw, list):
            return None
        return [dict(f) for f in raw if isinstance(f, Mapping)]

    @staticmethod
    def _is_blocking(finding: Mapping[str, Any], gate_rank: int) -> bool:
        """A finding gates iff it is explicitly ``blocking`` or at least as severe as the gate.

        ``gate_rank`` is the node's ``gate_severity`` rank (``_severity_rank``); a finding blocks
        when its own severity rank is ``<= gate_rank`` (lower rank = more severe).
        """
        if finding.get("blocking") is True:
            return True
        return _severity_rank(str(finding.get("severity", ""))) <= gate_rank

    def _gating_flags(
        self, node: EvaluatorNode, raw_findings: list[dict[str, Any]] | None
    ) -> tuple[bool, ...]:
        """Each raw finding's gate decision, in the findings' own order.

        Materialized as a whole tuple rather than folded into the verdict test, because ``any()``
        short-circuits: a lazy evaluation would leave every finding after the first gating one
        unflagged, and the persisted audit would then disagree with the verdict it came from. One
        pass feeds both :meth:`_verdict` and :func:`_findings_json`, so they cannot diverge.

        ``()`` when the provider did not honor the findings schema — there is nothing to decide
        about, and ``run()`` fails the node closed a few lines later.
        """
        if raw_findings is None:
            return ()
        gate_rank = _severity_rank(node.gate_severity)
        return tuple(self._is_blocking(f, gate_rank) for f in raw_findings)


def _no_gating_finding_names_a_path(
    findings: tuple[Finding, ...], gating: tuple[bool, ...]
) -> bool:
    """Whether the verdict gates and yet no gating finding says where.

    The rework edge leads to ``fixing``, whose whole job is to open a named source location and
    change it; a gating finding carrying no path gives it nothing to open. Observed twice on the
    same trial, both times the same shape: the evaluator did not find a defect, it reported that it
    *could not review* — a contradiction in its own instructions once, a build that died in its
    sandbox the other time — and the findings contract, whose ``path`` is nullable by design, had no
    way to say so. Each refusal was accepted as an ordinary verdict and spent a full fix round (426s
    and 474s) establishing there was nothing to fix.

    Judged over the **gating** findings only, and only when they are all pathless. One pathless
    blocker beside a located one still leaves ``fixing`` real work, so that is not this signal; and
    an advisory finding without a path routes nowhere and costs nothing.
    """
    gated = [f for f, gates in zip(findings, gating, strict=True) if gates]
    return bool(gated) and not any(f.paths for f in gated)


def _to_finding(raw: Mapping[str, Any]) -> Finding:
    """Map a raw structured finding to the typed :class:`Finding` (severity / reason / paths / fix).

    ``what``/``path`` are the findings schema's field names; ``reason``/``title``/``message``/
    ``paths`` (plural) stay as fallbacks for any pre-schema finding shape. The full raw dict is
    preserved as-is in the ``findings.json`` artifact ``fixing`` reads — this typed projection is
    for the audit trail and ``NodeOutcome.findings``. ``fix`` rides along because the audit row is
    what the operator's follow-up list is derived from, and the remedy is a finding's actionable
    half.
    """
    sev_token = str(raw.get("severity", "")).lower()
    if raw.get("blocking") is True or sev_token in _BLOCKING_SEVERITIES:
        severity: str = "high"
    elif sev_token in _MEDIUM_SEVERITIES:
        severity = "medium"
    else:
        severity = "low"
    reason = str(
        raw.get("what") or raw.get("reason") or raw.get("title") or raw.get("message") or raw
    )
    paths_raw = raw.get("paths")
    if isinstance(paths_raw, list | tuple):
        paths = tuple(str(p) for p in paths_raw)
    else:
        single_path = raw.get("path")
        paths = (str(single_path),) if single_path else ()
    fix_raw = raw.get("fix")
    fix = fix_raw.strip() if isinstance(fix_raw, str) and fix_raw.strip() else None
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        reason=reason,
        paths=paths,
        fix=fix,
    )


def _findings_json(findings: tuple[Finding, ...], gating: tuple[bool, ...]) -> str:
    """Serialize findings for the immutable ``evaluations`` row.

    ``gating`` is each finding's gate decision, in the same order, persisted because it cannot be
    recovered from what is stored: the typed projection above collapses an explicit ``blocking``
    flag, ``critical`` and ``high`` into one ``high``, so under a ``critical`` gate a reader
    comparing severities cannot tell a finding that sent work back from one that was let past. The
    flag is what lets the pull-request body list only the findings a gate accepted.

    ``fix`` is persisted for the same reason: the follow-ups an operator acts on are derived from
    this row, so a remedy left only in the per-run ``findings.json`` artifact never reaches them.

    ``strict=True`` is the guard that both sequences describe the same findings: a length mismatch
    is a programming error, and truncating silently would relabel gating findings as let-past.
    """
    return json.dumps(
        [
            {
                "severity": f.severity,
                "reason": f.reason,
                "paths": list(f.paths),
                "gating": g,
                "fix": f.fix,
            }
            for f, g in zip(findings, gating, strict=True)
        ],
        ensure_ascii=False,
    )
