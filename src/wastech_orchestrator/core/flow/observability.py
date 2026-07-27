"""Per-node observability — rendered-prompt, prompt-audit, and provider-attempt records.

Writes the rendered prompt + prompt-audit JSON and the per-attempt provider-attempt rows so the
engine path produces the same audit surfaces the integration suite asserts. The agent/evaluator
runners call :func:`record_run_observability` after ``router.run_stage``; it is keyed by the
``node_runs`` row id, so audit files still sort chronologically.

Gating: provider-attempt rows are always recorded (an audit surface). The on-disk
rendered-prompt / prompt-audit artifacts are written only when the orchestrator wired a
``register_artifact`` callback; the prompt-audit JSON additionally honors the per-task / global
``prompt_audit`` gate resolved into ``NodeServices.prompt_audit``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from wastech_orchestrator.core.flow.usage_accounting import compute_usage_delta
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
from wastech_orchestrator.providers.base import AgentRunResult, NormalizedUsage
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import ProviderAttemptRow

if TYPE_CHECKING:
    # Type-only: importing nodes.base at runtime would re-enter the nodes package whose __init__
    # imports the runners (which import this module) — a cycle. These are used only in annotations.
    from wastech_orchestrator.core.flow.nodes.base import NodeServices, RegisterArtifact


class ProviderAttemptSink(Protocol):
    """The single state-store method :func:`record_provider_attempts` needs, so the recorder is
    reusable by both a graph node (its ``NodeServices.store``) and the constant supervisor layer
    (its own store) without importing the concrete ``StateStore``."""

    def record_provider_attempt(self, attempt: ProviderAttemptRow) -> None: ...


def record_run_observability(
    services: NodeServices,
    *,
    task_id: str,
    node_id: str,
    subtask: int | None,
    run_id: int,
    prompt: str,
    route: ResolvedRoute,
    outcome: StageOutcome,
    model: str | None,
    reasoning: str | None,
    started_at: str,
    usage_baseline: NormalizedUsage | None = None,
    baseline_session_id: str | None = None,
) -> None:
    """Record provider attempts + (when wired) the rendered prompt and the prompt-audit step.

    ``usage_baseline`` / ``baseline_session_id`` are the resumed session's previous cumulative usage
    and the session it belongs to; the runner reads them from the lineage row (or, for an in-process
    HITL re-run, the first invocation's result) so the per-attempt usage can be reduced to a per-run
    delta. ``None`` for a fresh run.
    """
    record_provider_attempts(
        services.store,
        services.clock,
        task_id=task_id,
        node_run_id=run_id,
        outcome=outcome,
        usage_baseline=usage_baseline,
        baseline_session_id=baseline_session_id,
    )
    register = services.register_artifact
    if register is None:  # observability not wired (e.g. a bare unit test) — skip file artifacts
        return
    write_rendered_prompt(
        artifacts_root=services.artifacts_root,
        task_id=task_id,
        node_id=node_id,
        run_id=run_id,
        prompt=prompt,
        secrets=services.prompt_secrets,
        register=register,
    )
    if services.prompt_audit:
        write_prompt_audit(
            artifacts_root=services.artifacts_root,
            task_id=task_id,
            node_id=node_id,
            subtask=subtask,
            run_id=run_id,
            prompt=prompt,
            route=route,
            outcome=outcome,
            model=model,
            reasoning=reasoning,
            started_at=started_at,
            secrets=services.prompt_secrets,
            register=register,
        )


def record_provider_attempts(
    store: ProviderAttemptSink,
    clock: Callable[[], str],
    *,
    task_id: str,
    node_run_id: int | None,
    outcome: StageOutcome,
    usage_baseline: NormalizedUsage | None = None,
    baseline_session_id: str | None = None,
) -> None:
    """Persist one ``provider_attempts`` row per attempt (primary + any fallback) — always recorded.

    Every row carries the owning ``task_id`` so a cost/usage roll-up sums by task without a
    ``node_runs`` join. ``node_run_id`` is the ``node_runs`` id for a graph node, or ``None``
    for the constant supervisor layer (not a graph node). The result-bearing attempt (the router
    leaves at most one) also carries its normalized token usage as a summation-safe per-run delta
    against the resumed session's baseline. Takes the store + clock explicitly (not a full
    ``NodeServices``) so the supervisor, which has no ``NodeServices``, reuses the same recorder.
    """
    for attempt in outcome.attempts:
        result = attempt.result
        attempt_dir = (
            str(Path(result.stdout_path).parent) if result and result.stdout_path else None
        )
        scope, delta, status, raw = _usage_fields(result, usage_baseline, baseline_session_id)
        store.record_provider_attempt(
            ProviderAttemptRow(
                task_id=task_id,
                node_run_id=node_run_id,
                provider=attempt.provider.value,
                attempt=attempt.attempt,
                status=attempt.status.value if attempt.status else None,
                error_class=attempt.error_class.value if attempt.error_class else None,
                exit_code=result.exit_code if result else None,
                attempt_dir=attempt_dir,
                # Stamp the attempt's real measured interval from the result (already the
                # values the prompt-audit artifact uses below); fall back to the clock only for a
                # result-less attempt (an infra fallback that never produced a result).
                started_at=result.started_at if result else clock(),
                finished_at=result.finished_at if result else clock(),
                usage_scope=scope,
                usage_input_total=delta.input_total if delta else None,
                usage_cache_read=delta.cache_read if delta else None,
                usage_cache_write=delta.cache_write if delta else None,
                usage_uncached_input=delta.uncached_input if delta else None,
                usage_output_total=delta.output_total if delta else None,
                usage_reasoning_output=delta.reasoning_output if delta else None,
                usage_cost=delta.cost if delta else None,
                usage_delta_status=status,
                provider_usage_raw=raw,
            )
        )


def _usage_fields(
    result: AgentRunResult | None,
    baseline: NormalizedUsage | None,
    baseline_session_id: str | None,
) -> tuple[str | None, NormalizedUsage | None, str | None, str | None]:
    """``(scope, per-run delta, delta status, raw JSON)`` for one attempt's usage columns.

    All ``None`` when the attempt reported no usage (a result-less fallback attempt, or a provider
    that emitted none). An ``unknown`` delta returns ``(scope, None, "unknown", raw)`` — the raw
    payload and scope are kept, but no numbers.
    """
    if result is None or result.normalized_usage is None:
        return None, None, None, None
    delta, status = compute_usage_delta(
        result.normalized_usage,
        baseline,
        current_session_id=result.session_id,
        baseline_session_id=baseline_session_id,
    )
    raw = json.dumps(result.usage, separators=(",", ":")) if result.usage is not None else None
    return result.normalized_usage.scope.value, delta, status, raw


def write_rendered_prompt(
    *,
    artifacts_root: str,
    task_id: str,
    node_id: str,
    run_id: int,
    prompt: str,
    secrets: tuple[str, ...],
    register: RegisterArtifact,
) -> None:
    """Persist the rendered (redacted) node prompt for audit, once per node run.

    Keyed by the flow ``node_id`` **and** the reserved ``run_id`` (via :func:`node_run_dir`), so it
    sits next to that run's provider attempts and a re-running node keeps every pass's prompt on
    disk instead of clobbering the last. ``run_id`` uniqueness subsumes the old ``sub-NN/`` level.
    """
    run_dir = node_run_dir(artifacts_root, task_id, node_id, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "rendered-prompt.md"
    path.write_text(redact_text(prompt, extra_secrets=secrets), encoding="utf-8")
    register(task_id, "rendered_prompt", str(path))


def write_prompt_audit(
    *,
    artifacts_root: str,
    task_id: str,
    node_id: str,
    subtask: int | None,
    run_id: int,
    prompt: str,
    route: ResolvedRoute,
    outcome: StageOutcome,
    model: str | None,
    reasoning: str | None,
    started_at: str,
    secrets: tuple[str, ...],
    register: RegisterArtifact,
) -> None:
    """Write one prompt-audit step (who + redacted prompt) and append it to the timeline."""
    audit_dir = task_artifact_dir(artifacts_root, task_id) / "prompt-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    agents = [
        {
            "provider": attempt.provider.value,
            "attempt": attempt.attempt,
            "is_fallback": attempt.provider != route.primary,
            "status": attempt.status.value if attempt.status else None,
            "error_class": attempt.error_class.value if attempt.error_class else None,
            "started_at": attempt.result.started_at if attempt.result else None,
            "finished_at": attempt.result.finished_at if attempt.result else None,
        }
        for attempt in outcome.attempts
    ]
    record: dict[str, object] = {
        "node_run_id": run_id,
        "node_id": node_id,
        "subtask": subtask,
        "route_primary": route.primary.value,
        "provider_used": outcome.provider_used.value if outcome.provider_used else None,
        "model": model,
        "reasoning": reasoning,
        "started_at": started_at,
        "agents": agents,
        "prompt": redact_text(prompt, extra_secrets=secrets),
    }
    sub = f"-sub{subtask:02d}" if subtask is not None else ""
    step_path = audit_dir / f"{run_id:06d}-{node_id}{sub}.json"
    step_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    timeline_path = audit_dir / "timeline.jsonl"
    with timeline_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    register(task_id, "prompt_audit", str(step_path))
    register(task_id, "prompt_audit_timeline", str(timeline_path))
