"""Wiring — build :class:`NodeServices` / :class:`NodeInputs` from the orchestrator's live state.

The builder seam the cutover (P1.4) uses: ``run_task``/``resume`` resolve a validated
``FlowSnapshot``, then call these functions to turn the orchestrator's collaborators (router /
checks / git / notifier / store) and the per-run ``_Pipeline`` into the data bundles the node
runners read. Keeping it here (not in ``orchestrator.py``) keeps the node layer free of any
orchestrator import and makes the mapping unit-testable with fakes.

Routing is node-based (a node's ``provider`` field, else the global primary — PRE.1); each node
runner uses the node's own id as its identity (the request ``node_id`` and the audit / interaction
paths) and derives its typed-output contract from the node's declared fields. There is no
``Stage`` map: nothing here translates a node into a pipeline stage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from wastech_orchestrator.checks.model import ResolvedCheck
from wastech_orchestrator.core.flow.nodes.base import (
    CheckRunnerPort,
    GitPort,
    NodeInputs,
    NodeRunStorePort,
    NodeServices,
    NotifierPort,
    RegisterArtifact,
    RouterPort,
    RunProcess,
)
from wastech_orchestrator.providers.process import run_process as _default_run_process
from wastech_orchestrator.routing.snapshots import SnapshotHook

if TYPE_CHECKING:  # avoid a circular import — orchestrator imports this module at cutover.
    from wastech_orchestrator.core.orchestrator import _Pipeline


def build_node_services(
    *,
    router: RouterPort,
    check_runner: CheckRunnerPort,
    store: NodeRunStorePort,
    repo_dir: str,
    artifacts_root: str,
    clock: Callable[[], str],
    git: GitPort | None = None,
    notifier: NotifierPort | None = None,
    snapshot_hook: SnapshotHook | None = None,
    default_timeout_seconds: int = 7200,
    ask_timeout_s: int = 0,
    prompt_audit: bool = False,
    prompt_secrets: tuple[str, ...] = (),
    register_artifact: RegisterArtifact | None = None,
    finalize: Callable[[], str | None] | None = None,
    check_reresolve: Callable[[], tuple[ResolvedCheck, ...] | None] | None = None,
    run_process: RunProcess = _default_run_process,
    process_env: Mapping[str, str] | None = None,
    scan_timeout_s: int = 600,
) -> NodeServices:
    """Assemble the unit-shared :class:`NodeServices` (collaborators + the routing map).

    ``snapshot_hook`` is the git snapshot hook handed to the router for provider observability
    (the legacy ``run_stage(..., snapshot=git)``); the same git manager satisfies both ``git`` and
    ``SnapshotHook``, so callers usually pass it for both. The observability / finalize / re-resolve
    hooks are orchestrator-provided (see :class:`NodeServices`); they default off for unit tests.
    """
    return NodeServices(
        router=router,
        check_runner=check_runner,
        store=store,
        repo_dir=repo_dir,
        artifacts_root=artifacts_root,
        clock=clock,
        default_timeout_seconds=default_timeout_seconds,
        snapshot=snapshot_hook,
        git=git,
        notifier=notifier,
        ask_timeout_s=ask_timeout_s,
        prompt_audit=prompt_audit,
        prompt_secrets=prompt_secrets,
        register_artifact=register_artifact,
        finalize=finalize,
        check_reresolve=check_reresolve,
        run_process=run_process,
        process_env=dict(process_env or {}),
        scan_timeout_s=scan_timeout_s,
    )


def build_node_inputs(
    p: _Pipeline,
    *,
    flow_dir: Path,
    resolved_checks: tuple[ResolvedCheck, ...] | None = (),
    pr_title: str | None = None,
    summary_body_path: str | None = None,
    commit_message: str | None = None,
    subtask_spec_path: str | None = None,
) -> NodeInputs:
    """Build the per-unit :class:`NodeInputs` from the live ``_Pipeline``.

    Artifact paths are read straight off the pipeline (the values the legacy ``_prompt_variables``
    injected). The publish-only fields (``pr_title`` / ``summary_body_path`` / ``commit_message``)
    are not pipeline attributes — the publish wrapper computes and passes them; ``resolved_checks``
    comes from the resolved check profile. Editing-session continuity is durable now (the
    ``editing_lineage`` store, P2.2), not an in-memory map threaded through ``NodeInputs``.
    """
    return NodeInputs(
        flow_dir=flow_dir,
        task_path=p.task_file,
        plan_path=p.plan_path,
        diff_path=p.diff_path,
        checks_path=p.check_log,
        review_path=p.review_findings_path,
        skill_paths=tuple(ref.path for ref in p.selected_skills),
        subtask_count=p.decomposition.n if p.decomposition.accepted else None,
        subtask_spec_path=subtask_spec_path,
        resolved_checks=resolved_checks,
        branch=p.branch or None,
        pr_title=pr_title,
        summary_body_path=summary_body_path,
        commit_message=commit_message,
        contacts=tuple(p.task.contacts),
    )
