"""Agent node runner — thin adapter to the AgentRouter.

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
from dataclasses import dataclass, replace
from typing import Any

from wastech_orchestrator.core.dangerous_diff import (
    DangerousDiff,
    evaluate_diff_gate,
)
from wastech_orchestrator.core.flow.context_paths import (
    build_node_output_paths,
    build_path_context,
)
from wastech_orchestrator.core.flow.contracts import (
    PermissionProfile,
    SessionScope,
    resolve_git_evidence,
    resolve_network_access,
)
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInfraError,
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.diff_gate import (
    already_approved_in_task,
    dangerous_diff_signal,
    guardrail_request_matches,
)
from wastech_orchestrator.core.flow.nodes.exchange_publish import (
    assert_exchange_unchanged,
    assert_request_contained,
    capture_exchange_manifest,
    publish_artifact,
    publish_file,
)
from wastech_orchestrator.core.flow.nodes.human_gate import HumanGate
from wastech_orchestrator.core.flow.observability import record_run_observability
from wastech_orchestrator.core.flow.output_policy import resolve_output_policy, within_subdir
from wastech_orchestrator.core.flow.prompt import RoleFileError, read_role_file, render_role_prompt
from wastech_orchestrator.core.flow.prompt_vars import valid_prompt_vars
from wastech_orchestrator.core.flow.schema import AgentNode, FlowNode
from wastech_orchestrator.core.flow.usage_accounting import (
    deserialize_usage,
    guard_output_baseline,
    snapshot_for_lineage,
)
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
    sanitized_answer_packet,
    turn_gate_interaction_path,
    typed_output_schema,
)
from wastech_orchestrator.git_manager import ChangedPath, GitControlState
from wastech_orchestrator.notify import AskKind, AskResult
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import (
    MAX_TURNS_SUBTYPE,
    AgentRunRequest,
    NormalizedUsage,
    ProviderId,
    build_effective_prompt,
)
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import EditingLineageRow, NodeRunRow


@dataclass(frozen=True, slots=True)
class _GrantedShellBefore:
    """State captured before a read-only node with a granted shell runs, for reporting.

    Both fields exist to be compared against after the attempt: ``control`` catches a poisoned
    hook / ``.git/config`` / index, ``tree`` catches a stray working-tree write. Neither parks.
    """

    control: GitControlState
    tree: tuple[ChangedPath, ...]


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
        before = self._granted_shell_before(node, ctx, route)
        run_id, outcome, drift = self._invoke_with_turn_gate(
            node, ctx, route, human_input_path=None
        )
        drift = _merge_drift(drift, self._apply_post_edit_guard(node, ctx, route))
        return self._result(node, ctx, outcome, run_id, before, drift)

    # -- embedded HITL (refinement / planning) --------------------------------

    def _run_with_hitl(self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute) -> NodeResult:
        """Run the node with one embedded human round-trip, then gate its edit like any other.

        The post-edit guard runs on **both** returning exits — the answer-without-asking one and the
        one after the operator's answer — for the same reason ``_run_simple`` runs it: a
        workspace-write node's deletions and dependency edits need approval before the tests, and
        nothing about a HITL node makes its diff less dangerous. It ran on neither before, so a flow
        that declared ``hitl`` on a writing node published without ever asking; the guard is
        core-owned and automatic, and a flow cannot opt out of it by asking a question.
        """
        path = interaction_path(
            self._s.artifacts_root, ctx.task_id, node.id, subtask=ctx.subtask_order
        )
        persisted = load_interaction(path)
        had_interaction = persisted is not None
        human_input_path: str | None = None
        if persisted is not None:
            human_input_path = self._resume_interaction(node, ctx, path, persisted)

        before = self._granted_shell_before(node, ctx, route)
        run_id, outcome, drift = self._invoke_with_turn_gate(
            node, ctx, route, human_input_path=human_input_path
        )
        typed = self._typed(node, ctx, outcome)
        if typed.human_input is None:
            drift = _merge_drift(drift, self._apply_post_edit_guard(node, ctx, route))
            if had_interaction:
                mark_consumed(path)
            return self._result(node, ctx, outcome, run_id, before, drift)
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
        run_id2, outcome2, drift2 = self._invoke_with_turn_gate(
            node,
            ctx,
            route,
            human_input_path=self._exchange_human_input(node, ctx, path),
            resume_session_id=_same_provider_session_id(outcome, route),
            # Resuming the first run's session means its usage is the baseline: the second run's
            # cumulative includes the first's turns, so subtracting it recovers only the new work.
            resume_usage_baseline=outcome.result.normalized_usage if outcome.result else None,
        )
        if self._typed(node, ctx, outcome2).human_input is not None:
            raise NodeManualRequired(f"agent node {node.id!r}: second HITL request after an answer")
        # One bracket spans the node, so both attempts' drift is carried out together — the
        # operator is told the clone moved, not which half of a round-trip moved it.
        drift = _merge_drift(drift, drift2, self._apply_post_edit_guard(node, ctx, route))
        mark_consumed(path)
        return self._result(node, ctx, outcome2, run_id2, before, drift)

    def _resume_interaction(
        self, node: AgentNode, ctx: NodeContext, path: Any, persisted: Mapping[str, Any]
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
        return self._exchange_human_input(node, ctx, path)

    def _exchange_human_input(self, node: AgentNode, ctx: NodeContext, path: Any) -> str:
        """Publish the sanitized answer-only HITL packet to the exchange; return its path.

        The full durable interaction record (including the Telegram/durable transport handle) stays
        private; only the redacted ``{kind, question, answer, approved}`` projection becomes the
        provider-readable ``human_input_path``. Falls back to the private durable path when no
        exchange is wired (a unit harness)."""
        persisted = load_interaction(path)
        if persisted is None or not self._s.exchange_root:
            return str(path)
        suffix = f".sub-{ctx.subtask_order:02d}" if ctx.subtask_order is not None else ""
        return publish_artifact(
            self._s.exchange_root,
            ctx.task_id,
            f"hitl/{node.id}{suffix}.answer.json",
            json.dumps(sanitized_answer_packet(persisted), ensure_ascii=False, indent=2) + "\n",
            extra_secrets=self._s.prompt_secrets,
            private_path=str(path),
        )

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
        decompose/subtasks); any other HITL-capable node emits ``human_input`` (content +
        human_input); a plain author node emits ``none``. The proposer check comes first, so a
        proposer that also declares ``hitl:`` still gets ``planning`` (which carries human_input).
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

    # -- max-turns gate -------------------------------------------------------

    def _invoke_with_turn_gate(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        *,
        human_input_path: str | None,
        resume_session_id: str | None = None,
        resume_usage_baseline: NormalizedUsage | None = None,
    ) -> tuple[int, StageOutcome, str | None]:
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
                resume_usage_baseline=resume_usage_baseline,
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
        run_id, outcome, drift = self._invoke(
            node,
            ctx,
            route,
            human_input_path=human_input_path,
            resume_session_id=resume_session_id,
            resume_usage_baseline=resume_usage_baseline,
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
                # deny / timeout / transport → STOP (terminal as today)
                return run_id, outcome, drift
            mark_consumed(gate_path)
            run_id, outcome, more = self._invoke(
                node,
                ctx,
                route,
                human_input_path=human_input_path,
                resume_session_id=_same_provider_session_id(outcome, route),
            )
            drift = _merge_drift(drift, more)
        return run_id, outcome, drift

    # -- shared invocation ----------------------------------------------------

    def _is_workspace_write(self, node: AgentNode, ctx: NodeContext) -> bool:
        """True iff this node's resolved permission profile is workspace-write.

        Only a workspace-write attempt is *meant* to mutate the clone, so only it gets the post-edit
        diff guard (diff capture, output containment, the dangerous-diff approval). Whether an
        attempt *can* execute commands at all is a wider question — see :meth:`_can_run_commands`.
        """
        resolved = node.permission_profile or ctx.snapshot.doc.permission_ceiling
        return resolved == PermissionProfile.WORKSPACE_WRITE

    def _has_git_evidence(self, node: AgentNode) -> bool:
        """True iff this node asked for the read-only git verbs and the operator enabled them."""
        return resolve_git_evidence(node.git_evidence, self._s.allow_git_evidence)

    def _can_run_commands(self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute) -> bool:
        """True iff this attempt actually gets a shell on a provider it may land on.

        The question the detection brackets key on, because command execution — not the permission
        profile, not a declared grant — is what makes a working-tree write or a ``.git`` mutation
        reachable. Provider- and host-specific, so the answer comes from the adapters through the
        Router (:meth:`~wastech_orchestrator.routing.router.AgentRouter.route_grants_shell`): a
        Codex node runs commands on either profile, a Claude ``read-only`` node only with the
        git-evidence grant, and neither on native Windows where the shell has no OS sandbox to sit
        in. Fail-closed — an unclassifiable attempt counts as having one.
        """
        return self._s.router.route_grants_shell(
            route,
            permission_profile=node.permission_profile or ctx.snapshot.doc.permission_ceiling,
            git_evidence=self._has_git_evidence(node),
        )

    def _granted_shell_before(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute
    ) -> _GrantedShellBefore | None:
        """The fingerprints taken before a node that has a shell but is not meant to write runs.

        ``None`` for every other node — that is the signal to skip both comparisons entirely, so no
        node pays for a check that cannot apply to it. This bracket, not the profile-keyed one in
        :meth:`_invoke`, is what watches the node class that can reach the repository without being
        meant to: what makes a working-tree write or ``.git`` drift possible is the shell, so keying
        on the profile alone would leave exactly that class unwatched. It keys on
        :meth:`_can_run_commands` rather than on the git-evidence grant for the same reason — the
        grant is one way to arrive at a shell (Claude), while a Codex ``read-only`` node has one
        without asking and would otherwise be watched by nothing. Both signals are *reported*, never
        acted on, the same as every other node class's drift.

        Snapshotting before rather than reading state once afterwards is what keeps an earlier
        writer's diff — or the orchestrator's own branch prep — from being blamed on this node in a
        flow that mixes the two profiles. One bracket spans every attempt of the node, so a HITL
        re-run is covered without accumulating anything. A workspace-write node is bracketed instead
        inside :meth:`_invoke`, per attempt, because only there is the comparison ahead of the
        orchestrator's own `git`; the verdict is the same for both.
        """
        git = self._s.git
        if git is None or self._is_workspace_write(node, ctx):
            return None
        if not self._can_run_commands(node, ctx, route):
            return None
        return _GrantedShellBefore(
            control=git.capture_git_control_state(), tree=git.changed_code_entries(ctx.task_id)
        )

    def _result(
        self,
        node: AgentNode,
        ctx: NodeContext,
        outcome: StageOutcome,
        run_id: int,
        before: _GrantedShellBefore | None,
        attempt_drift: str | None = None,
    ) -> NodeResult:
        """The node's result, flagged when the node changed what it was not there to change.

        For a read-only node with a shell the provider's sandbox write-denies the whole clone, so a
        change here — in the working tree or in Git control state — means that enforcement did not
        hold. Both are reported rather than acted on: the outcome stays ``done``, the run continues,
        and the operator gets a warning plus a ⚠️ trace from the post-node hook. No node class parks
        on drift, so this is where every class's lands: ``attempt_drift`` carries what
        :meth:`_invoke`'s per-attempt bracket saw on a workspace-write node, which cannot be
        compared out here because the post-edit guard's own `git` has already run by then.

        That trade is deliberate, and it is why the warning carries the drift's aspect-level summary
        rather than just a flag: it is the only thing standing between a poisoned hook and the
        orchestrator's next git command.

        A read-only node's working-tree change is additionally never *consumed* — no diff is
        published and nothing downstream is handed it, since the post-edit guard stays off for such
        a node. Control state is compared first, so the ``git status`` behind the tree comparison
        cannot land between the attempt and the fingerprint that judges it.
        """
        git = self._s.git
        drift = attempt_drift
        wrote = False
        if before is not None and git is not None:
            control_drift = git.compare_git_control_state(before.control)
            drift = _merge_drift(drift, control_drift.summary() if control_drift else None)
            wrote = git.changed_code_entries(ctx.task_id) != before.tree
        return NodeResult(
            node_id=node.id,
            outcome=replace(
                _agent_outcome(outcome), unexpected_write=wrote, git_control_drift=drift
            ),
            node_run_id=run_id,
        )

    def _invoke(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        *,
        human_input_path: str | None,
        resume_session_id: str | None = None,
        resume_usage_baseline: NormalizedUsage | None = None,
    ) -> tuple[int, StageOutcome, str | None]:
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
        session_id, baseline, baseline_session_id = self._resolve_resume(
            node, ctx, route, resume_session_id, resume_usage_baseline
        )
        request = self._build_request(
            node, ctx, route, run_id, human_input_path, session_id, guard_output_baseline(baseline)
        )
        assert_request_contained(request, self._s.exchange_root)
        # Fingerprint the Git control state before a workspace-write attempt. The compare after
        # `run_stage` (below) has to happen here, before any orchestrator git touches the clone:
        # `_apply_post_edit_guard`'s own `git add --intent-to-add` / `git reset` would otherwise
        # read back as index drift of our own making. An attempt that is not meant to write but has
        # a shell can reach `.git` all the same — it is fingerprinted too, but from the outer
        # reporting bracket (`_granted_shell_before`), which runs where no such collision exists.
        #
        # The Write/Edit-deny roots (exchange/gitdir/common/hooks/tasks) are resolved fresh here —
        # they are only final after branch prep — and threaded onto the request for every attempt
        # that has *any* way to mutate the clone: write tools or a shell. Keyed on write access
        # alone they went missing from exactly the two classes that need them most: a shell-bearing
        # read-only attempt (whose deny roots the pre-launch canary probes) and, on native Windows,
        # a workspace-write attempt whose shell was dropped but whose Write/Edit tools remain. Each
        # adapter still decides what to do with them — a profile that grants no write at all needs
        # no carve-out from it.
        git = self._s.git
        control_before = None
        if git is not None:
            writes = self._is_workspace_write(node, ctx)
            if writes:
                control_before = git.capture_git_control_state()
            if writes or self._can_run_commands(node, ctx, route):
                request = replace(
                    request,
                    write_guard=git.resolve_control_paths(self._s.exchange_root),
                )
        # Detection-in-depth: fingerprint the curated exchange before the attempt so a
        # provider mutation of the read-only surface is caught from parent-held state (below),
        # before
        # any downstream node consumes it. Applies to every agent profile — the exchange is
        # immutable
        # to the agent regardless of write access.
        exchange_before = capture_exchange_manifest(self._s.exchange_root, ctx.task_id)
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
            usage_baseline=baseline,
            baseline_session_id=baseline_session_id,
        )
        if outcome.result is None:
            error_class = outcome.terminal_error.error_class if outcome.terminal_error else None
            err = error_class.value if error_class else "no_provider_available"
            raise NodeInfraError(
                f"agent node {node.id!r}: no provider could complete it ({err})",
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
        # The result is trusted (the quiescence barrier proved the provider tree empty inside the
        # adapter), so compare now — before `_apply_post_edit_guard`'s `git diff` touches the clone
        # and before any downstream node reads the exchange. Git control-state drift is REPORTED,
        # never parked: a moved `HEAD`, a moved task ref, a changed index or a branch that moved on
        # `origin` is ordinary working state in the operator's own repository, of exactly the kind
        # publishing reuses rather than treats as evidence of foreign ownership — and parking on it
        # would discard a finished node's work after the fact. The three aspects that are NOT
        # ordinary work (`hooks`, `config`, `executables`) are loud warnings on the same path, and
        # what that costs is stated in the shipped guide's floor rather than softened here. What
        # still holds the change is the dangerous-diff gate, which measures from the last commit the
        # orchestrator itself made — so whatever the drift committed is on the far side of that
        # reference and a human is still asked about it.
        # An exchange mutation is a different question and still parks: it is the agent editing its
        # own assignment, which no ordinary operator action looks like.
        drift: str | None = None
        if control_before is not None and git is not None:
            control_drift = git.compare_git_control_state(control_before)
            drift = control_drift.summary() if control_drift is not None else None
        assert_exchange_unchanged(
            exchange_before, self._s.exchange_root, ctx.task_id, node_id=node.id
        )
        self._persist_session(node, ctx, outcome)
        return run_id, outcome, drift

    def _apply_post_edit_guard(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute
    ) -> str | None:
        """After a workspace-write edit, write the diff (``{diff_path}``) and gate dangerous diffs.

        Core-owned and automatic — the flow never declares or disables it. A
        deletion/dependency diff requires a durable human approval (or a matching planning
        pre-approval); on denial the stage reconsiders once with the denial context and, if the diff
        is still dangerous, fails closed to manual review.

        Returns the Git control-state drift of the reconsider attempt, if it ran and if it drifted
        — that attempt is bracketed like any other, and its comparison would otherwise be computed
        and dropped on the floor. ``None`` on every other path.
        """
        if not self._is_workspace_write(node, ctx) or self._s.git is None:
            return None
        private_diff = self._s.git.write_current_diff(ctx.task_id)
        # Keep the private authoritative diff as the audit artifact; expose only the redacted
        # exchange copy as {diff_path} to the provider.
        self._in.diff_path = publish_file(
            self._s.exchange_root,
            ctx.task_id,
            "current.diff",
            private_diff,
            extra_secrets=self._s.prompt_secrets,
        )
        if self._s.register_artifact is not None:
            self._s.register_artifact(ctx.task_id, "diff", private_diff)
        self._apply_output_containment_guard(node, ctx)
        entries = self._s.git.changed_code_entries(ctx.task_id)
        dangerous = evaluate_diff_gate(entries, self._s.trust_level, self._s.protected_paths)
        if dangerous is None:
            return None
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
        elif already_approved_in_task(self._s.artifacts_root, ctx.task_id, dangerous):
            # this exact dangerous diff was already approved earlier in the task
            return None
        else:
            result = self._gate().request(
                task_id=ctx.task_id,
                node_id=node.id,
                subtask=ctx.subtask_order,
                signal=dangerous_diff_signal(node.id, dangerous),
                path=path,
            )
            self._require_human(node, "approval", result)
            approved = result.approved is True
        if approved:
            mark_consumed(path)
            return None
        return self._reconsider(node, ctx, route, path)

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
            for entry in self._s.git.changed_code_entries(ctx.task_id)
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
        if not guardrail_request_matches(persisted, dangerous):
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
    ) -> str | None:
        """Approval denied: re-run the node with the denial context, then re-classify.

        Returns this attempt's Git control-state drift for the node's outcome to carry, so a clone
        that moved during the reconsider pass is reported like one that moved during any other.
        """
        mark_interaction_status(path, "reconsidering")
        _run_id, _outcome, drift = self._invoke(
            node, ctx, route, human_input_path=self._exchange_human_input(node, ctx, path)
        )
        assert self._s.git is not None
        private_diff = self._s.git.write_current_diff(ctx.task_id)
        self._in.diff_path = publish_file(
            self._s.exchange_root,
            ctx.task_id,
            "current.diff",
            private_diff,
            extra_secrets=self._s.prompt_secrets,
        )
        # Re-evaluate under the same policy the request used, so the reconsider pass agrees on which
        # changes gate (level + protected floor) and does not spuriously flag a now-allowed change.
        still_dangerous = evaluate_diff_gate(
            self._s.git.changed_code_entries(ctx.task_id),
            self._s.trust_level,
            self._s.protected_paths,
        )
        if still_dangerous is not None:
            raise NodeManualRequired(
                f"agent node {node.id!r}: retained dangerous changes after approval was denied"
            )
        mark_interaction_status(path, "reconsidered")
        return drift

    def _build_request(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        run_id: int,
        human_input_path: str | None,
        session_id: str | None = None,
        resume_baseline_output_tokens: int | None = None,
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
            output_schema=output_schema,
            model=node.model,
            reasoning=node.reasoning,
            extra_args=list(node.extra_args),
            session_id=session_id,
            resume_baseline_output_tokens=resume_baseline_output_tokens,
            # Network is a per-node override on top of the flow-wide default: the node's
            # ``network_access`` wins (a node-level grant works even in a flow with no
            # ``network_policy``; a node-level ``False`` opts out), and absent it the node inherits
            # the flow's ``network_policy`` default. It only toggles network — never the
            # filesystem permission ceiling.
            network_access=resolve_network_access(
                node.network_access, ctx.snapshot.doc.network_policy
            ),
            # The read-only git verbs, when this node asked for them AND the operator enabled the
            # grant. Like network, it toggles one capability dimension and never the filesystem
            # ceiling — the node stays read-only, enforced by the provider's sandbox.
            git_evidence=self._has_git_evidence(node),
            # Defense-in-depth: the Core-owned advisory security contract, threaded via
            # NodeServices; the neutral seam prepends it to the effective prompt.
            security_preamble=self._s.security_preamble,
        )

    def _prompt_variables(self, ctx: NodeContext, node: AgentNode) -> dict[str, object | None]:
        # The allowlisted artifact paths come from the shared collector so the agent
        # prompt and the tool-node stdin never drift; the rest (ids, memory) is prompt-only.
        paths = build_path_context(self._in, self._s.repo_dir)
        variables: dict[str, object | None] = {
            "task_id": ctx.task_id,
            "stage": node.id,
            "repo_path": paths["repo"],
            **paths,
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
        """The generic ``{<node_id>_path}`` variables for this flow (shared with the evaluator)."""
        return build_node_output_paths(
            ctx.snapshot.doc.nodes,
            ctx.task_id,
            exchange_root=self._s.exchange_root,
            artifacts_root=self._s.artifacts_root,
        )

    def _memory_path(self, node: AgentNode, ctx: NodeContext) -> str | None:
        """Build this node's memory packet and return its path — node-driven.

        Returns the per-node packet path only when memory is enabled AND the node's (operator-
        editable) role prompt references ``{memory_path}``; otherwise ``None`` (so the variable
        renders empty and the conditional block drops). A node not referencing it never triggers a
        build, so a custom operator node opts in with no Core change. Best-effort: a memory read
        must never break a node run, so any failure degrades to no packet."""
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
        # The memory store + the private packet stay private; only the redacted per-node packet
        # crosses into the exchange as {memory_path}.
        return publish_file(
            self._s.exchange_root,
            ctx.task_id,
            f"memory/{node.id}.md",
            str(written),
            extra_secrets=self._s.prompt_secrets,
        )

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

    def _resolve_resume(
        self,
        node: AgentNode,
        ctx: NodeContext,
        route: ResolvedRoute,
        explicit_session_id: str | None,
        explicit_baseline: NormalizedUsage | None,
    ) -> tuple[str | None, NormalizedUsage | None, str | None]:
        """Resolve ``(session_id, usage_baseline, baseline_session_id)`` for one invocation.

        An explicit resume id (the in-process HITL round-trip) wins and carries the first
        invocation's cumulative usage as the baseline; otherwise the durable editing lineage gives
        both the session and its persisted usage snapshot. ``baseline_session_id`` is always the
        session the baseline was captured on, so the delta is subtracted only when the run actually
        continued that same session (a session the router silently dropped reads as fresh)."""
        if explicit_session_id is not None:
            return explicit_session_id, explicit_baseline, explicit_session_id
        row = self._resume_lineage(node, ctx, route)
        if row is None:
            return None, None, None
        return row.raw_session_id, deserialize_usage(row.usage_snapshot), row.raw_session_id

    def _resume_lineage(
        self, node: AgentNode, ctx: NodeContext, route: ResolvedRoute
    ) -> EditingLineageRow | None:
        """The durable editing lineage this node resumes, or ``None`` for a fresh session.

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
        return row

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
                usage_snapshot=snapshot_for_lineage(result.normalized_usage),
            )
        )


