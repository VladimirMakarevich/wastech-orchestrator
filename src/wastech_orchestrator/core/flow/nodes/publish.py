"""Publish node runner — thin adapter to the GitManager.

Publishing is the orchestrator's sole responsibility (the hard invariant: providers and flows never
touch git). The runner maps the flow's ``PublishingPolicy`` to the idempotent git operations and
returns an unconditional ``done`` outcome on success. A git failure *after* finalize has already
moved the task file into ``tasks/done/`` raises ``NodeManualRequired`` (a resumable manual stop)
rather than a terminal failure — see :meth:`PublishNodeRunner.run` for why.

``pull_request`` / ``documentation_pull_request`` take the PR path: commit code, commit the audit
trail, push the branch, open the PR — each idempotent via ``publish_operations``. The documentation
PR needs no special staging: the after-stage output guard already confined the writes to the
report directory, so the existing scoped staging commits only those docs.
``private_control_workspace_report`` stores the report under the gitignored ``.worc/``
control workspace and touches git not at all — it fails closed if that report would be git-trackable
(so it can never enter staging/a commit/a PR), leaving the target repo byte-for-byte. ``none`` /
``local_artifact`` write no git either (the deliverable is the in-workspace artifact). Task finalize
(moving the task file before the audit commit) and auto-merge stay orchestrator-level.
"""

from __future__ import annotations

import logging

from wastech_orchestrator.checks.selection import select_check_sets
from wastech_orchestrator.config.schema import PublishScope
from wastech_orchestrator.core.dangerous_diff import evaluate_diff_gate
from wastech_orchestrator.core.flow.contracts import PublishingPolicy
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.diff_gate import (
    consume_prior_approval,
    dangerous_diff_signal,
)
from wastech_orchestrator.core.flow.nodes.human_gate import HumanGate
from wastech_orchestrator.core.flow.output_policy import resolve_output_policy, within_subdir
from wastech_orchestrator.core.flow.schema import FlowNode, PublishNode
from wastech_orchestrator.core.hitl import guardrail_interaction_path, mark_consumed
from wastech_orchestrator.git_manager import GitCommandError
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.state_store import CheckRunRow, NodeRunRow

_LOG = logging.getLogger(__name__)

_PR_POLICIES = frozenset(
    {PublishingPolicy.PULL_REQUEST, PublishingPolicy.DOCUMENTATION_PULL_REQUEST}
)

#: How many adopted commits a PR notice lists before it stops enumerating.
_NOTICE_COMMIT_CAP = 10


def _local_adoption_notice(count: int) -> str | None:
    """A PR-body line for commits this run adopted locally, or ``None`` when there were none.

    The mirror of :func:`_adoption_notice` for the other half of the same event: there, someone
    pushed to the task branch; here, something inside the run committed the work itself — an agent's
    own ``git commit`` on a node whose class only warns, or the operator's. Its content did pass the
    dangerous-diff gate (which measures from the reference point, not from ``HEAD``), so this is not
    a block; what a reviewer needs to know is that the message and authorship of those commits are
    not the orchestrator's.
    """
    if count <= 0:
        return None
    return (
        f"> **Adopted {count} commit(s) this orchestrator did not make, from inside the run:** the "
        "work was already committed when publishing reached it, so its message and authorship are "
        "the committer's. The change itself passed the dangerous-diff gate."
    )


def _adoption_notice(branch: str | None, adopted: tuple[str, ...]) -> str | None:
    """A PR-body line naming the commits publishing merged in, or ``None`` when there were none.

    The reported diff is measured from the base branch, so once foreign commits are in it the diff
    no longer describes this task alone. A reviewer has to be told that in the PR itself.

    Deliberately says "not recorded as pushed by this run" rather than "this orchestrator did not
    make": the fourth case is also reached when the remote probe could not answer during an earlier
    attempt, and the commits then adopted are this orchestrator's own from that attempt. Claiming
    authorship it cannot verify would make the sentence false exactly where it is most confusing.
    """
    if not adopted:
        return None
    shown = ", ".join(f"`{sha}`" for sha in adopted[:_NOTICE_COMMIT_CAP])
    more = (
        ""
        if len(adopted) <= _NOTICE_COMMIT_CAP
        else f" (+{len(adopted) - _NOTICE_COMMIT_CAP} more)"
    )
    return (
        f"> **Adopted {len(adopted)} commit(s) from `origin/{branch}` that this run did not record "
        f"pushing:** {shown}{more}. The diff below is measured from the base branch, so it covers "
        "those commits as well as this task's work. They are usually someone else's; a run whose "
        "remote probe could not answer earlier can also adopt its own."
    )


