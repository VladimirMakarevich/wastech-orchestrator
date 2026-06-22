"""Constant supervisor layer above any flow (flow-engine P2.1).

The supervisor is **not** a graph node — it is an orchestrator-level oversight layer that exists for
every task under any flow shape (even a single implement agent with no checks/review). It starts at
task start, lives the whole cycle, observes each completed step read-only through its **own**
``resume_own_lineage`` session (≈one LLM call/step, accumulating context across steps), and at
whole-task close synthesizes the ``summary`` + advisory caveats.

It is **advisory by construction**: it never reworks, reopens, or routes. Each observation is
recorded as an immutable ``evaluations`` row (``supervisor_step`` / ``supervisor_final``,
``verdict='advisory'``) and surfaced to the human (the summary becomes the PR body), but the engine
never consumes it to route. Blocking is the job of in-flow ``review`` / ``test_quality`` evaluators.

Configured in ``config.yaml`` (``supervisor: {model, reasoning, role_file}``) under the same ceiling
as flow nodes: ``permission_profile`` is forced ``read-only`` here, ``reasoning`` ∈ the allowlist
(loader), ``role_file`` is path-contained (validator + the renderer's flow-dir containment). The own
``resume_own_lineage`` session is durable: it is persisted to / hydrated from ``node_lineage`` under
a reserved sentinel node id (``state.db`` only), so a resumed task continues the supervisor's
accumulated cross-step context instead of starting blind — gated by provider match, exactly like the
``resume_own_lineage`` evaluator.

It replaces the old summary provider and the removed blocking ``supervise_impl`` / ``supervise_fix``
nodes (2026-06-19 revision; see ``docs/backlog/flows/flow-contract.md``,
``p2-implementation.md``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from wastech_orchestrator.config.schema import SupervisorConfig
from wastech_orchestrator.core.flow.nodes.base import RegisterArtifact, RouterPort
from wastech_orchestrator.core.flow.prompt import RoleFileError, render_role_prompt
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import AgentRunRequest, ProviderId
from wastech_orchestrator.routing.router import ResolvedRoute
from wastech_orchestrator.state_store import EvaluationRow, NodeLineageRow

_LOG = logging.getLogger(__name__)

# The supervisor's read-only requests carry a dedicated ``supervisor`` node identity (audit dir /
# route label); it is not a graph node, so it records ``evaluations`` rows, never ``node_runs``.
_SUPERVISOR_IDENTITY = "supervisor"

# The reserved ``node_lineage`` key under which the supervisor's own durable session lives. It is a
# double-underscore sentinel, distinct from the routing identity above, so it can never collide with
# a real flow node id (an operator could legally name an evaluator node ``supervisor``).
_SUPERVISOR_LINEAGE_NODE_ID = "__supervisor__"


class SupervisorStorePort(Protocol):
    """The slice of the state store the supervisor needs: the immutable evaluation row plus the
    durable own-session lineage (read on first use, written after each turn)."""

    def record_evaluation(self, row: EvaluationRow) -> int: ...

    def get_node_lineage(
        self, task_id: str, node_id: str, subtask_order: int | None = None
    ) -> NodeLineageRow | None: ...

    def upsert_node_lineage(self, row: NodeLineageRow) -> None: ...


class Supervisor:
    """The per-task constant oversight layer. One instance per task (it carries its own session)."""

    def __init__(
        self,
        *,
        settings: SupervisorConfig,
        router: RouterPort,
        store: SupervisorStorePort,
        repo_dir: str,
        artifacts_root: str | Path,
        flow_dir: Path,
        register_artifact: RegisterArtifact | None = None,
        default_timeout_seconds: int = 7200,
    ) -> None:
        self._settings = settings
        self._router = router
        self._store = store
        self._repo_dir = repo_dir
        self._artifacts_root = artifacts_root
        self._flow_dir = flow_dir
        self._register_artifact = register_artifact
        self._default_timeout_seconds = default_timeout_seconds
        # The supervisor's own session (resume_own_lineage). Held in-memory within a process run and
        # persisted to / hydrated from ``node_lineage`` so it survives a restart (independent of the
        # editing-lineage authors). ``None`` until the first turn runs or the persisted row is read.
        self._own_session_id: str | None = None

    # -- per-step observation --------------------------------------------------

    def observe(
        self,
        *,
        task_id: str,
        node_id: str,
        node_run_id: int,
        outcome_kind: str,
        final_message: str | None = None,
        subtask_order: int | None = None,
    ) -> None:
        """Observe one completed step read-only and record an advisory ``supervisor_step`` row.

        Best-effort: a failed observation is logged and swallowed — it is advisory and must never
        fail or reroute the task. Namespaced by ``source_node_run_id`` (the step), so a resumed run
        does not duplicate observations.
        """
        prompt = self._step_prompt(task_id, node_id, outcome_kind, final_message)
        note = self._run(task_id, prompt)
        self._record(
            task_id,
            kind="supervisor_step",
            source_node_run_id=node_run_id,
            subtask_order=subtask_order,
            payload={"node": node_id, "outcome": outcome_kind, "note": note or ""},
        )

    # -- whole-task finalize ---------------------------------------------------

    def finalize(self, *, task_id: str, task_title: str) -> Path | None:
        """Synthesize the whole-task summary (once, at task close) and record ``supervisor_final``.

        Writes the working ``summary.{md,json}`` under the task artifact dir (the ``summary.md`` is
        the PR body). Best-effort: when the synthesis LLM call cannot run, no ``summary.md`` is
        written and ``None`` is returned, so the orchestrator's deterministic minimal-summary
        fallback applies (summary is *always* written, by one path or the other). ``summary.json``
        (local-only metadata) is always written. Returns the ``summary.md`` path, or ``None``.
        """
        summary_text = self._run(task_id, self._finalize_prompt(task_id, task_title))
        self._record(
            task_id,
            kind="supervisor_final",
            source_node_run_id=None,
            subtask_order=None,
            payload={"summary_written": summary_text is not None},
        )
        task_dir = Path(task_artifact_dir(self._artifacts_root, task_id))
        self._write_summary_json(task_dir, task_id, task_title, summary_text)
        if not summary_text or not summary_text.strip():
            return None  # orchestrator's deterministic minimal summary writes summary.md
        md_path = task_dir / "summary.md"
        md_path.write_text(summary_text.rstrip("\n") + "\n", encoding="utf-8")
        self._register(task_id, "summary_md", str(md_path))
        return md_path

    # -- internals -------------------------------------------------------------

    def _run(self, task_id: str, prompt: str) -> str | None:
        """Run one read-only supervisor LLM turn on its own session; return the final message.

        Continues the supervisor's own ``resume_own_lineage`` session: it resumes the in-memory id,
        or — on a fresh process after a restart — the persisted ``node_lineage`` session, and writes
        the new session id back after the turn. Best-effort by contract: any failure (no provider,
        infra error, role file unreadable) is logged and yields ``None`` — never raised.
        """
        try:
            route = self._router.resolve_route(_SUPERVISOR_IDENTITY, None)
            request = AgentRunRequest(
                task_id=task_id,
                node_id=_SUPERVISOR_IDENTITY,
                working_directory=self._repo_dir,
                prompt=prompt,
                permission_profile="read-only",  # forced — the supervisor never writes
                timeout_seconds=self._default_timeout_seconds,
                attempt=1,
                node_run_id=0,  # not a graph node; audit lives in the ``evaluations`` table
                model=self._settings.model,
                reasoning=self._settings.reasoning,
                session_id=self._resume_session(task_id, route),
            )
            outcome = self._router.run_stage(request, route)
        except Exception as exc:  # noqa: BLE001 — advisory layer must never break the task
            _LOG.warning(
                "supervisor observation failed (advisory, ignored)",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
            )
            return None
        result = outcome.result
        if result is None:
            return None
        if result.session_id:
            self._own_session_id = result.session_id  # resume_own_lineage continuity (in-memory)
            self._persist_session(task_id, result.session_id, outcome.provider_used)
        return result.final_message

    def _resume_session(self, task_id: str, route: ResolvedRoute) -> str | None:
        """The own session to resume: the in-memory id if a turn already ran this process, else the
        persisted lineage — but only when produced by the provider now resolved (you cannot resume a
        Claude session on Codex). On the first round there is no lineage yet, so it starts fresh.
        """
        if self._own_session_id is not None:
            return self._own_session_id
        row = self._store.get_node_lineage(task_id, _SUPERVISOR_LINEAGE_NODE_ID, None)
        if row is None or row.provider != route.primary.value:
            return None
        self._own_session_id = row.raw_session_id
        return self._own_session_id

    def _persist_session(
        self, task_id: str, session_id: str, provider_used: ProviderId | None
    ) -> None:
        """Persist the supervisor's own session after a successful turn (``state.db`` only — the raw
        id is redacted everywhere else), keyed by the reserved sentinel so a resumed task resumes
        it.
        """
        if provider_used is None:
            return
        self._store.upsert_node_lineage(
            NodeLineageRow(
                task_id=task_id,
                node_id=_SUPERVISOR_LINEAGE_NODE_ID,
                provider=provider_used.value,
                raw_session_id=session_id,
                subtask_order=None,
            )
        )

    def _record(
        self,
        task_id: str,
        *,
        kind: str,
        source_node_run_id: int | None,
        subtask_order: int | None,
        payload: dict[str, object],
    ) -> None:
        self._store.record_evaluation(
            EvaluationRow(
                task_id=task_id,
                node_id=None,  # the supervisor is a layer, not a node
                source_node_run_id=source_node_run_id,
                subtask_order=subtask_order,
                kind=kind,
                verdict="advisory",  # the supervisor never routes
                findings_json=json.dumps(payload, ensure_ascii=False),
            )
        )

    def _step_prompt(
        self, task_id: str, node_id: str, outcome_kind: str, final_message: str | None
    ) -> str:
        observed = f"## Step observed\nNode: {node_id}\nOutcome: {outcome_kind}\n"
        if final_message:
            observed += f"\nThe step reported:\n{final_message}\n"
        return self._base_prompt(task_id) + "\n\n" + observed

    def _finalize_prompt(self, task_id: str, task_title: str) -> str:
        return (
            self._base_prompt(task_id)
            + "\n\n## Final synthesis\n"
            + f"Synthesize a plain-language summary of the whole task ({task_title}): what was "
            + "done, how it works, how it integrates, and why. List any advisory caveats / "
            + "follow-ups you noted across the steps in a final section. Do not edit code.\n"
        )

    def _base_prompt(self, task_id: str) -> str:
        try:
            return render_role_prompt(
                self._flow_dir,
                self._settings.role_file,
                {"task_id": task_id, "repo": self._repo_dir, "repo_path": self._repo_dir},
            )
        except RoleFileError:
            # Fall back to a minimal instruction so a missing/bad role file never breaks the task
            # (the call is best-effort; the validator already rejects a traversal role_file).
            return "You are a read-only supervisor observing a software task. Do not edit code."

    def _write_summary_json(
        self, task_dir: Path, task_id: str, task_title: str, summary_text: str | None
    ) -> None:
        """Write the local-only ``summary.json`` metadata (never committed). Always written."""
        payload = {"what": task_title, "summary": summary_text or ""}
        path = task_dir / "summary.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError:
            return
        self._register(task_id, "summary_json", str(path))

    def _register(self, task_id: str, kind: str, path: str) -> None:
        if self._register_artifact is not None:
            self._register_artifact(task_id, kind, path)
