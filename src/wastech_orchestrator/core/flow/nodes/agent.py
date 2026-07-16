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

from wastech_orchestrator.core.dangerous_diff import (
    DangerousDiff,
    evaluate_diff_gate,
)
from wastech_orchestrator.core.flow.context_paths import build_path_context
from wastech_orchestrator.core.flow.contracts import (
    PermissionProfile,
    SessionScope,
    resolve_network_access,
)
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
from wastech_orchestrator.core.flow.prompt import RoleFileError, read_role_file, render_role_prompt
from wastech_orchestrator.core.flow.prompt_vars import valid_prompt_vars
from wastech_orchestrator.core.flow.schema import AgentNode, FlowNode, ToolNode
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    OutputContract,
    StageOutputError,
    TypedStageOutput,
    guardrail_interaction_path,
    interaction_path,
    iter_task_interactions,
    load_interaction,
    mark_consumed,
    mark_interaction_status,
    parse_typed_output,
    turn_gate_interaction_path,
    typed_output_schema,
)
from wastech_orchestrator.notify import AskKind, AskResult
from wastech_orchestrator.providers.artifacts import (
    TOOL_STDOUT_FILENAME,
    latest_run_file,
    task_artifact_dir,
)
from wastech_orchestrator.providers.base import (
    MAX_TURNS_SUBTYPE,
    AgentRunRequest,
    ErrorClass,
    ProviderId,
    build_effective_prompt,
)
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
            # Best-effort node (summary): the failed attempt is already recorded; continue
            # with no output so the downstream fallback (the minimal summary) applies.
            return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=0)

    # -- simple (non-HITL) agent run ------------------------------------------

    def _run_simple(self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute) -> NodeResult:
        run_id, outcome = self._invoke_with_turn_gate(node, ctx, route, human_input_path=None)
        self._apply_post_edit_guard(node, ctx, route)
        return NodeResult(node_id=node.id, outcome=_agent_outcome(outcome), node_run_id=run_id)

    # -- embedded HITL (refinement / planning) --------------------------------

    def _run_with_hitl(self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute) -> NodeResult:
        path = interaction_path(
            self._s.artifacts_root, ctx.task_id, node.id, subtask=ctx.subtask_order
        )
        persisted = load_interaction(path)
        had_interaction = persisted is not None
        human_input_path: str | None = None
        if persisted is not None:
            human_input_path = self._resume_interaction(node, path, persisted)

        run_id, outcome = self._invoke_with_turn_gate(
            node, ctx, route, human_input_path=human_input_path
        )
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
        # Resume the first run's session so the agent continues the same conversation with the
        # operator's answer (it does not re-derive from scratch). Same-provider only; across a
        # restart the first-run outcome is gone, so resume falls back to fresh + the answer file.
        run_id2, outcome2 = self._invoke_with_turn_gate(
            node,
            ctx,
            route,
            human_input_path=str(path),
            resume_session_id=_same_provider_session_id(outcome, route),
        )
        if self._typed(node, ctx, outcome2).human_input is not None:
            raise NodeManualRequired(f"agent node {node.id!r}: second HITL request after an answer")
        mark_consumed(path)
        return NodeResult(node_id=node.id, outcome=_agent_outcome(outcome2), node_run_id=run_id2)

    def _resume_interaction(self, node: AgentNode, path: Any, persisted: Mapping[str, Any]) -> str:
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
            self._s.notifier,
            timeout_s=self._s.ask_timeout_s,
            contacts=self._in.contacts,
            heartbeat_seconds=self._s.ask_heartbeat_seconds,
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

    # -- max-turns gate (idea 29) ---------------------------------------------

    def _invoke_with_turn_gate(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        *,
        human_input_path: str | None,
        resume_session_id: str | None = None,
    ) -> tuple[int, StageOutcome]:
        """Invoke the provider; when the Claude max-turns gate is on, pause on ``error_max_turns``
        for a durable operator continue/stop decision instead of failing immediately.

        Continue resumes the same agent session with a fresh turn grant (the adapter re-passes
        ``--max-turns`` on every invocation). Deny / timeout / no transport → STOP: the original
        max-turns failure is returned unchanged (terminal exactly as it is with the gate off). The
        gate is fail-closed across a daemon restart: a pending decision left ``waiting`` by an
        interrupted run is resolved at entry, before the provider is touched, so a restart never
        silently burns a fresh grant. Reuses the existing durable HITL primitive — no new transport.
        """
        if not self._s.max_turns_gate:
            return self._invoke(
                node,
                ctx,
                route,
                human_input_path=human_input_path,
                resume_session_id=resume_session_id,
            )
        gate_path = turn_gate_interaction_path(
            self._s.artifacts_root, ctx.task_id, node.id, subtask=ctx.subtask_order
        )
        persisted = load_interaction(gate_path)
        if persisted is not None and str(persisted.get("status", "")) == "waiting":
            result = self._gate().resume(gate_path, dict(persisted))
            if not _gate_approved(result):
                # Deny / timeout / no answer after a restart → fail-closed terminal (manual review):
                # the original max-turns outcome is gone, so there is nothing to return as today.
                reason = result.failure or ("denied" if result.answered else "no answer")
                raise NodeManualRequired(
                    f"agent node {node.id!r}: max-turns gate stopped after restart ({reason})"
                )
            mark_consumed(gate_path)
            # The in-memory session is gone after a restart; let _invoke fall back to the durable
            # editing-lineage session (state.db) for editing nodes, else a fresh run + fresh grant.
            resume_session_id = None
        run_id, outcome = self._invoke(
            node, ctx, route, human_input_path=human_input_path, resume_session_id=resume_session_id
        )
        while _is_max_turns(outcome):
            result = self._gate().request(
                task_id=ctx.task_id,
                node_id=node.id,
                subtask=ctx.subtask_order,
                signal=_turn_limit_signal(node.id),
                path=gate_path,
            )
            if not _gate_approved(result):
                return run_id, outcome  # deny / timeout / transport → STOP (terminal as today)
            mark_consumed(gate_path)
            run_id, outcome = self._invoke(
                node,
                ctx,
                route,
                human_input_path=human_input_path,
                resume_session_id=_same_provider_session_id(outcome, route),
            )
        return run_id, outcome

    # -- shared invocation ----------------------------------------------------

    def _invoke(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        *,
        human_input_path: str | None,
        resume_session_id: str | None = None,
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
        request = self._build_request(node, ctx, route, run_id, human_input_path, resume_session_id)
        outcome = self._s.router.run_stage(request, route, snapshot=self._s.snapshot)
        self._record_completion(run_id, outcome)
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
        )
        if outcome.result is None:
            error_class = outcome.terminal_error.error_class if outcome.terminal_error else None
            err = error_class.value if error_class else "no_provider_available"
            raise NodeInfraError(
                f"agent node {node.id!r}: no provider could complete it ({err})",
                error_class=error_class,
            )
        if (
            outcome.result.error is not None
            and outcome.result.error.error_class is ErrorClass.POLICY_DENIED
        ):
            raise NodeManualRequired(
                f"agent node {node.id!r}: provider policy denied a requested operation"
            )
        self._persist_session(node, ctx, outcome)
        return run_id, outcome

    def _apply_post_edit_guard(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute
    ) -> None:
        """After a workspace-write edit, write the diff (``{diff_path}``) and gate dangerous diffs.

        Core-owned and automatic — the flow never declares or disables it. A
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
        entries = self._s.git.changed_code_entries()
        dangerous = evaluate_diff_gate(entries, self._s.trust_level, self._s.protected_paths)
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
        elif self._already_approved_in_task(ctx, dangerous):
            return  # this exact dangerous diff was already approved earlier in the task
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
        # Re-evaluate under the same policy the request used, so the reconsider pass agrees on which
        # changes gate (level + protected floor) and does not spuriously flag a now-allowed change.
        still_dangerous = evaluate_diff_gate(
            self._s.git.changed_code_entries(), self._s.trust_level, self._s.protected_paths
        )
        if still_dangerous is not None:
            raise NodeManualRequired(
                f"agent node {node.id!r}: retained dangerous changes after approval was denied"
            )
        mark_interaction_status(path, "reconsidered")

    def _already_approved_in_task(self, ctx: NodeContext, dangerous: DangerousDiff) -> bool:
        """True if the operator already approved this exact dangerous diff earlier in the task.

        The dangerous-diff classifier runs over the whole *uncommitted* working-tree diff, so a
        second workspace-write node (``documentation`` after ``implementation``, or ``fixing`` in a
        re-test loop) re-sees a deletion/dependency change an upstream node already got cleared.
        Honoring any prior in-task approval of the identical change (same risk + exact path set) —
        the planning pre-approval, or an earlier node's guardrail approval — keeps the guard from
        re-prompting for it. A new or expanded dangerous set does not match, so it still prompts:
        the guard never weakens, it only avoids asking twice for the same approved change.
        """
        return any(
            persisted.get("approved") is True and _guardrail_request_matches(persisted, dangerous)
            for persisted in iter_task_interactions(self._s.artifacts_root, ctx.task_id)
        )

    def _build_request(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        run_id: int,
        human_input_path: str | None,
        resume_session_id: str | None = None,
    ) -> AgentRunRequest:
        ceiling = ctx.snapshot.doc.permission_ceiling
        permission = (node.permission_profile or ceiling).value
        # The renderer stays the fixed security core; the caller widens *which names* it may
        # substitute to the flow-derived set (core allowlist ∪ each agent node's {<id>_path}), and
        # only ever places path values in the dict — never inlined content.
        prompt = render_role_prompt(
            self._in.flow_dir,
            node.role_file,
            self._prompt_variables(ctx, node),
            allowed=valid_prompt_vars(ctx.snapshot),
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
            skill_reference_paths=self._in.skills_for(node.id),
            output_schema=output_schema,
            model=node.model,
            reasoning=node.reasoning,
            extra_args=list(node.extra_args),
            # An explicit HITL resume id (the in-process round-trip) wins; otherwise fall back to
            # the durable editing-lineage session. For an editing-lineage node these agree (the
            # explicit value is the just-persisted lineage session).
            session_id=resume_session_id or self._resume_session_id(node, ctx, route),
            # Network is a per-node override on top of the flow-wide default: the node's
            # ``network_access`` wins (a node-level grant works even in a flow with no
            # ``network_policy``; a node-level ``False`` opts out), and absent it the node inherits
            # the flow's ``network_policy`` default (P3.2). It only toggles network — never the
            # filesystem permission ceiling.
            network_access=resolve_network_access(
                node.network_access, ctx.snapshot.doc.network_policy
            ),
        )

    def _prompt_variables(self, ctx: NodeContext, node: AgentNode) -> dict[str, object | None]:
        # The allowlisted artifact paths come from the shared collector (seam #4) so the agent
        # prompt and the tool-node stdin never drift; the rest (ids, skills, memory) is prompt-only.
        paths = build_path_context(self._in, self._s.repo_dir)
        variables: dict[str, object | None] = {
            "task_id": ctx.task_id,
            "stage": node.id,
            "repo_path": paths["repo"],
            **paths,
            "skills_path": "\n".join(self._in.skills_for(node.id)) or None,
            "memory_path": self._memory_path(node, ctx),
        }
        if ctx.subtask_order is not None:
            variables["subtask_order"] = ctx.subtask_order
            variables["subtask_count"] = self._in.subtask_count
            variables["subtask_spec_path"] = self._in.subtask_spec_path
            variables["predecessor_context"] = self._predecessor_context(node, ctx)
        variables.update(self._node_output_paths(ctx))
        return variables

    def _predecessor_context(self, node: AgentNode, ctx: NodeContext) -> str | None:
        """Return the subtask handoff-brief path for ``{predecessor_context}`` — node-driven.

        Returns the assembled path (set on ``NodeInputs`` by the orchestrator's decomposition
        fan-out) only when a decompose region is active (``ctx.subtask_order`` set), the current
        subtask has a handoff assembled — the orchestrator sets the path only for a subtask with ≥1
        ``depends_on`` predecessor — AND this node's (operator-editable) role prompt references
        ``{predecessor_context}``. Otherwise ``None`` (the conditional block drops). Best-effort: a
        role file that cannot be read degrades to no context (mirrors :meth:`_memory_path`)."""
        path = self._in.predecessor_context_path
        if path is None:
            return None
        try:
            template = read_role_file(self._in.flow_dir, node.role_file)
        except RoleFileError:
            return None
        if "{predecessor_context}" not in template and "{?predecessor_context}" not in template:
            return None
        return path

    def _node_output_paths(self, ctx: NodeContext) -> dict[str, object | None]:
        """The generic ``{<node_id>_path}`` variables for every agent + tool node in the flow.

        A value resolves to the node's **latest** persisted output (an agent's ``<node_id>.out.md``
        or a tool's redacted ``stdout.txt``) — now kept per-run under
        ``stages/<node_id>/run-<id>/`` — so a re-running upstream node exposes its most recent pass
        while every earlier pass stays on disk. A not-yet-run or special-slot node's variable is
        empty (``None``) and a ``{?<id>_path}…{/<id>_path}`` block drops cleanly. Fan-in is free: a
        node names each upstream output it wants (``{scan_path}``, ``{md-check_path}``). The stored
        value is a POSIX path string (cross-platform). Only agent and tool nodes get this channel
        (node-output ADR + P5)."""
        paths: dict[str, object | None] = {}
        for other in ctx.snapshot.doc.nodes:
            if isinstance(other, AgentNode):
                filename = f"{other.id}.out.md"
            elif isinstance(other, ToolNode):
                filename = TOOL_STDOUT_FILENAME
            else:
                continue
            found = latest_run_file(self._s.artifacts_root, ctx.task_id, other.id, filename)
            paths[f"{other.id}_path"] = found.as_posix() if found is not None else None
        return paths

    def _memory_path(self, node: AgentNode, ctx: NodeContext) -> str | None:
        """Build this node's memory packet and return its path — node-driven (FR4/D5).

        Returns the per-node packet path only when memory is enabled AND the node's (operator-
        editable) role prompt references ``{memory_path}``; otherwise ``None`` (so the variable
        renders empty and the conditional block drops). A node not referencing it never triggers a
        build, so a custom operator node opts in with no Core change. Best-effort: a memory read
        must never break a node run, so any failure degrades to no packet (AC-R4)."""
        builder = self._s.packet_builder
        if builder is None:  # memory disabled (Q10) — no store, empty variable, today's behavior
            return None
        try:
            template = read_role_file(self._in.flow_dir, node.role_file)
        except RoleFileError:
            return None  # render_role_prompt surfaces the real read error
        if "{memory_path}" not in template and "{?memory_path}" not in template:
            return None
        # F48: this task's changed paths (per-task chain base), not the whole shared branch's, so
        # the packet's path-overlap ranking stays relevant on a chain branch.
        touched = (
            self._s.git.changed_code_paths_since_task_base() if self._s.git is not None else []
        )
        dest = task_artifact_dir(self._s.artifacts_root, ctx.task_id) / "memory" / f"{node.id}.md"
        written = builder.write_packet(
            node_id=node.id, task_type=self._in.task_type, touched_paths=touched, dest=dest
        )
        return str(written) if written is not None else None

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

        Only an ``editing_lineage`` node resumes an editing session, keyed by its lineage
        (:func:`_lineage_key`), and only when the stored lineage was produced by the same provider
        it resolves to (you cannot resume a Claude session on Codex). ``fresh_disposable`` always
        starts clean; ``resume_own_lineage`` (a node's own multi-round session) is not used by the
        implementation flow's author nodes. ``lineage_affinity`` is realized here: a node with
        ``lineage_affinity: X`` resumes lineage ``X`` (so ``fixing`` continues the session
        ``implementation`` established), while an affinity-less editing node keys its own lineage
        and stays isolated from the others on the unit."""
        if node.session_scope is not SessionScope.EDITING_LINEAGE:
            return None
        row = self._s.store.get_editing_lineage(ctx.task_id, _lineage_key(node), ctx.subtask_order)
        if row is None or row.provider != route.primary.value:
            return None  # no editing session yet, or it belongs to a different provider → fresh
        return row.raw_session_id

    def _persist_session(self, node: AgentNode, ctx: NodeContext, outcome: StageOutcome) -> None:
        """Persist this node's editing lineage after a successful editing-lineage run (durable).

        A ``fresh_disposable`` / ``resume_own_lineage`` node never writes the editing lineage, so it
        cannot leak its session into a later author node (validator-enforced read-only evaluators
        never reach here). The lineage is keyed by :func:`_lineage_key`, so a node that joins
        another lineage writes back to it. The raw session id is stored ONLY in ``state.db``."""
        if node.session_scope is not SessionScope.EDITING_LINEAGE:
            return
        result = outcome.result
        if result is None or not result.session_id or outcome.provider_used is None:
            return
        self._s.store.upsert_editing_lineage(
            EditingLineageRow(
                task_id=ctx.task_id,
                lineage_key=_lineage_key(node),
                subtask_order=ctx.subtask_order,
                provider=outcome.provider_used.value,
                raw_session_id=result.session_id,
                updated_at=self._s.clock(),
            )
        )


