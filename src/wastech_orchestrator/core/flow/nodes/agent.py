"""Agent node runner (P1.3/P1.4) — thin adapter to the AgentRouter.

Builds an :class:`~wastech_orchestrator.providers.base.AgentRunRequest` from the node fields + the
unit inputs (the node's ``role_file`` is the prompt template; only allowlisted path variables are
injected), runs it through the router, records a ``node_runs`` row, and returns an unconditional
``done`` outcome. Infra-exhaustion (no provider completed the stage) raises
:class:`~.base.NodeInfraError`; a quality-failed result flows on (the downstream evaluator/checks
judge quality).

Two core-owned behaviors wrap the run:

* **Embedded HITL** (refinement/planning): the typed output may carry a human question/approval;
  the runner does at most one durable round-trip via :class:`~.human_gate.HumanGate` and re-runs the
  stage with the answer, resuming a persisted interaction after a restart.
* **Dangerous-diff guard** (after a ``workspace-write`` edit): write the diff (``{diff_path}``) and
  classify deletion/dependency changes; a dangerous diff requires a durable human approval (a
  matching planning pre-approval counts), and on denial reconsiders once before failing closed to
  manual review.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from wastech_orchestrator.core.dangerous_diff import DangerousDiff, classify_dangerous_diff
from wastech_orchestrator.core.flow.contracts import PermissionProfile, SessionScope
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInfraError,
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.human_gate import HumanGate
from wastech_orchestrator.core.flow.observability import record_run_observability
from wastech_orchestrator.core.flow.output_policy import resolve_output_policy, within_subdir
from wastech_orchestrator.core.flow.prompt import render_role_prompt
from wastech_orchestrator.core.flow.schema import AgentNode, FlowNode
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    OutputContract,
    StageOutputError,
    TypedStageOutput,
    guardrail_interaction_path,
    interaction_path,
    load_interaction,
    mark_consumed,
    mark_interaction_status,
    parse_typed_output,
    typed_output_schema,
)
from wastech_orchestrator.notify import AskKind, AskResult
from wastech_orchestrator.providers.base import AgentRunRequest
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import EditingLineageRow, NodeRunRow


class AgentNodeRunner:
    """Run an ``agent`` node through the router (constructed per unit with its services/inputs)."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, AgentNode)
        route = self._s.router.resolve_route(node.id, node.provider)
        try:
            if _wants_hitl(node):
                return self._run_with_hitl(node, ctx, route)
            return self._run_simple(node, ctx, route)
        except NodeInfraError:
            if not node.best_effort:
                raise
            # Best-effort node (summary, §5.2): the failed attempt is already recorded; continue
            # with no output so the downstream fallback (the minimal summary) applies.
            return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=0)

    # -- simple (non-HITL) agent run ------------------------------------------

    def _run_simple(self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute) -> NodeResult:
        run_id, outcome = self._invoke(node, ctx, route, human_input_path=None)
        self._apply_post_edit_guard(node, ctx, route)
        return NodeResult(node_id=node.id, outcome=_agent_outcome(outcome), node_run_id=run_id)

    # -- embedded HITL (refinement / planning) --------------------------------

    def _run_with_hitl(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute
    ) -> NodeResult:
        path = interaction_path(
            self._s.artifacts_root, ctx.task_id, node.id, subtask=ctx.subtask_order
        )
        persisted = load_interaction(path)
        had_interaction = persisted is not None
        human_input_path: str | None = None
        if persisted is not None:
            human_input_path = self._resume_interaction(node, path, persisted)

        run_id, outcome = self._invoke(node, ctx, route, human_input_path=human_input_path)
        typed = self._typed(node, ctx, outcome)
        if typed.human_input is None:
            if had_interaction:
                mark_consumed(path)
            return NodeResult(node_id=node.id, outcome=_agent_outcome(outcome), node_run_id=run_id)
        if had_interaction:
            raise NodeManualRequired(f"agent node {node.id!r}: unexpected repeated HITL request")

        # First-time signal: one durable round-trip, then re-run with the answer.
        result = self._gate().request(
            task_id=ctx.task_id,
            node_id=node.id,
            subtask=ctx.subtask_order,
            signal=typed.human_input,
            path=path,
        )
        self._require_human(node, typed.human_input.kind, result)
        run_id2, outcome2 = self._invoke(node, ctx, route, human_input_path=str(path))
        if self._typed(node, ctx, outcome2).human_input is not None:
            raise NodeManualRequired(f"agent node {node.id!r}: second HITL request after an answer")
        mark_consumed(path)
        return NodeResult(node_id=node.id, outcome=_agent_outcome(outcome2), node_run_id=run_id2)

    def _resume_interaction(
        self, node: AgentNode, path: Any, persisted: Mapping[str, Any]
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

    def _typed(self, node: AgentNode, ctx: NodeContext, outcome: StageOutcome) -> TypedStageOutput:
        result = outcome.result
        if result is None:  # defensive: _invoke already raised on infra-exhaustion
            raise NodeInfraError(f"agent node {node.id!r}: no result to parse")
        try:
            return parse_typed_output(self._contract(node, ctx), result.structured_output)
        except StageOutputError as exc:
            raise NodeInfraError(
                f"agent node {node.id!r}: invalid structured output: {exc}"
            ) from exc

    def _contract(self, node: AgentNode, ctx: NodeContext) -> OutputContract:
        """The node's typed-output contract, derived from declared structure (never a stage name).

        The decomposition proposer emits the ``planning`` contract (content + human_input +
        decompose/subtasks/skills); any other HITL-capable node emits ``human_input`` (content +
        human_input); a plain author node emits ``none``.
        """
        decomp = ctx.snapshot.doc.decomposition
        if decomp is not None and node.id == decomp.proposed_by:
            return "planning"
        if _wants_hitl(node):
            return "human_input"
        return "none"

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
        route: ResolvedRoute,
        *,
        human_input_path: str | None,
    ) -> tuple[int, StageOutcome]:
        started_at = self._s.clock()
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
                started_at=started_at,
            )
        )
        request = self._build_request(node, ctx, route, run_id, human_input_path)
        outcome = self._s.router.run_stage(request, route, snapshot=self._s.snapshot)
        self._record_completion(run_id, outcome)
        record_run_observability(
            self._s,
            task_id=ctx.task_id,
            node_id=node.id,
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
            raise NodeInfraError(f"agent node {node.id!r}: no provider could complete it ({err})")
        self._persist_session(node, ctx, outcome)
        return run_id, outcome

    def _apply_post_edit_guard(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute
    ) -> None:
        """After a workspace-write edit, write the diff (``{diff_path}``) and gate dangerous diffs.

        Core-owned and automatic — the flow never declares or disables it (flow-contract §2.1). A
        deletion/dependency diff requires a durable human approval (or a matching planning
        pre-approval); on denial the stage reconsiders once with the denial context and, if the diff
        is still dangerous, fails closed to manual review.
        """
        resolved = node.permission_profile or ctx.snapshot.doc.permission_ceiling
        if resolved != PermissionProfile.WORKSPACE_WRITE or self._s.git is None:
            return
        self._in.diff_path = self._s.git.write_current_diff(ctx.task_id)
        if self._s.register_artifact is not None:
            self._s.register_artifact(ctx.task_id, "diff", self._in.diff_path)
        self._apply_output_containment_guard(node, ctx)
        dangerous = classify_dangerous_diff(self._s.git.changed_code_entries())
        if dangerous is None:
            return
        path = guardrail_interaction_path(
            self._s.artifacts_root,
            ctx.task_id,
            node.id,
            subtask=ctx.subtask_order,
            cycle=ctx.run_state.fix_iterations,
        )
        persisted = load_interaction(path)
        if persisted is not None:
            approved = self._resume_guardrail(node, path, persisted, dangerous)
        elif self._planning_approval_matches(ctx, dangerous):
            return  # the dangerous diff was pre-approved during planning
        else:
            result = self._gate().request(
                task_id=ctx.task_id,
                node_id=node.id,
                subtask=ctx.subtask_order,
                signal=_dangerous_diff_signal(node.id, dangerous),
                path=path,
            )
            self._require_human(node, "approval", result)
            approved = result.approved is True
        if approved:
            mark_consumed(path)
            return
        self._reconsider(node, ctx, route, path)

    def _apply_output_containment_guard(self, node: AgentNode, ctx: NodeContext) -> None:
        """After a workspace-write edit, enforce the flow's ``output_policy`` write containment.

        For a document/report flow the only writable area is the resolved report directory: a write
        anywhere else (a tracked or untracked code change outside it) fails closed to manual review.
        For ``private_control_workspace_report`` the report lives under the gitignored ``.worc/``
        home, so it never appears in ``changed_code_entries`` — the guard then requires the tracked
        tree to be byte-for-byte unchanged (any tracked/untracked code change is an escape).
        ``code_change`` flows have no report directory and rely on the dangerous-diff guard instead.
        """
        policy = resolve_output_policy(ctx.snapshot.doc.output_policy, ctx.task_id)
        if policy.report_subdir is None or self._s.git is None:
            return
        offenders = [
            entry.path
            for entry in self._s.git.changed_code_entries()
            if not within_subdir(entry.path, policy.report_subdir)
        ]
        if offenders:
            raise NodeManualRequired(
                f"agent node {node.id!r}: output_policy {policy.policy.value!r} confines writes to "
                f"{policy.report_subdir!r}; refusing changes outside it: {sorted(offenders)}"
            )

    def _resume_guardrail(
        self, node: AgentNode, path: Any, persisted: Mapping[str, Any], dangerous: DangerousDiff
    ) -> bool:
        if not _guardrail_request_matches(persisted, dangerous):
            raise NodeManualRequired(
                f"agent node {node.id!r}: dangerous diff expanded after its approval request"
            )
        status = str(persisted.get("status", ""))
        if status == "waiting":
            result = self._gate().resume(path, dict(persisted))
            self._require_human(node, "approval", result)
            return result.approved is True
        if status in ("answered", "consumed"):
            self._require_persisted_human(node, persisted)
            return persisted.get("approved") is True
        raise NodeManualRequired(
            f"agent node {node.id!r}: cannot resume dangerous-diff approval from status {status!r}"
        )

    def _reconsider(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute, path: Any
    ) -> None:
        """Approval denied: re-run the node with the denial context, then re-classify."""
        mark_interaction_status(path, "reconsidering")
        self._invoke(node, ctx, route, human_input_path=str(path))
        assert self._s.git is not None
        self._in.diff_path = self._s.git.write_current_diff(ctx.task_id)
        if classify_dangerous_diff(self._s.git.changed_code_entries()) is not None:
            raise NodeManualRequired(
                f"agent node {node.id!r}: retained dangerous changes after approval was denied"
            )
        mark_interaction_status(path, "reconsidered")

    def _planning_approval_matches(self, ctx: NodeContext, dangerous: DangerousDiff) -> bool:
        """A dangerous diff is pre-approved if the planning node (the decomposition proposer) got a
        matching human approval. No decomposition config → no planning node → no pre-approval."""
        decomp = ctx.snapshot.doc.decomposition
        if decomp is None:
            return False
        path = interaction_path(self._s.artifacts_root, ctx.task_id, decomp.proposed_by)
        persisted = load_interaction(path)
        if persisted is None or persisted.get("approved") is not True:
            return False
        return _guardrail_request_matches(persisted, dangerous)

    def _build_request(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        run_id: int,
        human_input_path: str | None,
    ) -> AgentRunRequest:
        ceiling = ctx.snapshot.doc.permission_ceiling
        permission = (node.permission_profile or ceiling).value
        prompt = render_role_prompt(
            self._in.flow_dir, node.role_file, self._prompt_variables(ctx, node)
        )
        output_schema = (
            json.loads(node.output_schema)
            if node.output_schema
            else typed_output_schema(self._contract(node, ctx))
        )
        return AgentRunRequest(
            task_id=ctx.task_id,
            node_id=node.id,
            working_directory=self._s.repo_dir,
            prompt=prompt,
            permission_profile=permission,
            timeout_seconds=node.timeout_seconds or self._s.default_timeout_seconds,
            attempt=1,
            node_run_id=run_id,
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
            session_id=self._resume_session_id(node, ctx, route),
            # Network is a flow-ceiling dimension: every node may reach the network iff the flow
            # declares a ``network_policy`` (advisories/research); absence = no network (P3.2).
            network_access=ctx.snapshot.doc.network_policy is not None,
        )

    def _prompt_variables(self, ctx: NodeContext, node: AgentNode) -> dict[str, object | None]:
        variables: dict[str, object | None] = {
            "task_id": ctx.task_id,
            "stage": node.id,
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

    def _resume_session_id(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute
    ) -> str | None:
        """The durable editing session to resume for this node (durable sessions, P2.2).

        Only an ``editing_lineage`` node resumes the execution unit's editing session, and only when
        the stored lineage was produced by the same provider it resolves to (you cannot resume a
        Claude session on Codex). ``fresh_disposable`` always starts clean; ``resume_own_lineage``
        (a node's own multi-round session) is not used by the implementation flow's author nodes.
        ``lineage_affinity`` is realized here: every editing node on the unit shares this one
        session, so ``fixing`` (affinity → ``implementation``) continues the session that
        ``implementation`` established."""
        if node.session_scope is not SessionScope.EDITING_LINEAGE:
            return None
        row = self._s.store.get_editing_lineage(ctx.task_id, ctx.subtask_order)
        if row is None or row.provider != route.primary.value:
            return None  # no editing session yet, or it belongs to a different provider → fresh
        return row.raw_session_id

    def _persist_session(self, node: AgentNode, ctx: NodeContext, outcome: StageOutcome) -> None:
        """Persist the unit's editing session after a successful editing-lineage run (durable).

        A ``fresh_disposable`` / ``resume_own_lineage`` node never writes the editing lineage, so it
        cannot leak its session into a later author node (validator-enforced read-only evaluators
        never reach here). The raw session id is stored ONLY in ``state.db``."""
        if node.session_scope is not SessionScope.EDITING_LINEAGE:
            return
        result = outcome.result
        if result is None or not result.session_id or outcome.provider_used is None:
            return
        self._s.store.upsert_editing_lineage(
            EditingLineageRow(
                task_id=ctx.task_id,
                subtask_order=ctx.subtask_order,
                provider=outcome.provider_used.value,
                raw_session_id=result.session_id,
                updated_at=self._s.clock(),
            )
        )


def _agent_outcome(outcome: StageOutcome) -> NodeOutcome:
    """An agent node's unconditional ``done`` outcome, carrying the agent output so the post-node
    hook can persist an ``output_artifact`` slot / read the decomposition contract. ``_invoke`` has
    already raised on infra-exhaustion, so ``result`` is present here; guard defensively anyway."""
    result = outcome.result
    return NodeOutcome(
        "done",
        structured_output=result.structured_output if result is not None else None,
        final_message=result.final_message if result is not None else None,
    )


def _wants_hitl(node: AgentNode) -> bool:
    """A node opts into the durable HITL round-trip by declaring ``hitl`` with a capability flag.

    Data-driven (flow-contract §2.1): the *decision* to do a human round-trip is the node's
    declared ``hitl`` settings, never the stage name. The typed-output schema/parsing that follows
    is selected by the node's derived :data:`~wastech_orchestrator.core.hitl.OutputContract`
    (see :meth:`AgentNodeRunner._contract`), also never the stage name.
    """
    return node.hitl is not None and (node.hitl.allow_question or node.hitl.allow_approval)


def _dangerous_diff_signal(node_id: str, dangerous: DangerousDiff) -> HumanInputSignal:
    detail: list[str] = []
    if dangerous.deleted_paths:
        detail.append("Deleted paths: " + ", ".join(dangerous.deleted_paths))
    if dangerous.dependency_paths:
        detail.append("Dependency manifests/locks: " + ", ".join(dangerous.dependency_paths))
    return HumanInputSignal(
        kind="approval",
        question=f"Approve dangerous changes produced by the {node_id!r} node?",
        context="\n".join(detail),
        risk=dangerous.risk,
        paths=dangerous.paths,
    )


def _guardrail_request_matches(persisted: Mapping[str, Any], dangerous: DangerousDiff) -> bool:
    request = persisted.get("request")
    if not isinstance(request, Mapping):
        return False
    paths = request.get("paths")
    return (
        request.get("kind") == "approval"
        and request.get("risk") == dangerous.risk
        and isinstance(paths, list)
        and tuple(sorted(str(path) for path in paths)) == dangerous.paths
    )


def _persisted_kind(persisted: Mapping[str, Any]) -> AskKind | None:
    request = persisted.get("request")
    if isinstance(request, Mapping):
        kind = request.get("kind")
        if kind == "question":
            return "question"
        if kind == "approval":
            return "approval"
    return None
