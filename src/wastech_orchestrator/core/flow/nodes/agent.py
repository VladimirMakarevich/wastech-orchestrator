"""Agent node runner (P1.3/P1.4) — thin adapter to the AgentRouter.

Builds an :class:`~wastech_orchestrator.providers.base.AgentRunRequest` from the node fields + the
unit inputs (the node's ``role_file`` is the prompt template; only allowlisted path variables are
injected), runs it through the router, records a ``node_runs`` row, and returns an unconditional
``done`` outcome. Infra-exhaustion (no provider completed the stage) raises
:class:`~.base.NodeInfraError`; a quality-failed result flows on (downstream evaluator/checks judge
quality), exactly like the legacy ``_run_stage`` + ``_require_result``.

Two core-owned behaviors wrap the run:

* **Embedded HITL** (refinement/planning): the typed output may carry a human question/approval;
  the runner does at most one durable round-trip via :class:`~.human_gate.HumanGate` and re-runs the
  stage with the answer, resuming a persisted interaction after a restart — a faithful port of
  ``_run_typed_stage``.
* **Dangerous-diff guard** (after a ``workspace-write`` edit): write the diff (``{diff_path}``) and
  classify deletion/dependency changes; a dangerous diff fails closed to manual review (the full
  durable approval round-trip mirroring ``_run_edit_stage_with_guardrail`` is the next step).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from wastech_orchestrator.core.dangerous_diff import classify_dangerous_diff
from wastech_orchestrator.core.flow.contracts import PermissionProfile
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInfraError,
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.human_gate import HumanGate
from wastech_orchestrator.core.flow.prompt import render_role_prompt
from wastech_orchestrator.core.flow.schema import AgentNode, FlowNode
from wastech_orchestrator.core.hitl import (
    StageOutputError,
    TypedStageOutput,
    interaction_path,
    load_interaction,
    mark_consumed,
    parse_typed_stage_output,
    stage_output_schema,
)
from wastech_orchestrator.notify import AskKind, AskResult
from wastech_orchestrator.providers.base import AgentRunRequest, Stage
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import NodeRunRow

_HITL_STAGES = frozenset({Stage.REFINEMENT, Stage.PLANNING})


class AgentNodeRunner:
    """Run an ``agent`` node through the router (constructed per unit with its services/inputs)."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, AgentNode)
        stage = self._s.stage_for_node[node.id]
        route = self._s.router.resolve_route(stage)
        if stage in _HITL_STAGES:
            return self._run_with_hitl(node, ctx, stage, route)
        return self._run_simple(node, ctx, stage, route)

    # -- simple (non-HITL) agent run ------------------------------------------

    def _run_simple(
        self, node: AgentNode, ctx: NodeContext, stage: Stage, route: ResolvedRoute
    ) -> NodeResult:
        run_id, _outcome = self._invoke(node, ctx, stage, route, human_input_path=None)
        self._apply_post_edit_guard(node, ctx)
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=run_id)

    # -- embedded HITL (refinement / planning) --------------------------------

    def _run_with_hitl(
        self, node: AgentNode, ctx: NodeContext, stage: Stage, route: ResolvedRoute
    ) -> NodeResult:
        path = interaction_path(
            self._s.artifacts_root, ctx.task_id, stage, subtask=ctx.subtask_order
        )
        persisted = load_interaction(path)
        had_interaction = persisted is not None
        human_input_path: str | None = None
        if persisted is not None:
            human_input_path = self._resume_interaction(node, stage, path, persisted)

        run_id, outcome = self._invoke(node, ctx, stage, route, human_input_path=human_input_path)
        typed = self._typed(node, stage, outcome)
        if typed.human_input is None:
            if had_interaction:
                mark_consumed(path)
            return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=run_id)
        if had_interaction:
            raise NodeManualRequired(f"agent node {node.id!r}: unexpected repeated HITL request")

        # First-time signal: one durable round-trip, then re-run with the answer.
        result = self._gate().request(
            task_id=ctx.task_id,
            stage=stage,
            subtask=ctx.subtask_order,
            signal=typed.human_input,
            path=path,
        )
        self._require_human(node, typed.human_input.kind, result)
        run_id2, outcome2 = self._invoke(node, ctx, stage, route, human_input_path=str(path))
        if self._typed(node, stage, outcome2).human_input is not None:
            raise NodeManualRequired(f"agent node {node.id!r}: second HITL request after an answer")
        mark_consumed(path)
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=run_id2)

    def _resume_interaction(
        self, node: AgentNode, stage: Stage, path: Any, persisted: Mapping[str, Any]
    ) -> str:
        status = str(persisted.get("status", ""))
        if status == "waiting":
            result = self._gate().resume(path, dict(persisted))
            self._require_human(node, _persisted_kind(persisted), result)
        elif status in ("answered", "consumed"):
            self._require_persisted_human(node, persisted)
        else:
            raise NodeManualRequired(
                f"agent node {node.id!r}: cannot resume HITL from status {status!r}"
            )
        return str(path)

    def _gate(self) -> HumanGate:
        if self._s.notifier is None:
            raise NodeManualRequired("HITL signal raised but no notifier transport is configured")
        return HumanGate(
            self._s.notifier, timeout_s=self._s.ask_timeout_s, contacts=self._in.contacts
        )

    def _typed(self, node: AgentNode, stage: Stage, outcome: StageOutcome) -> TypedStageOutput:
        result = outcome.result
        if result is None:  # defensive: _invoke already raised on infra-exhaustion
            raise NodeInfraError(f"agent node {node.id!r}: no result to parse")
        try:
            return parse_typed_stage_output(stage, result.structured_output)
        except StageOutputError as exc:
            raise NodeInfraError(
                f"agent node {node.id!r}: invalid structured output: {exc}"
            ) from exc

    def _require_human(self, node: AgentNode, kind: AskKind | None, result: AskResult) -> None:
        if result.failure is None and result.answered:
            if kind == "approval" and isinstance(result.approved, bool):
                return
            if kind == "question" and isinstance(result.text, str) and result.text.strip():
                return
        raise NodeManualRequired(
            f"agent node {node.id!r}: human input failed ({result.failure or 'invalid_response'})"
        )

    def _require_persisted_human(self, node: AgentNode, persisted: Mapping[str, Any]) -> None:
        if persisted.get("failure") is not None:
            raise NodeManualRequired(
                f"agent node {node.id!r}: human input failed ({persisted.get('failure')})"
            )
        kind = _persisted_kind(persisted)
        if kind == "approval" and isinstance(persisted.get("approved"), bool):
            return
        answer = persisted.get("answer")
        if kind == "question" and isinstance(answer, str) and answer.strip():
            return
        raise NodeManualRequired(f"agent node {node.id!r}: human input invalid")

    # -- shared invocation ----------------------------------------------------

    def _invoke(
        self,
        node: AgentNode,
        ctx: NodeContext,
        stage: Stage,
        route: ResolvedRoute,
        *,
        human_input_path: str | None,
    ) -> tuple[int, StageOutcome]:
        run_id = self._s.store.record_node_run(
            NodeRunRow(
                task_id=ctx.task_id,
                node_id=node.id,
                node_kind="agent",
                subtask_order=ctx.subtask_order,
                status="running",
                route_primary=route.primary.value,
                route_fallback=route.fallback.value if route.fallback else None,
                route_source=route.source.value,
                started_at=self._s.clock(),
            )
        )
        request = self._build_request(node, ctx, stage, route, run_id, human_input_path)
        outcome = self._s.router.run_stage(request, route, snapshot=self._s.snapshot)
        self._record_completion(run_id, outcome)
        if outcome.result is None:
            err = (
                outcome.terminal_error.error_class.value
                if outcome.terminal_error
                else "no_provider_available"
            )
            raise NodeInfraError(f"agent node {node.id!r}: no provider could complete it ({err})")
        self._update_session(outcome, route)
        return run_id, outcome

    def _apply_post_edit_guard(self, node: AgentNode, ctx: NodeContext) -> None:
        """After a workspace-write edit, write the diff (``{diff_path}``) and gate dangerous diffs.

        Core-owned and automatic — the flow never declares or disables it (flow-contract §2.1). The
        deletion/dependency classification is exact; the full durable approval round-trip (prompt /
        persist / resume / reconsider-on-denial, mirroring ``_run_edit_stage_with_guardrail``) is
        the next Step-B piece, so for now a dangerous diff fails closed to manual review.
        """
        resolved = node.permission_profile or ctx.snapshot.doc.permission_ceiling
        if resolved != PermissionProfile.WORKSPACE_WRITE or self._s.git is None:
            return
        self._in.diff_path = self._s.git.write_current_diff(ctx.task_id)
        dangerous = classify_dangerous_diff(self._s.git.changed_code_entries())
        if dangerous is not None:
            raise NodeManualRequired(
                f"agent node {node.id!r} produced a dangerous diff (risk={dangerous.risk}); "
                "the approval round-trip is a Step-B follow-up"
            )

    def _build_request(
        self,
        node: AgentNode,
        ctx: NodeContext,
        stage: Stage,
        route: ResolvedRoute,
        run_id: int,
        human_input_path: str | None,
    ) -> AgentRunRequest:
        ceiling = ctx.snapshot.doc.permission_ceiling
        permission = (node.permission_profile or ceiling).value
        prompt = render_role_prompt(
            self._in.flow_dir, node.role_file, self._prompt_variables(ctx, stage)
        )
        output_schema = (
            json.loads(node.output_schema) if node.output_schema else stage_output_schema(stage)
        )
        return AgentRunRequest(
            task_id=ctx.task_id,
            stage=stage,
            working_directory=self._s.repo_dir,
            prompt=prompt,
            permission_profile=permission,
            timeout_seconds=node.timeout_seconds or self._s.default_timeout_seconds,
            attempt=1,
            stage_run_id=run_id,
            task_path=self._in.task_path,
            plan_path=self._in.plan_path,
            diff_path=self._in.diff_path,
            check_artifacts_path=self._in.checks_path,
            review_artifacts_path=self._in.review_path,
            human_input_path=human_input_path,
            skill_reference_paths=self._in.skill_paths,
            output_schema=output_schema,
            model=node.model,
            reasoning=node.reasoning,
            extra_args=list(node.extra_args),
            session_id=self._in.session_ids.get(route.primary.value),
        )

    def _prompt_variables(self, ctx: NodeContext, stage: Stage) -> dict[str, object | None]:
        variables: dict[str, object | None] = {
            "task_id": ctx.task_id,
            "stage": stage.value,
            "repo_path": self._s.repo_dir,
            "repo": self._s.repo_dir,
            "task_path": self._in.task_path,
            "plan_path": self._in.plan_path,
            "diff_path": self._in.diff_path,
            "checks_path": self._in.checks_path,
            "review_path": self._in.review_path,
            "skills_path": "\n".join(self._in.skill_paths) or None,
        }
        if ctx.subtask_order is not None:
            variables["subtask_order"] = ctx.subtask_order
            variables["subtask_count"] = self._in.subtask_count
            variables["subtask_spec_path"] = self._in.subtask_spec_path
        return variables

    def _record_completion(self, run_id: int, outcome: StageOutcome) -> None:
        result = outcome.result
        if result is not None:
            status = result.status.value
            error_class = result.error.error_class.value if result.error else None
        else:
            status = "failed"
            error_class = (
                outcome.terminal_error.error_class.value if outcome.terminal_error else None
            )
        self._s.store.complete_node_run(
            run_id,
            status=status,
            outcome="done",
            provider_used=outcome.provider_used.value if outcome.provider_used else None,
            error_class=error_class,
            stage_attempts=outcome.stage_attempts,
            finished_at=self._s.clock(),
        )

    def _update_session(self, outcome: StageOutcome, route: ResolvedRoute) -> None:
        result = outcome.result
        if result is None or not result.session_id or outcome.provider_used is None:
            return
        self._in.session_ids[outcome.provider_used.value] = result.session_id
        if outcome.provider_used != route.primary:
            self._in.session_ids.pop(route.primary.value, None)


def _persisted_kind(persisted: Mapping[str, Any]) -> AskKind | None:
    request = persisted.get("request")
    if isinstance(request, Mapping):
        kind = request.get("kind")
        if kind == "question":
            return "question"
        if kind == "approval":
            return "approval"
    return None