def _lineage_key(node: AgentNode) -> str:
    """The editing-lineage key for a node: its ``lineage_affinity`` target, else its own id.

    An affinity-less ``editing_lineage`` node owns a lineage named after itself; a node with
    ``lineage_affinity: X`` joins lineage ``X``. Resume and persist MUST compute the same key, so
    they share this one helper. The validator forbids affinity chains (rule 7), so the target is
    always a lineage owner and one hop is enough — no transitive resolution here."""
    return node.lineage_affinity or node.id


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


def _same_provider_session_id(outcome: StageOutcome, route: ResolvedRoute) -> str | None:
    """The just-run session to resume on a same-route re-invoke, or ``None``.

    Used by both the embedded-HITL post-answer re-run and the max-turns gate's continue. Resume only
    when the session belongs to the route's primary provider: the re-invoke resolves the same route
    and leads with the primary, so a fallback-provider session cannot be resumed there (and the
    router drops ``session_id`` on a cross-provider fallback anyway). ``None`` everywhere else — no
    result, no session id, or a provider mismatch — yields a fresh session honestly."""
    result = outcome.result
    if result is None or not result.session_id or outcome.provider_used != route.primary:
        return None
    return result.session_id


def _is_max_turns(outcome: StageOutcome) -> bool:
    """True when the outcome is a Claude run that exhausted its ``max_turns`` cap (idea 29).

    Detected structurally via ``NormalizedError.failure_subtype`` — a quality ``task_failure`` the
    router returns as-is (no fallback), carrying the session id needed to resume on continue."""
    result = outcome.result
    return (
        result is not None
        and result.error is not None
        and outcome.provider_used is ProviderId.CLAUDE
        and result.error.failure_subtype == MAX_TURNS_SUBTYPE
    )


