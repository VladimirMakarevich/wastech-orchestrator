"""Evaluator node runner (P1.3/P2.1) — the shared in-flow evaluator primitive.

Runs the evaluator's ``role_file`` prompt (read-only) through the router and maps its structured
verdict to an engine outcome: a blocking finding (severity ``medium``/``high``) -> ``rework``, a
clean verdict -> ``accept``. A **blocking** evaluator gates every time it finds a blocking issue;
the engine's named-loop budget bounds the rework cycles (exhaustion -> manual). A **non-blocking**
evaluator (e.g. ``test_quality``) self-caps: it reworks until its own per-instance budget
(``max_rework_per_stage``) is spent, then takes the ``accept`` edge (-> continue), **never** manual
(P2.4). Each pass writes an immutable ``evaluations`` row (``in_flow_verdict``) namespaced by the
source ``node_run`` id — the per-instance rework limit is derived by COUNTing those verdicts, not a
mutable counter (flow-contract §2.2), so the core stays domain-free (the cap is the node's declared
budget, not knowledge of the role). One mechanism serves every in-flow role (review / test_quality /
critic / verifier / operator-defined); only the prompt, blocking flag, and budget differ.

Supervision is **not** an evaluator: the constant orchestrator layer above the flow owns
per-step + final advisory observation (see ``core/supervisor.py``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.engine import Finding, NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInfraError,
    NodeInputs,
    NodeServices,
)
from wastech_orchestrator.core.flow.observability import record_run_observability
from wastech_orchestrator.core.flow.prompt import render_role_prompt
from wastech_orchestrator.core.flow.schema import EvaluatorNode, FlowNode
from wastech_orchestrator.core.hitl import stage_output_schema
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import AgentRunRequest, Stage
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import EvaluationRow, NodeRunRow

_BLOCKING_SEVERITIES = frozenset({"blocking", "critical", "high"})
#: severity tokens the verdict treats as blocking (medium/high), normalized to the ``Finding`` set.
_HIGH_SEVERITIES = frozenset({"blocking", "critical", "high"})
_MEDIUM_SEVERITIES = frozenset({"medium", "moderate"})


class EvaluatorNodeRunner:
    """Run an ``evaluator`` node through the router and map its verdict to accept/rework/done."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, EvaluatorNode)
        stage = self._s.stage_for_node[node.id]
        route = self._s.router.resolve_route(stage, node.provider)
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
        request = self._build_request(node, ctx, stage, route, run_id)
        outcome = self._s.router.run_stage(request, route, snapshot=self._s.snapshot)
        kind = self._verdict(node, ctx, outcome)
        self._record_completion(run_id, outcome, kind)
        record_run_observability(
            self._s,
            task_id=ctx.task_id,
            stage=stage,
            subtask=ctx.subtask_order,
            run_id=run_id,
            prompt=request.prompt,
            route=route,
            outcome=outcome,
            model=node.model,
            started_at=started_at,
        )
        if outcome.result is None:
            err = (
                outcome.terminal_error.error_class.value
                if outcome.terminal_error
                else "no_provider_available"
            )
            raise NodeInfraError(f"evaluator node {node.id!r}: no provider could run it ({err})")
        raw_findings = self._extract_findings(outcome.result.structured_output)
        findings = tuple(_to_finding(f) for f in raw_findings)
        # Persist the findings artifact and expose it to downstream fixing as {review_path}.
        self._write_findings(ctx, raw_findings, outcome.result.final_message)
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
                findings_json=_findings_json(findings),
            )
        )
        return NodeResult(
            node_id=node.id,
            outcome=NodeOutcome(kind, findings=findings),
            node_run_id=run_id,
        )

    def _write_findings(
        self, ctx: NodeContext, findings: list[dict[str, Any]], summary: str | None
    ) -> None:
        review_dir = Path(task_artifact_dir(self._s.artifacts_root, ctx.task_id)) / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "findings.json").write_text(
            json.dumps({"findings": findings}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (review_dir / "summary.md").write_text(
            (summary or "(no review summary)") + "\n", encoding="utf-8"
        )
        self._in.review_path = str(review_dir / "findings.json")

    def _verdict(self, node: EvaluatorNode, ctx: NodeContext, outcome: StageOutcome) -> str:
        if outcome.result is None:
            return "accept"
        findings = self._extract_findings(outcome.result.structured_output)
        if not any(self._is_blocking(f) for f in findings):
            return "accept"
        if node.blocking:
            # A blocking evaluator gates every time it finds a blocking issue; the engine's
            # named-loop budget bounds the rework cycles (exhaustion → manual).
            return "rework"
        # A non-blocking evaluator (e.g. test_quality) self-caps: rework until its own per-instance
        # budget (max_rework_per_stage) is spent — counted from the immutable in_flow_verdict rows
        # (flow-contract §2.2), not a mutable counter — then accept (→ continue), never manual. The
        # core stays domain-free: the cap is the node's declared budget, not knowledge of the role.
        prior_rework = self._s.store.count_rework_verdicts(
            ctx.task_id, node_id=node.id, subtask_order=ctx.subtask_order
        )
        return "rework" if prior_rework < node.max_rework_per_stage else "accept"

    def _build_request(
        self,
        node: EvaluatorNode,
        ctx: NodeContext,
        stage: Stage,
        route: ResolvedRoute,
        run_id: int,
    ) -> AgentRunRequest:
        prompt = render_role_prompt(
            self._in.flow_dir, node.role_file, self._prompt_variables(ctx, stage)
        )
        return AgentRunRequest(
            task_id=ctx.task_id,
            stage=stage,
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
            output_schema=stage_output_schema(stage),
            model=node.model,
            reasoning=node.reasoning,
            # Evaluators are read-only and never editing_lineage (validator-enforced): a fresh
            # session each pass, never inheriting an author's editing lineage (durable sessions,
            # P2.2). A multi-round ``resume_own_lineage`` evaluator (research critic) is P3.
            session_id=None,
        )

    def _prompt_variables(self, ctx: NodeContext, stage: Stage) -> dict[str, object | None]:
        return {
            "task_id": ctx.task_id,
            "stage": stage.value,
            "repo_path": self._s.repo_dir,
            "repo": self._s.repo_dir,
            "task_path": self._in.task_path,
            "plan_path": self._in.plan_path,
            "diff_path": self._in.diff_path,
            "checks_path": self._in.checks_path,
            "review_path": self._in.review_path,
        }

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
    def _extract_findings(structured: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(structured, Mapping):
            return []
        raw = structured.get("findings")
        if not isinstance(raw, list):
            return []
        return [dict(f) for f in raw if isinstance(f, Mapping)]

    @staticmethod
    def _is_blocking(finding: Mapping[str, Any]) -> bool:
        if finding.get("blocking") is True:
            return True
        return str(finding.get("severity", "")).lower() in _BLOCKING_SEVERITIES


def _to_finding(raw: Mapping[str, Any]) -> Finding:
    """Map a raw structured finding to the typed :class:`Finding` (severity / reason / paths)."""
    sev_token = str(raw.get("severity", "")).lower()
    if raw.get("blocking") is True or sev_token in _HIGH_SEVERITIES:
        severity: str = "high"
    elif sev_token in _MEDIUM_SEVERITIES:
        severity = "medium"
    else:
        severity = "low"
    reason = str(raw.get("reason") or raw.get("title") or raw.get("message") or raw)
    paths_raw = raw.get("paths")
    paths = tuple(str(p) for p in paths_raw) if isinstance(paths_raw, list | tuple) else ()
    return Finding(severity=severity, reason=reason, paths=paths)  # type: ignore[arg-type]


def _findings_json(findings: tuple[Finding, ...]) -> str:
    """Serialize findings for the immutable ``evaluations`` row."""
    return json.dumps(
        [{"severity": f.severity, "reason": f.reason, "paths": list(f.paths)} for f in findings],
        ensure_ascii=False,
    )
