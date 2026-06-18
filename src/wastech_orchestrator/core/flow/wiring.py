"""Wiring — build :class:`NodeServices` / :class:`NodeInputs` from the orchestrator's live state.

The builder seam the cutover (P1.4) uses: ``run_task``/``resume`` resolve a validated
``FlowSnapshot``, then call these functions to turn the orchestrator's collaborators (router /
checks / git / notifier / store) and the per-run ``_Pipeline`` into the data bundles the node
runners read. Keeping it here (not in ``orchestrator.py``) keeps the node layer free of any
orchestrator import and makes the mapping unit-testable with fakes.

The ``node_id -> Stage`` map (:func:`build_stage_map`) is **routing data**, not behavior: the
router still selects a provider by ``Stage``, so each routed (agent / evaluator) node needs a
stage. It is the single remaining stage-coupling, removed when routing becomes node-based (P4).
Checks / publish nodes are not routed and never index it (the engine-driver test relies on this).
"""

from __future__ import annotations

from collections.abc import Callable
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
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.providers.base import Stage
from wastech_orchestrator.routing.snapshots import SnapshotHook

if TYPE_CHECKING:  # avoid a circular import — orchestrator imports this module at cutover.
    from wastech_orchestrator.core.orchestrator import _Pipeline

_ROUTED_KINDS = frozenset({"agent", "evaluator"})
_STAGE_VALUES = frozenset(s.value for s in Stage)


def build_stage_map(snapshot: FlowSnapshot) -> dict[str, Stage]:
    """Map each routed node id to its routing ``Stage``.

    Only agent / evaluator nodes are routed, and in the packaged flows their ids are Stage-aligned
    (``implementation`` -> ``IMPLEMENTATION`` …). A routed node whose id is not a ``Stage`` value
    is a P1 limitation (arbitrary operator-flow ids get node-based routing in P4); it is absent
    from the map, so its runner raises ``KeyError`` — a loud, early failure, not a silent one.
    """
    return {
        node.id: Stage(node.id)
        for node in snapshot.nodes_by_id.values()
        if node.kind in _ROUTED_KINDS and node.id in _STAGE_VALUES
    }


def build_node_services(
    *,
    router: RouterPort,
    check_runner: CheckRunnerPort,
    store: NodeRunStorePort,
    repo_dir: str,
    artifacts_root: str,
    snapshot: FlowSnapshot,
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
        stage_for_node=build_stage_map(snapshot),
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
    )


def build_node_inputs(
    p: _Pipeline,
    *,
    flow_dir: Path,
    resolved_checks: tuple[ResolvedCheck, ...] = (),
    pr_title: str | None = None,
    summary_body_path: str | None = None,
    commit_message: str | None = None,
    subtask_spec_path: str | None = None,
) -> NodeInputs:
    """Build the per-unit :class:`NodeInputs` from the live ``_Pipeline``.

    Artifact paths are read straight off the pipeline (the values the legacy ``_prompt_variables``
    injected). The publish-only fields (``pr_title`` / ``summary_body_path`` / ``commit_message``)
    are not pipeline attributes — the publish wrapper computes and passes them; ``resolved_checks``
    comes from the resolved check profile. ``session_ids`` is shared by reference so the agent
    runner's in-memory session continuity (legacy parity; durable lineage is P2.2) survives nodes.
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
        session_ids=p.session_ids,
    )