class PublishConfigError(Exception):
    """The publish node needs git/branch inputs the unit did not provide."""


class PublishNodeRunner:
    """Run a ``publish`` node through the GitManager (constructed per unit)."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        # Commits this publish had to merge in because the branch had moved on without us. Held on
        # the runner so the node's outcome can carry the fact to the operator surfaces: with
        # ``publish: push``/``commit`` there is no PR body to put it in.
        self._adopted: tuple[str, ...] = ()
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, PublishNode)
        run_id = self._s.store.record_node_run(
            NodeRunRow(
                task_id=ctx.task_id,
                node_id=node.id,
                node_kind="publish",
                subtask_order=ctx.subtask_order,
                status="running",
                started_at=self._s.clock(),
            )
        )
        try:
            result_ref = self._publish(node, ctx)
        except GitCommandError as exc:
            # finalize() has already moved the task file into tasks/done/ and committed the audit
            # trail, so the deliverable is committed but publishing did not finish. Surface a
            # resumable manual stop instead of a terminal failure: a terminal `failed` here would
            # both mislabel a done-committed task AND strand its file in tasks/done/ (the failure
            # path cannot relocate a file it can no longer find at the pre-finalize path). The git
            # operations are idempotent via `publish_operations`, so `rerun --continue` re-enters
            # this node and completes the push/PR without duplicating the commits.
            #
            # The git stderr is the only diagnosis of *why* publish failed. Without surfacing
            # it the operator sees only `error_class=publish_failed` and must reproduce it by hand,
            # so log it (ERROR, daemon log) and persist it as a node artifact. Scrub secrets: git
            # stderr (paths/pathspec) is normally safe, but a remote URL / push error can echo a
            # token, so it goes through the same redactor as stored prompts before it is written.
            detail = redact_text(str(exc), extra_secrets=self._s.prompt_secrets)
            bind(_LOG, task_id=ctx.task_id, node_id=node.id).error(
                "publish git operation failed", extra={"error": detail, "policy": node.policy.value}
            )
            self._record_publish_error(ctx, node, detail)
            self._s.store.complete_node_run(
                run_id,
                status="failed",
                outcome=None,
                error_class="publish_failed",
                finished_at=self._s.clock(),
            )
            raise NodeManualRequired(
                f"publish node {node.id!r} ({node.policy.value}) could not complete the git "
                f"publish (resumable via rerun --continue): {detail}"
            ) from exc
        self._s.store.complete_node_run(
            run_id,
            status="published",
            outcome="done",
            finished_at=self._s.clock(),
            # `commit_sha_after` is the node's result reference; for a publish node that is the PR
            # URL (a PR policy) or None (no-git policies) — not a commit SHA. See NodeRunRow.
            commit_sha_after=result_ref,
        )
        return NodeResult(
            node_id=node.id,
            outcome=NodeOutcome("done", adopted_commits=self._adopted),
            node_run_id=run_id,
        )

    def _record_publish_error(self, ctx: NodeContext, node: PublishNode, detail: str) -> None:
        """Persist the (already-redacted) git failure as a ``publish_error`` node artifact so the
        cause survives the run and is diagnosable off-line. Best-effort: a write/register
        failure must never mask the manual stop, so it is swallowed."""
        if self._s.register_artifact is None:
            return
        try:
            dest = task_artifact_dir(self._s.artifacts_root, ctx.task_id) / "publish-error.txt"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                f"publish node {node.id!r} ({node.policy.value}) git failure:\n{detail}\n",
                encoding="utf-8",
            )
            self._s.register_artifact(ctx.task_id, "publish_error", str(dest))
        except OSError:
            pass

    def _publish(self, node: PublishNode, ctx: NodeContext) -> str | None:
        if node.policy is PublishingPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT:
            return self._store_private_report(ctx)
        if node.policy not in _PR_POLICIES:
            # none / local_artifact: the deliverable is the in-workspace artifact; git is untouched.
            return None
        git = self._s.git
        if git is None or self._in.branch is None:
            raise PublishConfigError(
                f"publish node {node.id!r} ({node.policy.value}) requires a GitPort + branch"
            )
        # Finalize (move the task file + write the committed summary) BEFORE the audit commit so
        # both land in it (legacy ordering); the committed summary.md is the PR body, falling back
        # to the logs/ summary slot.
        committed_summary = self._s.finalize() if self._s.finalize is not None else None
        # Per-task downgrade-only cap: stop after the commits (`commit`) or before
        # the PR (`push`). This branch IS ``min(flow_policy, task.publish)`` — a PR flow's implicit
        # scope is ``pull_request``, so capping here caps it. Full sequence when unset (``None``).
        scope = self._in.publish_scope
        # The PR body is required only when a PR will actually be opened (unset or the full
        # `pull_request` scope). A `commit`/`push` cap needs no body — and, as before, the guard
        # fires *before* any commit so a missing body refuses cleanly with no side effects.
        will_open_pr = scope is None or scope is PublishScope.PULL_REQUEST
        body_path = committed_summary or self._in.summary_body_path
        if will_open_pr and body_path is None:
            raise PublishConfigError(
                f"publish node {node.id!r} ({node.policy.value}) has no PR body: wire a finalize "
                "hook or set summary_body_path (refusing to open a PR with an empty body)"
            )
        # The last chance to ask a human about a dangerous change, and for some flows the only
        # one: the gate measures from the last commit the orchestrator made, and any node with a
        # shell can commit — a `tool`, an `evaluator`, a read-only agent attempt — while those
        # classes only warn by policy. A flow that ends with one of them (or has no writing node at
        # all) used to reach this commit with content nobody had been asked about.
        self._require_dangerous_diff_approval(node, ctx)
        message = self._in.commit_message or f"feat({ctx.task_id}): publish"
        git.commit_code(ctx.task_id, message)
        git.commit_audit(ctx.task_id, task_packet_digest=self._s.task_packet_digest)
        if scope is PublishScope.COMMIT:
            return None
        # The branch may carry commits nobody here made. Merge them in FIRST — locally, where the
        # result can still be thrown away — because what the quality gate passed was "our commits on
        # top of base" and what would go out is a combination it never saw. Pushing first and
        # re-running the gate afterwards published the untested combination and only then asked
        # about it, which is the reverse of the invariant.
        adopted = git.adopt_foreign_commits(ctx.task_id, self._in.branch, mode=self._in.branch_mode)
        if adopted:
            self._recheck_adopted(node, ctx, adopted)
        git.push(ctx.task_id, self._in.branch, mode=self._in.branch_mode)
        # The reported diff is measured from the base, so once foreign commits are in it the diff no
        # longer describes this task alone: carry that out on the outcome, where the operator's
        # console warning and ⚠️ trace pick it up. The PR body says the same to a reviewer (below) —
        # but a ``push``/``commit`` scope opens no PR, and there the trace is the only carrier.
        self._adopted = adopted
        if scope is PublishScope.PUSH:
            return None
        # Both adoptions a reviewer has to know about: commits the remote had that we merged in,
        # and commits made inside the run that publishing found already committed.
        notices = [
            _adoption_notice(self._in.branch, adopted),
            _local_adoption_notice(git.adopted_commit_count(ctx.task_id)),
        ]
        notice = "\n\n".join(line for line in notices if line) or None
        return git.create_pr(
            ctx.task_id,
            self._in.branch,
            title=self._in.pull_request_title or ctx.task_id,
            body_path=body_path or "",
            notice=notice,
        )

    def _require_dangerous_diff_approval(self, node: PublishNode, ctx: NodeContext) -> None:
        """Ask the operator about a dangerous diff still unapproved at publish time; else stop.

        Measured from the same reference point the writing node's gate used — the last commit the
        orchestrator made for this task — so the only thing that can appear here is what arrived
        after it: a self-commit by a node that merely warns, or the whole change in a flow with no
        writing node. Anything the operator already approved in this task (a planning pre-approval,
        the writing node's own approval of the identical set) is not asked again.

        A denial is a manual stop rather than a reconsideration: the agent is gone by the time
        publishing runs, so there is nobody to hand the denial to. Nothing has been committed at
        this point, which is exactly why the call sits here and not after ``commit_code``.
        """
        git = self._s.git
        if git is None:
            return
        dangerous = evaluate_diff_gate(
            git.changed_code_entries(ctx.task_id), self._s.trust_level, self._s.protected_paths
        )
        if dangerous is None:
            return
        path = guardrail_interaction_path(
            self._s.artifacts_root,
            ctx.task_id,
            node.id,
            subtask=ctx.subtask_order,
            cycle=ctx.run_state.fix_iterations,
        )
        if consume_prior_approval(self._s.artifacts_root, ctx.task_id, dangerous, path):
            return
        if self._s.notifier is None:
            raise NodeManualRequired(
                f"publish node {node.id!r}: the change carries {dangerous.risk} requiring approval "
                f"({', '.join(dangerous.paths[:5])}) and no notifier transport is configured to ask"
            )
        gate = HumanGate(
            self._s.notifier,
            timeout_s=self._s.ask_timeout_s,
            contacts=self._in.contacts,
            heartbeat_seconds=self._s.ask_heartbeat_seconds,
        )
        result = gate.request(
            task_id=ctx.task_id,
            node_id=node.id,
            subtask=ctx.subtask_order,
            signal=dangerous_diff_signal(node.id, dangerous),
            path=path,
        )
        if result.failure is not None or not result.answered or result.approved is not True:
            raise NodeManualRequired(
                f"publish node {node.id!r}: publication of a change carrying {dangerous.risk} was "
                f"not approved ({result.failure or 'denied'}); nothing was committed or pushed"
            )
        mark_consumed(path)

    def _recheck_adopted(
        self, node: PublishNode, ctx: NodeContext, adopted: tuple[str, ...]
    ) -> None:
        """Re-run the quality gate over the adopted combination; park when it does not pass.

        A failure here is not this task's work going bad — it is an untested combination that a
        human has to look at — so it parks with the adopted state on disk and never falls back or
        enters the fixing loop.
        """
        git = self._s.git
        changed = git.changed_code_paths_since_base() if git is not None else None
        selected = select_check_sets(self._in.check_sets, changed)
        log = bind(_LOG, task_id=ctx.task_id, node=node.id)
        if not selected:
            log.info("no check set covers the adopted commits; nothing to re-run")
            return
        outcome = self._s.check_runner.run(
            clone_dir=self._s.repo_dir,
            artifacts_root=self._s.artifacts_root,
            task_id=ctx.task_id,
            subtask=ctx.subtask_order,
            selected=selected,
            clock=self._s.clock,
        )
        for run in outcome.runs:
            self._s.store.record_check_run(
                CheckRunRow(
                    task_id=ctx.task_id,
                    subtask_order=ctx.subtask_order,
                    command=run.command,
                    exit_code=run.exit_code,
                    timed_out=run.timed_out,
                    passed=run.passed,
                    log_path=run.log_path,
                    skipped=run.skipped,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
            )
        if outcome.passed and not (outcome.any_launch_failed or outcome.nothing_ran):
            log.info("checks re-run over the adopted commits and passed")
            return
        raise NodeManualRequired(
            f"publish node {node.id!r}: this branch carried {len(adopted)} commit(s) this run did "
            f"not record pushing ({', '.join(adopted[:5])}), and the checks over the combined tree "
            "did not pass — the combination is untested and nothing has been pushed"
        )

    def _store_private_report(self, ctx: NodeContext) -> str | None:
        """Finalize a ``private_control_workspace_report`` deliverable without touching git.

        The report is already written under the gitignored ``.worc/`` control workspace by the
        writing node (the after-stage guard confined it there). This node touches git **not at all**
        — the target repo stays byte-for-byte. It fails closed if any file under the report dir is
        git-trackable: a report that could enter staging / a commit / a PR is a leak, so we refuse
        rather than risk publishing it. The report files are registered as audit artifacts.
        """
        resolved = resolve_output_policy(ctx.snapshot.doc.output_policy, ctx.task_id)
        if resolved.report_subdir is None:  # defensive: a non-report output_policy on this node
            return None
        if self._s.git is not None:
            leaking = [
                entry.path
                for entry in self._s.git.changed_code_entries(ctx.task_id)
                if within_subdir(entry.path, resolved.report_subdir)
            ]
            if leaking:
                raise NodeManualRequired(
                    f"private report under {resolved.report_subdir!r} is git-trackable "
                    f"(not ignored): {sorted(leaking)} would enter staging/commit/PR — refusing to "
                    "publish (ensure the .worc/ control workspace is gitignored)"
                )
        report_dir = resolved.report_dir(self._s.repo_dir)
        if report_dir is not None and self._s.register_artifact is not None:
            for filename in resolved.required_files:
                path = report_dir / filename
                if path.exists():
                    self._s.register_artifact(ctx.task_id, "report", str(path))
        return None
