"""Per-node observability — rendered-prompt, prompt-audit, and provider-attempt records.

Writes the rendered prompt + the prompt-audit records and the per-attempt provider-attempt
rows so the engine path produces the same audit surfaces the integration suite asserts. The
agent/evaluator runners call :func:`record_run_observability` after ``router.run_stage``; it is
keyed by the ``node_runs`` row id, so audit files still sort chronologically.

Gating: provider-attempt rows are always recorded (an audit surface). The on-disk
rendered-prompt / prompt-audit artifacts are written only when the orchestrator wired a
``register_artifact`` callback; the prompt-audit records additionally honor the per-task / global
``prompt_audit`` gate resolved into ``NodeServices.prompt_audit``.

The prompt-audit directory carries the same content twice, one half per reader: the per-step ``.md``
file an operator opens (metadata header + the prompt as readable text) and ``timeline.jsonl``, the
machine-readable record of every step in order.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from wastech_orchestrator.core.flow.usage_accounting import compute_usage_delta
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
from wastech_orchestrator.providers.base import AgentRunResult, NormalizedUsage
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.routing.router import ProviderAttempt, ResolvedRoute, StageOutcome
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
    configured_model: str | None,
    configured_reasoning: str | None,
    started_at: str,
    usage_baseline: NormalizedUsage | None = None,
    baseline_session_id: str | None = None,
) -> None:
    """Record provider attempts + (when wired) the rendered prompt and the prompt-audit step.

    ``usage_baseline`` / ``baseline_session_id`` are the resumed session's previous cumulative usage
    and the session it belongs to; the runner reads them from the lineage row (or, for an in-process
    HITL re-run, the first invocation's result) so the per-attempt usage can be reduced to a per-run
    delta. ``None`` for a fresh run.

    ``configured_*`` are the flow node's own ``model`` / ``reasoning`` overrides — ``None`` for a
    node that declares neither. The values the attempts actually ran with are carried by the
    attempt rows themselves (:class:`ProviderAttempt`), which is where the audit reads them.
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
            configured_model=configured_model,
            configured_reasoning=configured_reasoning,
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
    supervisor_function: str | None = None,
    usage_baseline: NormalizedUsage | None = None,
    baseline_session_id: str | None = None,
) -> None:
    """Persist one ``provider_attempts`` row per attempt (primary + any fallback) — always recorded.

    Every row carries the owning ``task_id`` so a cost/usage roll-up sums by task without a
    ``node_runs`` join. ``node_run_id`` is the ``node_runs`` id for a graph node, or ``None``
    for the constant supervisor layer (not a graph node), which passes its phase as
    ``supervisor_function`` instead so its own spend is readable per phase. The result-bearing
    attempt (the router leaves at most one) also carries its normalized token usage as a
    summation-safe per-run delta against the resumed session's baseline. Takes the store + clock
    explicitly (not a full ``NodeServices``) so the supervisor, which has no ``NodeServices``,
    reuses the same recorder.
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
                supervisor_function=supervisor_function,
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


def _settled_attempt(outcome: StageOutcome) -> ProviderAttempt | None:
    """The attempt whose run the stage settled on — the one that produced ``outcome.result``.

    Identity, not provider equality: a stage can invoke the same provider more than once (a
    session-unavailable retry, a transient retry), and only one of those rows is the run the node's
    verdict came from. When every attempt raised, the last row is the one that decided the failure;
    with no attempts at all (a unit harness) there is nothing to name.
    """
    for attempt in outcome.attempts:
        if outcome.result is not None and attempt.result is outcome.result:
            return attempt
    return outcome.attempts[-1] if outcome.attempts else None


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
    configured_model: str | None,
    configured_reasoning: str | None,
    started_at: str,
    secrets: tuple[str, ...],
    register: RegisterArtifact,
) -> None:
    """Write one prompt-audit step (who + redacted prompt) and append it to the timeline.

    The step file is Markdown — a fenced-JSON metadata header, then the prompt verbatim as the
    body — because it is the surface an operator opens: inside a JSON string every newline is an
    escaped ``\n``, so a multi-page prompt reads as one flat line. Nothing is lost by moving the
    prompt out of the JSON, since ``timeline.jsonl`` still carries each record whole and is the
    machine-readable half of this directory.

    ``model`` / ``reasoning`` are the **effective** values the settled attempt ran with, because
    this is the artifact an operator opens to answer "what did this node actually run on"; the
    ``configured_*`` pair beside them is the flow node's (or supervisor phase's) own override,
    ``None`` whenever it overrode nothing. Recording only the override — which is what this
    surface used to carry under the plain names — made the record named for auditing disagree
    with the attempt's ``request.json`` on every node that took a provider default. Each row in
    ``agents`` carries its own pair, so a stage that fell over to another provider does not report
    one model for two different CLIs.
    """
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
            "model": attempt.model,
            "reasoning": attempt.reasoning,
        }
        for attempt in outcome.attempts
    ]
    settled = _settled_attempt(outcome)
    record: dict[str, object] = {
        "node_run_id": run_id,
        "node_id": node_id,
        "subtask": subtask,
        "route_primary": route.primary.value,
        "provider_used": outcome.provider_used.value if outcome.provider_used else None,
        "model": settled.model if settled else None,
        "reasoning": settled.reasoning if settled else None,
        "model_configured": configured_model,
        "reasoning_configured": configured_reasoning,
        "started_at": started_at,
        "agents": agents,
        "prompt": redact_text(prompt, extra_secrets=secrets),
    }
    sub = f"-sub{subtask:02d}" if subtask is not None else ""
    title = f"{node_id} — run {run_id:06d}" + (f" — subtask {subtask:02d}" if sub else "")
    step_path = audit_dir / f"{run_id:06d}-{node_id}{sub}.md"
    step_path.write_text(_render_step(record, title=title), encoding="utf-8")
    timeline_path = audit_dir / "timeline.jsonl"
    with timeline_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    register(task_id, "prompt_audit", str(step_path))
    register(task_id, "prompt_audit_timeline", str(timeline_path))


def _render_step(record: Mapping[str, object], *, title: str) -> str:
    """Render one prompt-audit record as the readable step document.

    Metadata goes into a fenced ``json`` block (still copy-pasteable into a parser); the prompt
    follows as ordinary Markdown body text. The prompt is written last and unescaped, so a prompt
    that contains its own fences or headings cannot break anything above it.
    """
    meta = {key: value for key, value in record.items() if key != "prompt"}
    prompt = str(record.get("prompt", "")).rstrip("\n")
    return (
        f"# {title}\n\n"
        f"```json\n{json.dumps(meta, indent=2, ensure_ascii=False)}\n```\n\n"
        f"## Prompt\n\n{prompt}\n"
    )
