"""Evaluator node runner (P1.3) — minimal ``role=review`` evaluator.

Runs the evaluator's ``role_file`` prompt (read-only) through the router and maps its structured
verdict to an engine outcome: blocking findings -> ``rework``, a clean verdict -> ``accept``. A
``final_handoff`` evaluator is unconditional (-> ``done``); a non-blocking evaluator never gates
(-> ``accept``). This is the P1.3 parity of the legacy review stage; the full evaluator primitive
(supervisor/critic/verifier/test_quality, immutable verdict store, the review findings artifact +
``{review_path}`` wiring) lands in P2.1/P2.3 and the P1.4 wiring.

Blocking detection mirrors the legacy ``_extract_findings`` / ``_is_blocking``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.contracts import EvaluationKind
from wastech_orchestrator.core.flow.engine import Finding, NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import NodeInfraError, NodeInputs, NodeServices
from wastech_orchestrator.core.flow.observability import record_run_observability
from wastech_orchestrator.core.flow.prompt import render_role_prompt
from wastech_orchestrator.core.flow.schema import EvaluatorNode, FlowNode
from wastech_orchestrator.core.hitl import stage_output_schema
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import AgentRunRequest, Stage
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import NodeRunRow

_BLOCKING_SEVERITIES = frozenset({"blocking", "critical", "high"})


class EvaluatorNodeRunner:
    """Run an ``evaluator`` node through the router and map its verdict to accept/rework/done."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, EvaluatorNode)
        stage = self._s.stage_for_node[node.id]
        route = self._s.router.resolve_route(stage)
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
        kind = self._verdict(node, outcome)
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
        if node.evaluation_kind == EvaluationKind.STAGE_OUTPUT:
            # Persist the findings artifact and expose it to downstream fixing as {review_path}
            # (parity with the legacy ``_write_review``). The parity flow has a single stage_output
            # evaluator (``review``); the per-evaluator verdict store for supervisor/test_quality is
            # P2.1.
            self._write_findings(ctx, raw_findings, outcome.result.final_message)
        findings = tuple(
            Finding(message=str(f.get("title", f)), blocking=self._is_blocking(f))
            for f in raw_findings
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

    def _verdict(self, node: EvaluatorNode, outcome: StageOutcome) -> str:
        if node.evaluation_kind == EvaluationKind.FINAL_HANDOFF:
            return "done"
        if not node.blocking or outcome.result is None:
            return "accept"
        findings = self._extract_findings(outcome.result.structured_output)
        return "rework" if any(self._is_blocking(f) for f in findings) else "accept"

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
            stage_run_id=run_id,
            task_path=self._in.task_path,
            plan_path=self._in.plan_path,
            diff_path=self._in.diff_path,
            check_artifacts_path=self._in.checks_path,
            review_artifacts_path=self._in.review_path,
            output_schema=stage_output_schema(stage),
            model=node.model,
            reasoning=node.reasoning,
            session_id=self._in.session_ids.get(route.primary.value),
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
