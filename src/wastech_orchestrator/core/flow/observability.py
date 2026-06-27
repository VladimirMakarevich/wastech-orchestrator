"""Per-node observability (P1.4) — rendered-prompt, prompt-audit, and provider-attempt records.

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
from pathlib import Path
from typing import TYPE_CHECKING

from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.state_store import ProviderAttemptRow

if TYPE_CHECKING:
    # Type-only: importing nodes.base at runtime would re-enter the nodes package whose __init__
    # imports the runners (which import this module) — a cycle. These are used only in annotations.
    from wastech_orchestrator.core.flow.nodes.base import NodeServices, RegisterArtifact


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
) -> None:
    """Record provider attempts + (when wired) the rendered prompt and the prompt-audit step."""
    record_provider_attempts(services, run_id, outcome)
    register = services.register_artifact
    if register is None:  # observability not wired (e.g. a bare unit test) — skip file artifacts
        return
    write_rendered_prompt(
        artifacts_root=services.artifacts_root,
        task_id=task_id,
        node_id=node_id,
        subtask=subtask,
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


def record_provider_attempts(services: NodeServices, run_id: int, outcome: StageOutcome) -> None:
    """Persist one ``provider_attempts`` row per attempt (primary + any fallback) — always recorded.

    The row's ``node_run_id`` holds the ``node_runs`` id of the run these attempts belong to.
    """
    for attempt in outcome.attempts:
        attempt_dir = (
            str(Path(attempt.result.stdout_path).parent)
            if attempt.result and attempt.result.stdout_path
            else None
        )
        services.store.record_provider_attempt(
            ProviderAttemptRow(
                node_run_id=run_id,
                provider=attempt.provider.value,
                attempt=attempt.attempt,
                status=attempt.status.value if attempt.status else None,
                error_class=attempt.error_class.value if attempt.error_class else None,
                exit_code=attempt.result.exit_code if attempt.result else None,
                attempt_dir=attempt_dir,
                started_at=services.clock(),
                finished_at=services.clock(),
            )
        )


def write_rendered_prompt(
    *,
    artifacts_root: str,
    task_id: str,
    node_id: str,
    subtask: int | None,
    prompt: str,
    secrets: tuple[str, ...],
    register: RegisterArtifact,
) -> None:
    """Persist the rendered (redacted) node prompt for audit, once per node run.

    Keyed by the flow ``node_id`` so distinct nodes (even same-capability ones in a research/audit
    flow) each get their own ``stages/<node_id>/`` directory and never overwrite each other.
    """
    node_dir = task_artifact_dir(artifacts_root, task_id) / "stages" / node_id
    if subtask is not None:
        node_dir = node_dir / f"sub-{subtask:02d}"
    node_dir.mkdir(parents=True, exist_ok=True)
    path = node_dir / "rendered-prompt.md"
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