def _lineage_key(node: AgentNode) -> str:
    """The editing-lineage key for a node: its ``lineage_affinity`` target, else its own id.

    An affinity-less ``editing_lineage`` node owns a lineage named after itself; a node with
    ``lineage_affinity: X`` joins lineage ``X``. Resume and persist MUST compute the same key, so
    they share this one helper. The validator forbids affinity chains (rule 7), so the target is
    always a lineage owner and one hop is enough — no transitive resolution here."""
    return node.lineage_affinity or node.id


def _merge_drift(*parts: str | None) -> str | None:
    """Join the drift summaries of a node's attempts into the one its outcome carries.

    A node can attempt more than once — a HITL round-trip, a max-turns continue, a reconsider pass
    after a denied approval — and each attempt is bracketed on its own. Reporting only the last one
    would drop the earlier clone move, which is precisely the aspect an operator needs; a second
    carrier on ``NodeOutcome`` would buy nothing over one joined line. ``None`` when no attempt
    drifted, which is the ordinary case.
    """
    found = [part for part in parts if part]
    return "; ".join(found) if found else None


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
    """True when the outcome is a Claude run that exhausted its ``max_turns`` cap.

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


def _persisted_kind(persisted: Mapping[str, Any]) -> AskKind | None:
    request = persisted.get("request")
    if isinstance(request, Mapping):
        kind = request.get("kind")
        if kind == "question":
            return "question"
        if kind == "approval":
            return "approval"
    return None