def _gate_approved(result: AskResult) -> bool:
    """True only on an explicit approval; deny / timeout / transport / invalid → False (STOP)."""
    return result.failure is None and result.answered and result.approved is True


def _turn_limit_signal(node_id: str) -> HumanInputSignal:
    """The continue/stop approval prompt for the max-turns gate — task/node ids only, no secrets."""
    return HumanInputSignal(
        kind="approval",
        question="Turn limit reached — continue this run?",
        context=(
            f"Node {node_id!r} hit its turn cap (max_turns). Approve to resume the same agent "
            "session with a fresh turn grant, or deny to stop the run."
        ),
        risk="other",
        paths=(),
    )


def _wants_hitl(node: AgentNode) -> bool:
    """A node opts into the durable HITL round-trip by declaring ``hitl`` with a capability flag.

    Data-driven: the *decision* to do a human round-trip is the node's
    declared ``hitl`` settings, never the stage name. The typed-output schema/parsing that follows
    is selected by the node's derived :data:`~wastech_orchestrator.core.hitl.OutputContract`
    (see :meth:`AgentNodeRunner._contract`), also never the stage name.
    """
    return node.hitl is not None and (node.hitl.allow_question or node.hitl.allow_approval)


def _dangerous_diff_signal(node_id: str, dangerous: DangerousDiff) -> HumanInputSignal:
    detail: list[str] = []
    if dangerous.protected_paths:
        detail.append(
            "Protected paths (always require approval): " + ", ".join(dangerous.protected_paths)
        )
    if dangerous.deleted_paths:
        detail.append("Deleted paths: " + ", ".join(dangerous.deleted_paths))
    if dangerous.dependency_paths:
        detail.append("Dependency manifests/locks: " + ", ".join(dangerous.dependency_paths))
    return HumanInputSignal(
        kind="approval",
        question=f"Approve changes requiring approval produced by the {node_id!r} node?",
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
