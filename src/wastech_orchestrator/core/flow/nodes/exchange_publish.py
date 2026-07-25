"""Route a node's agent-facing artifact into the provider-readable exchange (WRI-001).

A thin wrapper the node runners, per-node post-processing, and the orchestrator share. Each routing
point stays **additive**: the caller keeps its private authoritative write (and its
``register_artifact`` audit call) unchanged and calls one of these to publish a redacted copy into
``<exchange_root>/<task-id>/…`` and re-point the downstream ``NodeInputs``/prompt-variable field at
the returned POSIX path.

When no exchange is wired (``exchange_root == ""`` — a unit harness), publication is skipped and the
private path is returned, so the node keeps today's behavior. ``extra_secrets`` are the per-attempt
redaction literals (the same set the stored-prompt redaction uses).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
from wastech_orchestrator.providers.artifacts import exchange_node_run_dir, exchange_task_dir
from wastech_orchestrator.providers.base import AgentRunRequest
from wastech_orchestrator.providers.exchange import (
    ExchangeError,
    ExchangeManifest,
    assert_orchestration_paths_contained,
    build_exchange_manifest,
    diff_exchange_manifests,
    publish_to_exchange,
)


class ExchangeMutationManual(NodeManualRequired):
    """An agent-side exchange mutation was detected (WRI-002) — routed to manual_action_required.

    Carries the parent-held pre-attempt manifest (``before``) and the post-attempt manifest
    (``after``) so the terminal seam (WRI-007) can quarantine the contaminated tree as evidence with
    both manifests recorded, instead of sealing it as a clean snapshot. A plain
    :class:`NodeManualRequired` (no manifest to record) is used for the pre-run integrity failures.
    """

    def __init__(
        self, message: str, *, before: ExchangeManifest | None, after: ExchangeManifest | None
    ) -> None:
        super().__init__(message)
        self.before = before
        self.after = after


def assert_request_contained(request: AgentRunRequest, exchange_root: str) -> None:
    """Fail closed unless every provider-input path in ``request`` is under the exchange (WRI-001).

    A containment breach is a routing bug about to leak a private/live path to the provider — never
    a fallback infrastructure error, so it routes to non-fallback ``manual_action_required``. A
    no-op when no exchange is wired (a unit harness whose requests still carry private paths).
    """
    if not exchange_root:
        return
    try:
        assert_orchestration_paths_contained(request, exchange_root)
    except ExchangeError as exc:
        raise NodeManualRequired(f"exchange containment violation: {exc}") from exc


def capture_exchange_manifest(exchange_root: str, task_id: str) -> ExchangeManifest | None:
    """Fingerprint the current-task exchange before a provider attempt (WRI-002 detection-in-depth).

    Returns ``None`` when no exchange is wired or the task dir does not exist yet (nothing to
    protect). A pre-existing path-safety violation (a planted symlink/hard-link/ADS surfaced by the
    walk) is itself a non-fallback ``manual_action_required`` condition — the exchange is
    compromised
    before the attempt even runs. Provider-neutral: both the agent and evaluator node runners
    bracket
    ``run_stage`` with this + :func:`assert_exchange_unchanged`, so Codex reuses it unchanged.
    """
    if not exchange_root:
        return None
    task_dir = exchange_task_dir(exchange_root, task_id)
    if not os.path.lexists(task_dir):
        return None
    try:
        return build_exchange_manifest(task_dir, task_id)
    except ExchangeError as exc:
        raise NodeManualRequired(f"exchange integrity (pre-run): {exc}") from exc


def assert_exchange_unchanged(
    before: ExchangeManifest | None, exchange_root: str, task_id: str, *, node_id: str
) -> None:
    """Fail closed unless the exchange is byte-identical to ``before`` (WRI-002).

    Called after a provider attempt (once WRI-012 has proven the provider tree quiescent), before
    any
    downstream node consumes an exchange artifact. A mutation — content edit, add/delete/rename,
    identity swap, or a path-safety violation raised by the re-walk — is a non-fallback security
    policy failure routed to ``manual_action_required``; the changed copy is never trusted
    downstream.
    A no-op when ``before`` is ``None`` (nothing was captured). Timestamp-only touches do not trip
    it.
    """
    if before is None:
        return
    task_dir = exchange_task_dir(exchange_root, task_id)
    try:
        after = build_exchange_manifest(task_dir, task_id)
    except ExchangeError as exc:
        raise NodeManualRequired(f"exchange integrity (post-run): {exc}") from exc
    changes = diff_exchange_manifests(before, after)
    if changes:
        raise ExchangeMutationManual(
            f"node {node_id!r}: exchange mutated during a provider attempt ({'; '.join(changes)})",
            before=before,
            after=after,
        )


def publish_artifact(
    exchange_root: str,
    task_id: str,
    relpath: str,
    content: str | bytes,
    *,
    extra_secrets: Iterable[str] = (),
    private_path: str,
) -> str:
    """Publish ``content`` to ``<exchange>/<task>/<relpath>`` (redacted); else the private path."""
    if not exchange_root:
        return private_path
    task_dir = exchange_task_dir(exchange_root, task_id)
    return publish_to_exchange(task_dir, relpath, content, extra_secrets=extra_secrets)


def publish_file(
    exchange_root: str,
    task_id: str,
    relpath: str,
    source_path: str,
    *,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Publish an already-written private file's bytes to the exchange; else the private path.

    A no-op returning ``source_path`` when no exchange is wired, the source path is empty, or it is
    not a regular file — a missing artifact must not crash the run (kept the private path before).
    """
    if not exchange_root or not source_path or not Path(source_path).is_file():
        return source_path
    return publish_artifact(
        exchange_root,
        task_id,
        relpath,
        Path(source_path).read_bytes(),
        extra_secrets=extra_secrets,
        private_path=source_path,
    )


def publish_node_run_file(
    exchange_root: str,
    task_id: str,
    node_id: str,
    node_run_id: int,
    filename: str,
    content: str | bytes,
    *,
    extra_secrets: Iterable[str] = (),
    private_path: str,
) -> str:
    """Publish a per-run node artifact under ``stages/<node>/run-<N>/<filename>`` in the exchange.

    Derives the relative name from :func:`exchange_node_run_dir` so it stays in lockstep with the
    :func:`exchange_latest_run_file` fan-in. Falls back to the private path when none is wired.
    """
    if not exchange_root:
        return private_path
    run_dir = exchange_node_run_dir(exchange_root, task_id, node_id, node_run_id)
    task_dir = exchange_task_dir(exchange_root, task_id)
    relpath = (run_dir.relative_to(task_dir) / filename).as_posix()
    return publish_artifact(
        exchange_root,
        task_id,
        relpath,
        content,
        extra_secrets=extra_secrets,
        private_path=private_path,
    )
