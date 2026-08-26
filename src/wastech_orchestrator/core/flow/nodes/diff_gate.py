"""The dangerous-diff approval, shared by the node classes that can reach it.

Two of them do. The **agent** node asks before it hands its edit downstream, and can still send the
node back with the denial (:mod:`~wastech_orchestrator.core.flow.nodes.agent`). The **publish** node
asks immediately before ``commit_code``, and cannot: the agent is gone by then, so a denial is a
manual stop. What they share is everything except that last step — the classification, the signal an
operator reads, the persisted interaction that survives a restart, and the rule that the same
approved change is never asked about twice in one task — so it lives here rather than in either.

Why publishing asks at all, when the writing node already did: the gate measures from the last
commit the orchestrator itself made, and *any* node with a shell can commit — a ``tool``, an
``evaluator``, a read-only agent attempt, and in advanced mode that is every node. A flow whose last
writing node is followed by such a node (or that has no writing node at all, like the packaged
``security_audit``) therefore reached ``commit_code`` with content no human had been asked about.
A commit made inside a run is reported and never parked, so this gate is the one place left to ask
about it — and the only thing that holds such a commit before it goes out.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.dangerous_diff import DangerousDiff
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    iter_task_interactions,
    load_interaction,
    mark_consumed,
)


def dangerous_diff_signal(node_id: str, dangerous: DangerousDiff) -> HumanInputSignal:
    """The approval question an operator reads, naming what makes this diff dangerous."""
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


def guardrail_request_matches(persisted: Mapping[str, Any], dangerous: DangerousDiff) -> bool:
    """Whether a persisted interaction is the approval request for *this* dangerous set.

    Identity is the risk plus the exact path set, so an expanded set never matches an earlier
    answer — the guard only avoids asking twice for the same change, it never weakens.
    """
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


def already_approved_in_task(
    artifacts_root: str | Path, task_id: str, dangerous: DangerousDiff
) -> bool:
    """True if the operator already approved this exact dangerous diff earlier in the task.

    A second writing node (``documentation`` after ``implementation``, or ``fixing`` in a re-test
    loop) re-sees a deletion an upstream node already got cleared, and so does the publish node
    downstream of all of them. Honoring any prior in-task approval of the identical change — a
    planning pre-approval, or an earlier node's guardrail approval — keeps the guard from
    re-prompting; a new or expanded set does not match and still prompts.
    """
    return any(
        persisted.get("approved") is True and guardrail_request_matches(persisted, dangerous)
        for persisted in iter_task_interactions(artifacts_root, task_id)
    )


def consume_prior_approval(
    artifacts_root: str | Path, task_id: str, dangerous: DangerousDiff, path: Path
) -> bool:
    """Whether *path*'s own persisted answer, or a prior in-task one, already approved this diff.

    Returns ``True`` when nothing needs to be asked. A persisted answer at *path* is honored only
    when it is an approval of this same set: anything else (a denial, a failure, a different set)
    returns ``False``, which is what makes the caller ask — or, where it cannot ask, stop.
    """
    persisted = load_interaction(path)
    if persisted is not None:
        if persisted.get("approved") is True and guardrail_request_matches(persisted, dangerous):
            mark_consumed(path)
            return True
        return False
    return already_approved_in_task(artifacts_root, task_id, dangerous)
