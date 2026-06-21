"""Publish node runner (P1.3/P1.4) — thin adapter to the GitManager.

Publishing is the orchestrator's sole responsibility (the hard invariant: providers and flows never
touch git). The runner maps the flow's ``PublishingPolicy`` to the idempotent git operations and
returns an unconditional ``done`` outcome on success. A git failure *after* finalize has already
moved the task file into ``tasks/done/`` raises ``NodeManualRequired`` (a resumable manual stop)
rather than a terminal failure — see :meth:`PublishNodeRunner.run` for why.

``pull_request`` / ``documentation_pull_request`` take the PR path: commit code, commit the audit
trail, push the branch, open the PR — each idempotent via ``publish_operations``. The documentation
PR needs no special staging: the after-stage output guard (P3.2) already confined the writes to the
report directory, so the existing scoped staging commits only those docs.
``private_control_workspace_report`` (P3.2) stores the report under the gitignored ``.worc/``
control workspace and touches git not at all — it fails closed if that report would be git-trackable
(so it can never enter staging/a commit/a PR), leaving the target repo byte-for-byte. ``none`` /
``local_artifact`` write no git either (the deliverable is the in-workspace artifact). Task finalize
(moving the task file before the audit commit) and auto-merge stay orchestrator-level.
"""

from __future__ import annotations

from wastech_orchestrator.core.flow.contracts import PublishingPolicy
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.output_policy import resolve_output_policy, within_subdir
from wastech_orchestrator.core.flow.schema import FlowNode, PublishNode
from wastech_orchestrator.git_manager import GitCommandError
from wastech_orchestrator.state_store import NodeRunRow

_PR_POLICIES = frozenset(
    {PublishingPolicy.PULL_REQUEST, PublishingPolicy.DOCUMENTATION_PULL_REQUEST}
)


class PublishConfigError(Exception):
    """The publish node needs git/branch inputs the unit did not provide."""


class PublishNodeRunner:
    """Run a ``publish`` node through the GitManager (constructed per unit)."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
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
            self._s.store.complete_node_run(
                run_id,
                status="failed",
                outcome=None,
                error_class="publish_failed",
                finished_at=self._s.clock(),
            )
            raise NodeManualRequired(
                f"publish node {node.id!r} ({node.policy.value}) could not complete the git "
                f"publish (resumable via rerun --continue): {exc}"
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
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=run_id)

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
        body_path = committed_summary or self._in.summary_body_path
        if body_path is None:
            raise PublishConfigError(
                f"publish node {node.id!r} ({node.policy.value}) has no PR body: wire a finalize "
                "hook or set summary_body_path (refusing to open a PR with an empty body)"
            )
        message = self._in.commit_message or f"feat({ctx.task_id}): publish"
        git.commit_code(ctx.task_id, message)
        git.commit_audit(ctx.task_id)
        git.push(ctx.task_id, self._in.branch)
        return git.create_pr(
            ctx.task_id,
            self._in.branch,
            title=self._in.pr_title or ctx.task_id,
            body_path=body_path,
        )

    def _store_private_report(self, ctx: NodeContext) -> str | None:
        """Finalize a ``private_control_workspace_report`` deliverable without touching git (P3.2).

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
                for entry in self._s.git.changed_code_entries()
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
