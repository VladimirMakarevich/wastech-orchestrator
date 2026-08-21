"""Unit tests for the constant supervisor layer + evaluator primitive.

The supervisor is the orchestrator-level oversight layer above any flow: per-step read-only
observation in its own resume_own_lineage session, advisory-only (never reworks/routes), and a
single whole-task summary at close. The evaluator primitive is the immutable ``evaluations`` table
plus the single ``record_rework`` accounting path.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import (
    ObserveMode,
    SupervisorConfig,
    SupervisorObserveConfig,
    SupervisorTurnConfig,
)
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.core.flow.engine import Finding
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import SupervisorBlock
from wastech_orchestrator.core.follow_ups import FINDING_TITLE_MAX
from wastech_orchestrator.core.loop_control import record_rework
from wastech_orchestrator.core.skills import SkillInventory, SkillRef
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.core.supervisor import (
    _HANDOFF_RUN_ID_BASE,
    _SUMMARY_MIN_CHARS,
    Supervisor,
)
from wastech_orchestrator.git_manager import GitControlDrift, GitControlDriftItem
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    NormalizedUsage,
    ProviderId,
    RunStatus,
    UsageScope,
)
from wastech_orchestrator.routing.router import (
    ProviderAttempt,
    ResolvedRoute,
    RouteSource,
    StageOutcome,
)
from wastech_orchestrator.runtime_layout import ProviderWriteGuardPolicy
from wastech_orchestrator.state_store import (
    CheckRunRow,
    EditingLineageRow,
    EvaluationRow,
    NodeRunRow,
    StateStore,
    TaskRow,
)

_TASK = "task-1"


def _prose(lead: str) -> str:
    """*lead* plus enough structured prose to clear finalize's collapsed-generation floor.

    ``supervisor._SUMMARY_MIN_CHARS`` discards a shorter summary as a collapse (an observed run
    published ``summary: "test"`` as its PR body), so a fixture standing in for a real synthesis has
    to look like one. The lead sentence stays first, so assertions about it are unaffected.
    """
    return (
        f"{lead}\n\n## What changed\nThe parser validates its input before dispatch, and the "
        "callers now pass the typed record.\n\n## Why\nThe untyped path accepted malformed rows "
        "silently, so the failure surfaced two layers away from its cause."
    )


def _ok(session_id: str = "sess-super", message: str = "noted") -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="claude",
        node_id="supervisor",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message=message,
        session_id=session_id,
    )


class FakeRouter:
    """Records requests; returns scripted results (``None`` => infra-unavailable for that call)."""

    def __init__(self, results: list[AgentRunResult | None] | None = None) -> None:
        self.requests: list[Any] = []
        self.route_providers: list[Any] = []  # the provider arg passed to resolve_route per call
        self._results = list(results) if results is not None else None

    def route_grants_shell(
        self, route: ResolvedRoute, *, permission_profile: Any = None, git_evidence: bool = False
    ) -> bool:
        # The real Router asks the adapters whether this attempt gets a shell. The double
        # answers from the node's grant — a Claude-shaped answer — unless a test sets
        # ``grants_shell`` to model a provider whose profile carries a shell on its own
        # (Codex ``read-only``) or a host where it was dropped.
        override = getattr(self, "grants_shell", None)
        return git_evidence if override is None else bool(override)

    def resolve_route(self, node_id: str, provider: Any = None) -> ResolvedRoute:
        self.route_providers.append(provider)
        return ResolvedRoute(
            node_id=node_id, primary=ProviderId.CLAUDE, fallback=None, source=RouteSource.CONFIG
        )

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        self.requests.append(request)
        if self._results is not None:
            result = self._results.pop(0) if self._results else None
        else:
            result = _ok()
        return StageOutcome(
            route=route,
            result=result,
            provider_used=ProviderId.CLAUDE if result is not None else None,
            stage_attempts=1,
            terminal_error=None,
            attempts=(),
        )


def _store(tmp_path: Path) -> StateStore:
    store = StateStore.open(tmp_path / "state.db")
    store.insert_task(TaskRow(task_id=_TASK, title="T", status=Status.RUNNING))
    return store


def _supervisor(
    tmp_path: Path,
    router: Any,
    store: StateStore,
    *,
    model: str | None = None,
    reasoning: str | None = None,
    provider: ProviderId | None = None,
    observe: SupervisorObserveConfig | None = None,
    finalize: SupervisorTurnConfig | None = None,
    flow_supervisor: SupervisorBlock | None = None,
    flow_name: str | None = None,
    task_type: str | None = None,
    register_artifact: Any = None,
    prompt_audit: bool = False,
    prompt_secrets: tuple[str, ...] = (),
    security_preamble: str | None = None,
    repo_dir: str = "/repo",
    exchange_root: str = "",
    git: Any = None,
) -> Supervisor:
    (tmp_path / "roles").mkdir(exist_ok=True)
    (tmp_path / "roles" / "supervisor.md").write_text("Observe {task_id} in {repo}.", "utf-8")
    return Supervisor(
        settings=SupervisorConfig(
            role_file="roles/supervisor.md",
            provider=provider,
            # `model`/`reasoning` are the "same pair on every phase" shorthand, which is what a test
            # about anything other than the split wants; a test about the split itself passes an
            # explicit `observe=` / `finalize=` block.
            observe=observe or SupervisorObserveConfig(model=model, reasoning=reasoning),
            finalize=finalize or SupervisorTurnConfig(model=model, reasoning=reasoning),
            handoff=SupervisorTurnConfig(model=model, reasoning=reasoning),
        ),
        router=router,
        store=store,
        repo_dir=repo_dir,
        git=git,
        artifacts_root=str(tmp_path / "art"),
        exchange_root=exchange_root,
        flow_dir=tmp_path,
        flow_supervisor=flow_supervisor,
        flow_name=flow_name,
        task_type=task_type,
        register_artifact=register_artifact,
        prompt_audit=prompt_audit,
        prompt_secrets=prompt_secrets,
        security_preamble=security_preamble,
    )


def _wired(tmp_path: Path, router: Any, store: StateStore, **kwargs: Any) -> Supervisor:
    """A supervisor whose exchange lives inside a real repo dir, so packet paths are resolvable.

    The default :func:`_supervisor` keeps the historical unit-harness wiring (a fake ``/repo``, no
    exchange), where publication is skipped and the private path is used — fine for the turns that
    do not care. The packet tests need the real seam: an exchange under the repo, so the published
    copy exists and the repo-relative paths inside the packet can be asserted.
    """
    repo = tmp_path / "repo"
    (repo / ".worc-io").mkdir(parents=True, exist_ok=True)
    return _supervisor(
        tmp_path,
        router,
        store,
        repo_dir=str(repo),
        exchange_root=str(repo / ".worc-io"),
        **kwargs,
    )


# -- per-step observation -----------------------------------------------------


def test_supervisor_request_carries_security_preamble(tmp_path: Path) -> None:
    # The supervisor's own read-only turn carries the Core-owned preamble too.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(
        tmp_path, router, store, security_preamble="[Orchestrator security contract] baseline"
    )
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    assert router.requests[0].security_preamble == "[Orchestrator security contract] baseline"


def test_supervisor_records_one_advisory_row_per_observed_step(tmp_path: Path) -> None:
    # Which steps reach `observe` at all is the orchestrator hook's decision (`tool`/`checks`/
    # `publish` never do — see tests/core/test_orchestrator.py); this pins what one observation
    # costs and records: one read-only turn on the layer's own session, one advisory row.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    sup.observe(task_id=_TASK, node_id="review", node_run_id=7, outcome_kind="accept")

    # One read-only LLM call per completed step, in its own resume_own_lineage session: the first is
    # fresh (no session) and the second resumes the first call's session id.
    assert len(router.requests) == 2
    assert all(r.permission_profile == "read-only" for r in router.requests)
    assert router.requests[0].session_id is None
    assert router.requests[1].session_id == "sess-super"

    # One advisory supervisor_step row per step, namespaced by the source node_run id (not a node).
    evals = store.get_evaluations(_TASK)
    assert [e.kind for e in evals] == ["supervisor_step", "supervisor_step"]
    assert [e.source_node_run_id for e in evals] == [5, 7]
    assert all(e.verdict == "advisory" and e.node_id is None for e in evals)


def test_observe_prompt_carries_the_evaluator_findings_digest(tmp_path: Path) -> None:
    # `_step_prompt` had no slot for findings, so the observation for the critic step
    # was a bare `## Step observed / Outcome: accept` with nothing to react to — and the observer
    # made zero tool calls on every evaluator step of the run this came from. Severity + reason +
    # paths now reach the prompt, which is what the observer is being asked to acknowledge.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(
        task_id=_TASK,
        node_id="critical_review",
        node_run_id=82,
        outcome_kind="accept",
        findings=(
            Finding(severity="medium", reason="Uneven audit depth", paths=("report.md",)),
            Finding(severity="low", reason="Wording nit"),
        ),
    )
    prompt = router.requests[0].prompt
    assert "## Step observed" in prompt
    assert "Node: critical_review" in prompt
    assert "Outcome: accept" in prompt
    assert "- [medium] Uneven audit depth (report.md)" in prompt
    assert "- [low] Wording nit" in prompt


def test_observe_prompt_has_no_findings_section_when_there_are_none(tmp_path: Path) -> None:
    # An agent step, or a clean evaluator: no heading, no empty list.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    assert "Findings it recorded" not in router.requests[0].prompt


def test_observe_prompt_bounds_a_long_finding_reason(tmp_path: Path) -> None:
    # A chatty evaluator must not inflate every per-step turn: the digest line is capped at the same
    # bound the follow-up titles use, and newlines are folded so one finding stays one line.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(
        task_id=_TASK,
        node_id="review",
        node_run_id=9,
        outcome_kind="accept",
        findings=(Finding(severity="high", reason="y\n" * 200),),
    )
    digest = next(
        line for line in router.requests[0].prompt.splitlines() if line.startswith("- [high]")
    )
    assert len(digest) <= FINDING_TITLE_MAX + len("- [high] ") + 1
    assert digest.endswith("…")


def test_observe_prompt_bounds_a_long_step_message(tmp_path: Path) -> None:
    # The node's own closing message reached this prompt unbounded, so one chatty node inflated
    # every observation of the run — and each rework round paid again. Same cap as the packet's.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(
        task_id=_TASK,
        node_id="revise",
        node_run_id=5,
        outcome_kind="done",
        final_message="z" * 900,
    )
    reported = router.requests[0].prompt.split("The step reported:\n")[1].strip()
    assert len(reported) == 500 and reported.endswith("…")


def test_supervisor_observe_writes_rendered_prompt_when_registered(tmp_path: Path) -> None:
    # A supervisor turn is now part of the audit trail: previously rendered-prompt.md / the
    # prompt-audit JSON were never written for observe/finalize/handoff turns at all.
    registered: list[Any] = []
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(
        tmp_path, router, store, register_artifact=lambda t, k, p: registered.append((t, k, p))
    )
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")

    rendered = (
        node_run_dir(str(tmp_path / "art"), _TASK, "supervisor", 5) / "rendered-prompt.md"
    ).read_text("utf-8")
    assert rendered  # the observe turn's prompt was persisted
    kinds = {k for _, k, _ in registered}
    assert "rendered_prompt" in kinds
    assert "prompt_audit" not in kinds  # prompt_audit gate defaults to False


def test_supervisor_observability_write_failure_does_not_break_turn(tmp_path: Path) -> None:
    # The audit write is advisory: a failing register_artifact must not affect the turn's own
    # result or its evaluations/session bookkeeping (the layer's "never breaks the task" contract).
    def _raising_register(task_id: str, kind: str, path: str | None) -> None:
        raise RuntimeError("disk full")

    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store, register_artifact=_raising_register)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")

    evals = store.get_evaluations(_TASK)
    assert [e.kind for e in evals] == ["supervisor_step"]
    payload = json.loads(evals[0].findings_json)
    assert payload["observation_failed"] is False  # the LLM turn itself still succeeded


def test_supervisor_pins_its_provider_at_route(tmp_path: Path) -> None:
    # The supervisor resolves the route with its own `provider` (here claude), not an implicit
    # None-inherits-primary — so its claude model reaches claude even under a codex primary.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store, provider=ProviderId.CLAUDE)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    assert router.route_providers == [ProviderId.CLAUDE]


def test_supervisor_inherits_primary_when_provider_unset(tmp_path: Path) -> None:
    # Default (no supervisor.provider) still passes None -> router falls back to the global primary.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    assert router.route_providers == [None]


def test_supervisor_session_is_durable_across_restart(tmp_path: Path) -> None:
    # #10: the supervisor's own resume_own_lineage session is persisted to node_lineage so a
    # resumed task continues its accumulated cross-step context instead of starting blind.
    from wastech_orchestrator.core.supervisor import _SUPERVISOR_LINEAGE_NODE_ID

    store = _store(tmp_path)
    # First process: one observation persists the supervisor's own session under the sentinel id.
    sup1 = _supervisor(tmp_path, FakeRouter(), store)
    sup1.observe(task_id=_TASK, node_id="implementation", node_run_id=1, outcome_kind="done")
    row = store.get_node_lineage(_TASK, _SUPERVISOR_LINEAGE_NODE_ID, None)
    assert row is not None
    assert row.raw_session_id == "sess-super" and row.provider == "claude"

    # A restart rebuilds a fresh Supervisor with no in-memory session; its first turn must resume
    # the persisted session (provider matches) rather than start fresh.
    router2 = FakeRouter()
    sup2 = _supervisor(tmp_path, router2, store)
    sup2.observe(task_id=_TASK, node_id="review", node_run_id=2, outcome_kind="accept")
    assert router2.requests[0].session_id == "sess-super"


def test_supervisor_advisory_never_reworks(tmp_path: Path) -> None:
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=1, outcome_kind="done")
    sup.finalize(task_id=_TASK, task_title="T")

    # Every record the supervisor writes is advisory — it never emits accept/rework and exposes no
    # rework/route capability (it is a layer, not an evaluator node).
    evals = store.get_evaluations(_TASK)
    assert {e.verdict for e in evals} == {"advisory"}
    assert any(e.kind == "supervisor_final" for e in evals)
    assert not hasattr(sup, "rework")
    assert not hasattr(sup, "route")


def test_supervisor_step_artifact_run_id_is_the_observed_step(tmp_path: Path) -> None:
    # Each per-step observation requests its artifact dir under the OBSERVED step's node_run_id, so
    # successive supervisor turns never collide on run-000000 (the writer never overwrites a dir).
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    sup.observe(task_id=_TASK, node_id="review", node_run_id=7, outcome_kind="accept")
    assert [r.node_run_id for r in router.requests] == [5, 7]


# -- upfront skill-map proposal (skills-selection-rework) ---------------------

_INV = SkillInventory(
    skills=(SkillRef("safe-change", "review", ".claude/skills/safe-change/SKILL.md"),)
)


def _proposal_result(assignments: list[dict[str, Any]]) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="claude",
        node_id="supervisor",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message=None,
        structured_output={"assignments": assignments},
        session_id="sess-super",
    )


def test_supervisor_propose_skill_map_parses_and_records(tmp_path: Path) -> None:
    result = _proposal_result([{"node": "implementation", "skills": ["safe-change", "ghost"]}])
    router, store = FakeRouter([result]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    proposed = sup.propose_skill_map(
        task_id=_TASK,
        agent_node_ids=["implementation", "review"],
        inventory=_INV,
        task_path="/x/.worc-io/t/task.md",
    )
    # The supervisor proposes verbatim tokens (the Core resolves them against the inventory later).
    assert proposed == {"implementation": ("safe-change", "ghost")}
    # One read-only structured turn, on its own session, carrying the proposal output schema.
    assert len(router.requests) == 1
    req = router.requests[0]
    assert req.permission_profile == "read-only"
    assert req.output_schema is not None and "assignments" in str(req.output_schema)
    # Recorded as exactly one advisory evaluation row (the supervisor proposes, never routes).
    evals = store.get_evaluations(_TASK)
    assert [e.kind for e in evals] == ["supervisor_skill_proposal"]
    assert evals[0].verdict == "advisory" and evals[0].node_id is None


def test_supervisor_propose_skill_map_skips_when_inventory_empty(tmp_path: Path) -> None:
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    proposed = sup.propose_skill_map(
        task_id=_TASK,
        agent_node_ids=["implementation"],
        inventory=SkillInventory(),
        task_path=None,
    )
    assert proposed == {}
    assert router.requests == []  # no LLM call when there is nothing to propose
    assert store.get_evaluations(_TASK) == []


def test_supervisor_propose_skill_map_best_effort_on_infra_failure(tmp_path: Path) -> None:
    router, store = FakeRouter([None]), _store(tmp_path)  # the turn could not run
    sup = _supervisor(tmp_path, router, store)
    proposed = sup.propose_skill_map(
        task_id=_TASK,
        agent_node_ids=["implementation"],
        inventory=_INV,
        task_path=None,
    )
    assert proposed == {}  # advisory: a failed proposal never raises, the run continues on pins
    evals = store.get_evaluations(_TASK)
    assert [e.kind for e in evals] == ["supervisor_skill_proposal"]
    assert json.loads(evals[0].findings_json)["proposal_failed"] is True


def test_supervisor_records_observation_failure_distinctly(tmp_path: Path) -> None:
    # A failed observation (no provider) is recorded with observation_failed=True — distinct from an
    # empty advisory note ("nothing to add") — so a silent advisory layer is diagnosable.
    router, store = FakeRouter([None, _ok(message="noted")]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=1, outcome_kind="done")
    sup.observe(task_id=_TASK, node_id="review", node_run_id=2, outcome_kind="accept")
    payloads = [json.loads(e.findings_json) for e in store.get_evaluations(_TASK)]
    assert payloads[0]["observation_failed"] is True and payloads[0]["note"] == ""
    assert payloads[1]["observation_failed"] is False and payloads[1]["note"] == "noted"


def test_supervisor_own_session_not_editing_lineage(tmp_path: Path) -> None:
    # The supervisor resumes only its OWN session across steps; it has no editing-session map and
    # every request is read-only, so it can never inherit or overwrite an author's editing lineage.
    router, store = FakeRouter(), _store(tmp_path)
    # An author editing session already exists for the task; the supervisor must not touch it.
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id=_TASK,
            lineage_key="implementation",
            provider="claude",
            raw_session_id="author-session",
        )
    )
    sup = _supervisor(tmp_path, router, store)

    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=1, outcome_kind="done")
    sup.observe(task_id=_TASK, node_id="fixing", node_run_id=2, outcome_kind="done")
    sup.finalize(task_id=_TASK, task_title="T")

    assert router.requests[0].session_id is None  # fresh own session
    assert router.requests[1].session_id == "sess-super"  # resumes its OWN session, not an author's
    assert all(r.permission_profile == "read-only" for r in router.requests)
    # The author's editing lineage is never read into the supervisor's session nor overwritten.
    row = store.get_editing_lineage(_TASK, "implementation")
    assert row is not None and row.raw_session_id == "author-session"


# -- whole-task finalize ------------------------------------------------------


def test_supervisor_runs_above_any_flow_writes_summary(tmp_path: Path) -> None:
    # Independent of flow shape: finalize synthesizes and writes the summary (the PR body) + the
    # local summary.json, and records exactly one supervisor_final row.
    router, store = FakeRouter([_ok("s1", _prose("The whole task summary."))]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    path = sup.finalize(task_id=_TASK, task_title="T").summary_path
    assert path is not None and path.name == "summary.md"
    assert "The whole task summary." in path.read_text("utf-8")

    summary_json = path.with_name("summary.json")
    assert json.loads(summary_json.read_text("utf-8"))["what"] == "T"
    finals = [e for e in store.get_evaluations(_TASK) if e.kind == "supervisor_final"]
    assert len(finals) == 1


def test_supervisor_finalize_best_effort_when_llm_unavailable(tmp_path: Path) -> None:
    # finalize is best-effort: no provider result → no summary.md (the orchestrator's deterministic
    # minimal-summary fallback then applies), but summary.json + the advisory final row are written
    # so the summary is *always* recorded.
    router, store = FakeRouter([None]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    path = sup.finalize(task_id=_TASK, task_title="T").summary_path
    assert path is None
    summary_json = Path(task_artifact_dir(tmp_path / "art", _TASK)) / "summary.json"
    assert summary_json.exists()
    assert len([e for e in store.get_evaluations(_TASK) if e.kind == "supervisor_final"]) == 1


def test_finalize_sanitizes_leaked_structured_dump(tmp_path: Path) -> None:
    # A model that emits its structured output as a `<summary>…</summary><follow_ups>[JSON]
    # </follow_ups><memory_delta>…` text dump must never let those machine sections ride into
    # summary.md (the PR body). Only the human prose survives.
    leaked = (
        f"<summary>{_prose('Refactored the parser and added tests.')}</summary>"
        '<follow_ups>[{"title":"x"}]</follow_ups>'
        '<memory_delta>{"lessons":[]}</memory_delta><lessons>[]</lessons>'
    )
    router, store = FakeRouter([_ok("s1", leaked)]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    path = sup.finalize(task_id=_TASK, task_title="T").summary_path
    assert path is not None
    body = path.read_text("utf-8")
    assert "Refactored the parser and added tests." in body
    for tag in ("<follow_ups>", "<memory_delta>", "<lessons>", "</summary>", "<summary>"):
        assert tag not in body
    assert "lessons" not in body and "title" not in body  # no machine JSON leaked


def test_finalize_discards_a_collapsed_summary_instead_of_publishing_it(
    tmp_path: Path, package_log_text: Callable[[], str]
) -> None:
    # A finalize turn that fights the response schema can collapse to a minimal probe. One did — it
    # wrote a full synthesis three times, was rejected three times for a missing required property,
    # then emitted `{"summary": "test", "follow_ups": []}`, which validated and shipped as the PR
    # body. Existence was the only guard, so nothing fired. Below the floor the prose is discarded:
    # no summary.md (the orchestrator's deterministic report becomes the body, flagged degraded), a
    # WARNING carrying the collapsed text, and a summary.json that does not claim a summary.
    router, store = FakeRouter([_ok("s1", "test")]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    result = sup.finalize(task_id=_TASK, task_title="T")

    assert result.summary_path is None
    assert not (Path(task_artifact_dir(tmp_path / "art", _TASK)) / "summary.md").exists()
    logged = package_log_text()
    assert f"below the {_SUMMARY_MIN_CHARS}-char floor" in logged and "'test'" in logged
    payload = json.loads(
        (Path(task_artifact_dir(tmp_path / "art", _TASK)) / "summary.json").read_text("utf-8")
    )
    assert payload["summary"] == ""


def test_the_floor_keeps_a_terse_but_complete_prose_summary(tmp_path: Path) -> None:
    # The floor is enforced for every flow, and the packaged prose lenses (blog_article,
    # content_translate, …) ask for four labelled points and tell the turn to keep it concrete. A
    # complete answer to all four on a small revision is short — and discarding it would replace
    # finished work with a mechanical report on a healthy run, which is a worse PR body, not a
    # better one. Every other finalize test runs the code-flow fixture, so this shape needs its own.
    concise = (
        "**What** — tightened the opening of posts/foo.md. **How** — moved the claim into the "
        "first sentence. **Sources** — none added. **Product** — unchanged."
    )
    assert len(concise) < 200  # the kind of summary a prose lens is asked to produce
    router, store = FakeRouter([_ok("s1", concise)]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)

    path = sup.finalize(task_id=_TASK, task_title="Revise the intro").summary_path

    assert path is not None and concise in path.read_text("utf-8")
    final = next(e for e in store.get_evaluations(_TASK) if e.kind == "supervisor_final")
    assert json.loads(final.findings_json)["summary_written"] is True


def test_supervisor_final_summary_written_matches_what_reached_disk(tmp_path: Path) -> None:
    # `summary_written` used to be derived from the RAW turn output, before sanitize and before
    # the floor, so the ledger claimed a summary for a run that wrote none — exactly the case an
    # operator audits. It now states what actually landed.
    dump = '<summary></summary><follow_ups>[{"title":"x"}]</follow_ups>'  # sanitizes to nothing
    cases = ((dump, False), ("test", False), (_prose("Real synthesis."), True))
    # One store per case, under an index-named directory: the messages themselves carry `<`/`>`,
    # which Windows forbids in a path component.
    for index, (message, written) in enumerate(cases):
        root = tmp_path / f"case{index}"
        store = _store(root)
        sup = _supervisor(root, FakeRouter([_ok("s1", message)]), store)
        sup.finalize(task_id=_TASK, task_title="T")
        final = next(e for e in store.get_evaluations(_TASK) if e.kind == "supervisor_final")
        assert json.loads(final.findings_json)["summary_written"] is written


def test_finalize_writes_prompt_audit_when_enabled(tmp_path: Path) -> None:
    registered: list[Any] = []
    router, store = FakeRouter([_ok("s1", _prose("Synthesized summary."))]), _store(tmp_path)
    sup = _supervisor(
        tmp_path,
        router,
        store,
        register_artifact=lambda t, k, p: registered.append((t, k, p)),
        prompt_audit=True,
    )
    sup.finalize(task_id=_TASK, task_title="T")

    audit_dir = task_artifact_dir(str(tmp_path / "art"), _TASK) / "prompt-audit"
    # finalize's node_run_id is the reserved ``0`` sentinel (not a node_runs id).
    step_path = audit_dir / "000000-supervisor.json"
    assert step_path.exists()
    record = json.loads(step_path.read_text("utf-8"))
    assert record["node_id"] == "supervisor"
    assert record["prompt"]  # the finalize turn's input prompt was persisted
    timeline = (audit_dir / "timeline.jsonl").read_text("utf-8").splitlines()
    assert any(json.loads(line)["node_id"] == "supervisor" for line in timeline)


def test_finalize_prefixes_h1_when_missing(tmp_path: Path) -> None:
    # A headless paragraph-slab summary gets a deterministic `# {task_title}` H1 prefix.
    router, store = FakeRouter([_ok("s1", _prose("One flat line of synthesis."))]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    path = sup.finalize(task_id=_TASK, task_title="My Task").summary_path
    assert path is not None
    assert path.read_text("utf-8").startswith("# My Task\n\nOne flat line of synthesis.")


def test_finalize_keeps_model_h1_without_double_prefix(tmp_path: Path) -> None:
    # When the model already opened with its own top-level heading, don't double-prefix.
    router, store = FakeRouter([_ok("s1", _prose("# Model heading\n\nBody."))]), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    path = sup.finalize(task_id=_TASK, task_title="T").summary_path
    assert path is not None
    body = path.read_text("utf-8")
    assert body.startswith("# Model heading")
    assert "# T" not in body


# -- packet-first finalize (P0) ------------------------------------------------


def _record_step(
    store: StateStore, run_id: int, *, node: str, outcome: str, note: str, failed: bool = False
) -> None:
    """Append a supervisor_step observation as a prior process would (append-only in state.db)."""
    store.record_evaluation(
        EvaluationRow(
            task_id=_TASK,
            node_id=None,
            source_node_run_id=run_id,
            kind="supervisor_step",
            verdict="advisory",
            findings_json=json.dumps(
                {"node": node, "outcome": outcome, "note": note, "observation_failed": failed}
            ),
        )
    )


def _packet(sup: Supervisor, tmp_path: Path) -> dict[str, Any]:
    """The published packet JSON for the task (the copy the provider actually read)."""
    published = tmp_path / "repo" / ".worc-io" / _TASK / "supervisor" / "packet.json"
    return dict(json.loads(published.read_text("utf-8")))


def test_finalize_runs_fresh_from_the_packet_even_with_a_live_session(tmp_path: Path) -> None:
    # Inverted from the old contract: an in-process session that just succeeded is NOT resumed. The
    # warm resume was why a revived task got a thinner summary and why this call's input grew with
    # every rework cycle — finalize is now grounded in the packet, on a fresh session, always.
    router, store = (
        FakeRouter([_ok(), _ok("sess-super", _prose("Packet synthesis."))]),
        _store(tmp_path),
    )
    sup = _wired(tmp_path, router, store)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    assert router.requests[0].session_id is None  # the observe turn opened the session

    path = sup.finalize(task_id=_TASK, task_title="T").summary_path

    assert path is not None and "Packet synthesis." in path.read_text("utf-8")
    finalize_req = router.requests[-1]
    assert finalize_req.session_id is None  # NOT resumed, though the session is alive
    assert "## Run facts (the packet)" in finalize_req.prompt
    # The packet travels as a path in its own request field, never as inline JSON in the prompt.
    assert finalize_req.supervisor_packet_path.endswith("/supervisor/packet.json")
    assert "material_observations" not in finalize_req.prompt
    final = next(e for e in store.get_evaluations(_TASK) if e.kind == "supervisor_final")
    assert json.loads(final.findings_json)["packet_built"] is True


def test_finalize_packet_carries_the_observation_digest(tmp_path: Path) -> None:
    # The digest that used to be inlined in the revive prompt is now a packet field, so the same
    # material reaches the turn without being re-sent as prompt input.
    router, store = FakeRouter([_ok("s1", _prose("Synthesis."))]), _store(tmp_path)
    _record_step(store, 5, node="implementation", outcome="done", note="wired the parser")
    _record_step(store, 7, node="review", outcome="accept", note="tests cover the edge case")
    sup = _wired(tmp_path, router, store)

    sup.finalize(task_id=_TASK, task_title="T")

    observations = _packet(sup, tmp_path)["material_observations"]
    assert "wired the parser" in observations and "tests cover the edge case" in observations
    (req,) = router.requests
    assert "wired the parser" not in req.prompt  # material rides the packet, not the prompt


def test_finalize_digest_skips_failed_and_empty_notes(tmp_path: Path) -> None:
    router, store = FakeRouter(), _store(tmp_path)
    _record_step(store, 1, node="planning", outcome="done", note="")  # nothing to add
    _record_step(store, 2, node="impl", outcome="done", note="", failed=True)  # observation failed
    _record_step(store, 3, node="review", outcome="accept", note="looks solid")
    sup = _supervisor(tmp_path, router, store)

    digest = sup._finalize_digest(store.get_evaluations(_TASK))

    assert digest == "- [review → accept] looks solid"  # only the substantive note survives


def test_finalize_digest_none_when_no_usable_observations(tmp_path: Path) -> None:
    router, store = FakeRouter(), _store(tmp_path)
    _record_step(store, 1, node="planning", outcome="done", note="", failed=True)
    sup = _supervisor(tmp_path, router, store)
    assert sup._finalize_digest(store.get_evaluations(_TASK)) is None


def test_finalize_still_runs_when_the_packet_cannot_be_built(tmp_path: Path) -> None:
    # Best-effort by contract: a packet that cannot be built is logged and the turn runs unseeded.
    # There is deliberately NO warm-session fallback — that would put the non-determinism back.
    router, store = FakeRouter([_ok("s1", _prose("Thin but present."))]), _store(tmp_path)
    sup = _wired(tmp_path, router, store)

    def _boom(task_id: str) -> list:
        raise OSError("state.db unreadable")

    sup._store.get_node_runs = _boom  # type: ignore[method-assign]

    path = sup.finalize(task_id=_TASK, task_title="T").summary_path

    assert path is not None and "Thin but present." in path.read_text("utf-8")
    (req,) = router.requests
    assert req.supervisor_packet_path is None
    assert "## Run facts (the packet)" not in req.prompt
    final = next(e for e in store.get_evaluations(_TASK) if e.kind == "supervisor_final")
    assert json.loads(final.findings_json)["packet_built"] is False


# -- the SupervisorPacket itself (P0-D2 / P0-D3) -------------------------------

_DIFF = (
    "diff --git a/src/parser.py b/src/parser.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/parser.py\n"
    "+++ b/src/parser.py\n"
    "@@ -1,2 +1,3 @@\n"
    " keep\n"
    "-gone\n"
    "+added\n"
    "+also added\n"
)


def _run_row(store: StateStore, node: str, kind: str, **kwargs: Any) -> int:
    """Insert a completed node run and return its id (which names its artifact dir)."""
    return store.record_node_run(
        NodeRunRow(
            task_id=_TASK,
            node_id=node,
            node_kind=kind,
            status=kwargs.pop("status", "completed"),
            outcome=kwargs.pop("outcome", "done"),
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:01:00+00:00",
            **kwargs,
        )
    )


def _node_output(tmp_path: Path, node: str, run_id: int, text: str) -> None:
    """Write the per-run ``<node_id>.out.md`` the orchestrator writes for an agent node."""
    run_dir = node_run_dir(tmp_path / "art", _TASK, node, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{node}.out.md").write_text(text, encoding="utf-8")


def _seed_diff(tmp_path: Path, text: str = _DIFF) -> None:
    """Write both copies of ``current.diff``: the private artifact and the exchange copy."""
    task_dir = task_artifact_dir(tmp_path / "art", _TASK)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "current.diff").write_text(text, encoding="utf-8")
    exchange_task = tmp_path / "repo" / ".worc-io" / _TASK
    exchange_task.mkdir(parents=True, exist_ok=True)
    (exchange_task / "current.diff").write_text(text, encoding="utf-8")


def test_packet_is_a_pure_function_of_durable_state(tmp_path: Path) -> None:
    # The reproducibility contract (P0-D2): two builds off the same state.db are byte-identical, so
    # the summary a revive synthesizes is grounded in exactly the same input as the first run's.
    store = _store(tmp_path)
    run_id = _run_row(store, "implementation", "agent", provider_used="claude")
    _node_output(tmp_path, "implementation", run_id, "wired the parser")
    _seed_diff(tmp_path)
    sup = _wired(tmp_path, FakeRouter(), store)

    first = sup._build_packet(_TASK, "T", store.get_evaluations(_TASK))
    second = sup._build_packet(_TASK, "T", store.get_evaluations(_TASK))

    assert first == second  # byte-identical, not merely equivalent
    assert first == json.dumps(json.loads(first), indent=2, sort_keys=True) + "\n"  # canonical


def test_packet_paths_are_repo_relative_posix(tmp_path: Path) -> None:
    # An absolute path would make the bytes machine-dependent; a Windows separator would make them
    # platform-dependent. Both break the byte-identity the criterion above rests on.
    store = _store(tmp_path)
    _seed_diff(tmp_path)
    sup = _wired(tmp_path, FakeRouter(), store)

    packet = json.loads(sup._build_packet(_TASK, "T", []))

    assert packet["changes"]["diff_path"] == f".worc-io/{_TASK}/current.diff"
    # Every path the packet names is relative and POSIX (the inlined diff body is exempt — it is
    # verbatim artifact content, not a path the packet authored).
    for path in [*packet["changes"]["paths"], packet["changes"]["diff_path"]]:
        assert "\\" not in path
        assert not Path(path).is_absolute()
    assert str(tmp_path) not in json.dumps({k: v for k, v in packet.items() if k != "changes"})


def test_packet_revive_without_reexecution_is_unchanged(tmp_path: Path) -> None:
    # A revive that re-executed nothing must reproduce the packet exactly; one that re-executed a
    # node must differ by exactly that step and nothing else.
    store = _store(tmp_path)
    _run_row(store, "implementation", "agent")
    sup = _wired(tmp_path, FakeRouter(), store)
    before = json.loads(sup._build_packet(_TASK, "T", []))

    assert json.loads(sup._build_packet(_TASK, "T", [])) == before  # nothing re-executed

    _run_row(store, "polish", "agent")  # the revive re-entered one more node
    after = json.loads(sup._build_packet(_TASK, "T", []))

    assert after["steps"][:-1] == before["steps"]
    assert after["steps"][-1]["node"] == "polish"
    assert {k: v for k, v in after.items() if k != "steps"} == {
        k: v for k, v in before.items() if k != "steps"
    }


def test_packet_inlines_a_small_diff_but_only_references_a_large_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup = _wired(tmp_path, FakeRouter(), store)

    _seed_diff(tmp_path)
    small = json.loads(sup._build_packet(_TASK, "T", []))["changes"]
    assert small["diff"] == _DIFF  # a small diff rides along: skipping it costs an extra tool round
    assert small["paths"] == ["src/parser.py"]
    assert small["diff_stats"] == {"files": 1, "insertions": 2, "deletions": 1}

    _seed_diff(tmp_path, _DIFF + "".join(f"+filler line {i}\n" for i in range(400)))
    large = json.loads(sup._build_packet(_TASK, "T", []))["changes"]
    assert "diff" not in large  # above the bound only the stats + the artifact path remain
    assert large["paths"] == ["src/parser.py"]
    assert large["diff_path"] == f".worc-io/{_TASK}/current.diff"


def test_packet_bounds_a_long_step_message(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = _run_row(store, "revise", "agent")
    _node_output(tmp_path, "revise", run_id, "x" * 900)
    sup = _wired(tmp_path, FakeRouter(), store)

    message = json.loads(sup._build_packet(_TASK, "T", []))["steps"][0]["message"]

    assert len(message) == 500 and message.endswith("…")


def test_packet_bounds_the_observation_digest(tmp_path: Path) -> None:
    # The oldest lines are dropped (the newest observations matter most) and the cut is marked, so a
    # truncated digest never reads as "that was all there was".
    store = _store(tmp_path)
    for i in range(300):
        _record_step(store, i + 1, node=f"n{i}", outcome="done", note="y" * 60)
    sup = _wired(tmp_path, FakeRouter(), store)

    observations = json.loads(sup._build_packet(_TASK, "T", store.get_evaluations(_TASK)))[
        "material_observations"
    ]

    assert len(observations) <= 8_000
    assert observations.startswith("(older observations dropped")
    assert "[n299 → done]" in observations and "[n0 → done]" not in observations


def test_packet_splits_checks_by_result(tmp_path: Path) -> None:
    # A skipped check (toolchain absent) is never folded into `failed`: the summary must not
    # report a failure where the gate simply did not run. These rows are the only record left of
    # the `checks` node, which is no longer observed.
    store = _store(tmp_path)
    for command, passed, skipped in (("pytest", True, False), ("mypy", False, False)):
        store.record_check_run(
            CheckRunRow(
                task_id=_TASK, command=command, passed=passed, log_path="l", skipped=skipped
            )
        )
    store.record_check_run(
        CheckRunRow(task_id=_TASK, command="cargo", passed=False, log_path="l", skipped=True)
    )
    sup = _wired(tmp_path, FakeRouter(), store)

    checks = json.loads(sup._build_packet(_TASK, "T", []))["checks"]

    assert checks == {"passed": ["pytest"], "failed": ["mypy"], "skipped": ["cargo"]}


def test_packet_records_fallback_and_retry_facts(tmp_path: Path) -> None:
    # Kept, not scrubbed (P0-D2): an attempt that landed on the other provider after two tries is
    # exactly the material a summary caveat is written from.
    store = _store(tmp_path)
    _run_row(
        store,
        "implementation",
        "agent",
        route_primary="codex",
        provider_used="claude",
        stage_attempts=2,
        error_class="rate_limited",
    )
    sup = _wired(tmp_path, FakeRouter(), store)

    step = json.loads(sup._build_packet(_TASK, "T", []))["steps"][0]

    assert step["fallback_from"] == "codex" and step["provider_used"] == "claude"
    assert step["stage_attempts"] == 2 and step["error_class"] == "rate_limited"


def test_packet_step_emits_only_the_facts_a_clean_run_has(tmp_path: Path) -> None:
    # The exact key set matters, not just the values: a blanket `null` per absent fact would inflate
    # every packet and read as a *recorded* absence ("this step has no error_class" vs "no error").
    store = _store(tmp_path)
    run_id = _run_row(store, "implementation", "agent", provider_used="claude")
    _node_output(tmp_path, "implementation", run_id, "wired the parser")
    sup = _wired(tmp_path, FakeRouter(), store)

    (step,) = json.loads(sup._build_packet(_TASK, "T", []))["steps"]

    assert set(step) == {
        "node",
        "kind",
        "status",
        "outcome",
        "stage_attempts",
        "started_at",
        "finished_at",
        "provider_used",
        "message",
    }
    assert step["node"] == "implementation" and step["kind"] == "agent"
    assert step["status"] == "completed" and step["outcome"] == "done"
    assert step["started_at"] == "2026-01-01T00:00:00+00:00"
    assert step["finished_at"] == "2026-01-01T00:01:00+00:00"
    assert step["stage_attempts"] == 0  # a plain int, never normalized away to null


def test_packet_step_records_a_skip_and_a_subtask_boundary(tmp_path: Path) -> None:
    # A skipped node reaches the packet (its row is in `node_runs`) but is never observed, so these
    # keys are the only record that a branch was not taken.
    store = _store(tmp_path)
    store.record_node_skip(_TASK, "refinement", "agent", reason="when false", subtask_order=None)
    _run_row(store, "implementation", "agent", subtask_order=2)
    sup = _wired(tmp_path, FakeRouter(), store)

    skipped, subtask = json.loads(sup._build_packet(_TASK, "T", []))["steps"]

    assert skipped["skipped"] is True and skipped["skip_reason"] == "when false"
    assert subtask["subtask"] == 2
    assert "skipped" not in subtask and "subtask" not in skipped


def test_packet_omits_a_whitespace_only_step_message(tmp_path: Path) -> None:
    # The node-output writer only skips falsy content, so a blank-but-present `.out.md` is real. An
    # empty `message` would be a fact the node never stated.
    store = _store(tmp_path)
    run_id = _run_row(store, "implementation", "agent")
    _node_output(tmp_path, "implementation", run_id, "  \n ")
    sup = _wired(tmp_path, FakeRouter(), store)

    (step,) = json.loads(sup._build_packet(_TASK, "T", []))["steps"]

    assert "message" not in step


def test_packet_names_the_latest_evaluator_findings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_evaluation(
        EvaluationRow(
            task_id=_TASK,
            node_id="review",
            source_node_run_id=12,
            kind="in_flow_verdict",
            verdict="accept",
            findings_json="[]",
        )
    )
    findings = tmp_path / "repo" / ".worc-io" / _TASK / "stages" / "review" / "run-000012"
    findings.mkdir(parents=True, exist_ok=True)
    (findings / "findings.json").write_text('{"findings": []}\n', encoding="utf-8")
    sup = _wired(tmp_path, FakeRouter(), store)

    packet = json.loads(sup._build_packet(_TASK, "T", store.get_evaluations(_TASK)))

    assert packet["findings_path"] == f".worc-io/{_TASK}/stages/review/run-000012/findings.json"


def test_packet_publication_redacts_and_keeps_a_private_copy(tmp_path: Path) -> None:
    # Publication goes through the exchange seam, which redacts on the way in, so the packet
    # needs no redaction mechanism of its own — only the per-attempt secret literals (P0-D6).
    store = _store(tmp_path)
    run_id = _run_row(store, "implementation", "agent")
    _node_output(tmp_path, "implementation", run_id, "used token hunter2-secret to call the API")
    sup = _wired(tmp_path, FakeRouter([_ok()]), store, prompt_secrets=("hunter2-secret",))

    sup.finalize(task_id=_TASK, task_title="T")

    published = (tmp_path / "repo" / ".worc-io" / _TASK / "supervisor" / "packet.json").read_text(
        "utf-8"
    )
    assert "hunter2-secret" not in published
    private = node_run_dir(tmp_path / "art", _TASK, "supervisor", 0) / "packet.json"
    assert private.is_file()  # the unredacted authoritative copy stays in the audit dir


def test_packet_carries_the_flow_and_task_header(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup = _wired(tmp_path, FakeRouter(), store, flow_name="blog_article_revise", task_type="blog")

    packet = json.loads(sup._build_packet(_TASK, "Rewrite the intro", []))

    assert packet["task"] == {"id": _TASK, "title": "Rewrite the intro", "type": "blog"}
    assert packet["flow"] == {"name": "blog_article_revise"}


def test_finalize_failed_does_not_clobber_existing_summary_json(tmp_path: Path) -> None:
    # A failed finalize (no provider result) must NOT overwrite an existing non-empty
    # summary.json with a blank one (symmetric to leaving summary.md untouched on failure).
    store = _store(tmp_path)
    # First finalize succeeds and writes a non-empty summary.json.
    real = _prose("Real summary.")
    sup_ok = _supervisor(tmp_path, FakeRouter([_ok("s1", real)]), store)
    sup_ok.finalize(task_id=_TASK, task_title="T")
    summary_json = Path(task_artifact_dir(tmp_path / "art", _TASK)) / "summary.json"
    assert json.loads(summary_json.read_text("utf-8"))["summary"] == real

    # A later finalize whose turn fails must not blank it.
    sup_fail = _supervisor(tmp_path, FakeRouter([None]), store)
    sup_fail.finalize(task_id=_TASK, task_title="T")
    assert json.loads(summary_json.read_text("utf-8"))["summary"] == real


def test_schema_turn_caps_max_reasoning_but_free_text_keeps_it(tmp_path: Path) -> None:
    # A structured-output turn is capped to `high` when configured at a max tier (xhigh/max) so
    # the schema turn is not fragile; a free-text turn keeps the configured tier.
    schema_router, store = FakeRouter([_structured(_prose("Sum."), {})]), _store(tmp_path)
    sup_schema = _supervisor(tmp_path, schema_router, store, reasoning="xhigh")
    sup_schema.finalize(task_id=_TASK, task_title="T", emit_delta=True)  # schema turn
    assert schema_router.requests[0].reasoning == "high"

    free_dir = tmp_path / "b"
    free_dir.mkdir()
    free_router = FakeRouter([_ok("s", _prose("plain"))])
    sup_free = _supervisor(free_dir, free_router, _store(free_dir), reasoning="xhigh")
    sup_free.finalize(task_id=_TASK, task_title="T")  # free-text turn (no delta/follow-ups)
    assert free_router.requests[0].reasoning == "xhigh"


def test_observe_and_finalize_use_their_own_model_and_reasoning(tmp_path: Path) -> None:
    # The point of the split: a cheap note and the whole-task synthesis are configured separately,
    # so
    # an operator can make the notes cheap without weakening the summary that becomes the PR body.
    router, store = (
        FakeRouter([_ok("s", "note"), _ok("s", _prose("# Summary\n\nDone."))]),
        _store(tmp_path),
    )
    sup = _supervisor(
        tmp_path,
        router,
        store,
        observe=SupervisorObserveConfig(model="haiku", reasoning="low"),
        finalize=SupervisorTurnConfig(model="opus", reasoning="high"),
    )

    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=1, outcome_kind="rework")
    sup.finalize(task_id=_TASK, task_title="T")

    observe_request, finalize_request = router.requests
    assert (observe_request.model, observe_request.reasoning) == ("haiku", "low")
    assert (finalize_request.model, finalize_request.reasoning) == ("opus", "high")


def test_handoff_and_skill_proposal_run_independently_of_the_observation_cadence(
    tmp_path: Path,
) -> None:
    # Both are unaffected by `observe.mode`: they are not per-step observations. With observations
    # off
    # entirely they still run — and the skill proposal rides the cheap `observe` model/effort, since
    # it is a one-shot schema-bound turn rather than the whole-task synthesis.
    router, store = (
        FakeRouter(
            [
                _structured_handoff(
                    {"new_surface_area": "a", "locked_decisions": "", "open_edges": ""}
                ),
                _proposal_result([{"node": "implementation", "skills": ["safe-change"]}]),
            ]
        ),
        _store(tmp_path),
    )
    sup = _supervisor(
        tmp_path,
        router,
        store,
        observe=SupervisorObserveConfig(mode=ObserveMode.NONE, model="haiku", reasoning="low"),
        finalize=SupervisorTurnConfig(model="opus", reasoning="high"),
    )

    assert sup.handoff(task_id=_TASK, subtask_order=1, floor_context="floor") is not None
    assert sup.propose_skill_map(
        task_id=_TASK, agent_node_ids=["implementation"], inventory=_INV
    ) == {"implementation": ("safe-change",)}

    handoff_request, proposal_request = router.requests
    # `handoff` takes neither observe's nor finalize's pair — it has its own block (unset here).
    assert handoff_request.model is None
    assert (proposal_request.model, proposal_request.reasoning) == ("haiku", "low")


def test_observe_turn_caps_max_reasoning(tmp_path: Path) -> None:
    # Per-step observation is advisory and runs once per node-run, so a deep fix loop drives
    # many observe turns; it never needs a max tier — cap it to `high` (like a schema turn), while
    # the free-text finalize above keeps the configured tier.
    router, store = FakeRouter(), _store(tmp_path)
    sup = _supervisor(tmp_path, router, store, reasoning="xhigh")
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=1, outcome_kind="done")
    assert router.requests[0].reasoning == "high"


def _structured(summary: str, memory_delta: dict[str, Any]) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="claude",
        node_id="supervisor",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message="",
        session_id="s1",
        structured_output={"summary": summary, "memory_delta": memory_delta},
    )


def test_finalize_emits_delta_on_the_same_turn(tmp_path: Path) -> None:
    # With memory enabled, the SAME finalize turn yields the summary + the candidate delta.
    memory_delta = {
        "lessons": [
            {
                "kind": "semantic",
                "subject": "cfg",
                "statement": "bump docs",
                "evidence": [{"type": "repo_doc", "ref": "CLAUDE.md"}],
            }
        ]
    }
    router = FakeRouter([_structured(_prose("The summary."), memory_delta)])
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    result = sup.finalize(task_id=_TASK, task_title="T", emit_delta=True)
    assert len(router.requests) == 1  # exactly one turn — zero extra LLM calls
    assert result.summary_path is not None
    assert "The summary." in result.summary_path.read_text("utf-8")
    assert result.candidate_delta is not None
    assert result.candidate_delta.lessons[0].subject == "cfg"


def test_finalize_call_count_identical_with_memory_on_or_off(tmp_path: Path) -> None:
    # Enabling memory adds no provider turns.
    counts: list[int] = []
    cases = (
        ("off", FakeRouter([_ok("s", _prose("sum"))])),
        ("on", FakeRouter([_structured(_prose("sum"), {})])),
    )
    for name, router in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        sup = _supervisor(case_dir, router, _store(case_dir))
        sup.finalize(task_id=_TASK, task_title="T", emit_delta=(name == "on"))
        counts.append(len(router.requests))
    assert counts == [1, 1]


def test_finalize_malformed_delta_still_writes_summary(tmp_path: Path) -> None:
    # Best-effort: an unusable memory_delta never blocks the summary / publish.
    router = FakeRouter([_structured(_prose("Good summary."), {"lessons": "not-a-list"})])
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    result = sup.finalize(task_id=_TASK, task_title="T", emit_delta=True)
    assert result.summary_path is not None  # summary still written
    assert result.candidate_delta is None  # unusable delta -> None, never an exception


# -- Cluster B: flow-local supervisor prompts + emit_follow_ups ----------------


def _flow_lens(tmp_path: Path, name: str, body: str) -> str:
    """Write a flow-owned supervisor prompt file under flow_dir and return its relative path."""
    (tmp_path / "flow").mkdir(exist_ok=True)
    (tmp_path / "flow" / name).write_text(body, "utf-8")
    return f"flow/{name}"


def test_observe_lens_fallback_flow_then_config_then_builtin(tmp_path: Path) -> None:
    # Observe lens resolves flow role_file -> config.supervisor.role_file -> built-in (3 steps).
    flow_rf = _flow_lens(tmp_path, "supervisor.md", "FLOWLENS {task_id}")
    # (a) flow file present -> flow lens
    router = FakeRouter()
    sup = _supervisor(
        tmp_path, router, _store(tmp_path), flow_supervisor=SupervisorBlock(role_file=flow_rf)
    )
    sup.observe(task_id=_TASK, node_id="n", node_run_id=1, outcome_kind="done")
    assert "FLOWLENS" in router.requests[0].prompt

    # (b) flow file missing -> config lens (roles/supervisor.md, written by _supervisor)
    router = FakeRouter()
    sup = _supervisor(
        tmp_path,
        router,
        _store(tmp_path),
        flow_supervisor=SupervisorBlock(role_file="flow/nope.md"),
    )
    sup.observe(task_id=_TASK, node_id="n", node_run_id=1, outcome_kind="done")
    assert "Observe" in router.requests[0].prompt  # roles/supervisor.md content

    # (c) no flow block and a missing config file -> built-in
    router = FakeRouter()
    sup = Supervisor(
        settings=SupervisorConfig(role_file="roles/does-not-exist.md"),
        router=router,
        store=_store(tmp_path),
        repo_dir="/repo",
        artifacts_root=str(tmp_path / "art2"),
        flow_dir=tmp_path,
    )
    sup.observe(task_id=_TASK, node_id="n", node_run_id=1, outcome_kind="done")
    assert "read-only supervisor observing" in router.requests[0].prompt


def test_finalize_lens_fallback_flow_then_builtin(tmp_path: Path) -> None:
    # Finalize lens resolves flow finalize_role_file -> built-in (2 steps; no global counterpart).
    fin_rf = _flow_lens(tmp_path, "summary.md", "FINALIZE-EMPHASIS {task_id}")
    router = FakeRouter([_ok("s", _prose("sum"))])
    sup = _supervisor(
        tmp_path,
        router,
        _store(tmp_path),
        flow_supervisor=SupervisorBlock(finalize_role_file=fin_rf),
    )
    sup.finalize(task_id=_TASK, task_title="T")
    assert "FINALIZE-EMPHASIS" in router.requests[0].prompt

    # No finalize file -> built-in finalize emphasis.
    router = FakeRouter([_ok("s", _prose("sum"))])
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    sup.finalize(task_id=_TASK, task_title="T")
    assert "closing out a software task" in router.requests[0].prompt


def test_finalize_free_text_when_no_follow_ups_no_delta(tmp_path: Path) -> None:
    # Absent/false emit_follow_ups and memory off -> the finalize turn stays free-text (no
    # output_schema forced), exactly today's behavior.
    router = FakeRouter([_ok("s", _prose("plain summary"))])
    sup = _supervisor(tmp_path, router, _store(tmp_path))  # no flow block => emit_follow_ups False
    result = sup.finalize(task_id=_TASK, task_title="T", emit_delta=False)
    assert router.requests[0].output_schema is None
    assert result.follow_ups == ()
    assert "plain summary" in result.summary_path.read_text("utf-8")  # type: ignore[union-attr]


def _structured_follow_ups(summary: str, follow_ups: list[dict[str, Any]]) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="claude",
        node_id="supervisor",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message="",
        session_id="s1",
        structured_output={"summary": summary, "follow_ups": follow_ups},
    )


def test_emit_follow_ups_writes_json_and_summary_section(tmp_path: Path) -> None:
    # A flow that opts in: the SAME finalize turn yields {summary, follow_ups}; evidence-gated
    # records land in summary.json and a "Technical debt / follow-ups" section in summary.md.
    follow_ups = [
        {
            "title": "Extract the router",
            "rationale": "resolve_route is doing too much",
            "paths": ["src/routing/router.py"],
            "evidence": ["router.py:120 mixes fallback + retry"],
            "severity": "medium",
            "action_hint": "split retry into its own unit",
        },
        {"title": "ungrounded idea", "rationale": "no evidence", "evidence": [], "severity": "low"},
    ]
    router = FakeRouter([_structured_follow_ups(_prose("The summary."), follow_ups)])
    sup = _supervisor(
        tmp_path,
        router,
        _store(tmp_path),
        flow_supervisor=SupervisorBlock(emit_follow_ups=True),
    )
    result = sup.finalize(task_id=_TASK, task_title="T")

    assert len(router.requests) == 1  # one turn — no extra LLM call
    assert router.requests[0].output_schema is not None  # structured turn
    assert len(result.follow_ups) == 1  # the evidence-less record was dropped
    assert result.follow_ups[0].title == "Extract the router"

    md = result.summary_path.read_text("utf-8")  # type: ignore[union-attr]
    assert "## Technical debt / follow-ups" in md
    assert "Extract the router" in md and "ungrounded idea" not in md

    summary_json = json.loads((result.summary_path.with_name("summary.json")).read_text("utf-8"))  # type: ignore[union-attr]
    assert len(summary_json["follow_ups"]) == 1
    assert summary_json["follow_ups"][0]["evidence"] == ["router.py:120 mixes fallback + retry"]


def test_emit_follow_ups_malformed_still_writes_summary(tmp_path: Path) -> None:
    # Best-effort: a malformed follow_ups payload never blocks the summary.
    router = FakeRouter([_structured_follow_ups(_prose("Good summary."), "not-a-list")])  # type: ignore[arg-type]
    sup = _supervisor(
        tmp_path,
        router,
        _store(tmp_path),
        flow_supervisor=SupervisorBlock(emit_follow_ups=True),
    )
    result = sup.finalize(task_id=_TASK, task_title="T")
    assert result.summary_path is not None and result.follow_ups == ()
    assert "## Technical debt" not in result.summary_path.read_text("utf-8")


def test_finalize_prompt_and_schema_agree_that_follow_ups_is_mandatory(tmp_path: Path) -> None:
    # The schema requires `follow_ups` (OpenAI strict mode requires every property in `required`)
    # while the prompt said "leave the array empty" — so a model with nothing to report omitted the
    # key and was rejected three times. Both surfaces must say the same thing, and the schema has to
    # carry the contract itself, because the rejection message never explains the fix.
    router = FakeRouter([_structured_follow_ups(_prose("S."), [])])
    sup = _supervisor(
        tmp_path, router, _store(tmp_path), flow_supervisor=SupervisorBlock(emit_follow_ups=True)
    )
    sup.finalize(task_id=_TASK, task_title="T")

    assert "Always emit the `follow_ups` key" in router.requests[0].prompt
    description = router.requests[0].output_schema["properties"]["follow_ups"]["description"]
    assert "ALWAYS present" in description and "empty array" in description


def test_finalize_prompt_forbids_restating_the_gate_findings(tmp_path: Path) -> None:
    # Both sources land in one list and the merge dedups on exact text, so a paraphrase of an
    # accepted finding survives as a second bullet (measured: 10 bullets for ~6 issues, two pairs
    # disagreeing on severity). The turn is told not to restate them — but only when it was actually
    # shown the gate verdicts, so the instruction never references an absent section.
    store = _store(tmp_path)
    store.record_evaluation(_verdict([{"severity": "low", "reason": "a nit", "gating": False}]))
    with_gates = FakeRouter([_structured_follow_ups(_prose("S."), [])])
    sup = _supervisor(
        tmp_path, with_gates, store, flow_supervisor=SupervisorBlock(emit_follow_ups=True)
    )
    sup.finalize(task_id=_TASK, task_title="T")
    assert "do **not** restate the evaluator findings" in with_gates.requests[0].prompt.lower()

    no_gates_dir = tmp_path / "b"
    no_gates_dir.mkdir()
    no_gates = FakeRouter([_structured_follow_ups(_prose("S."), [])])
    sup_no_gates = _supervisor(
        no_gates_dir,
        no_gates,
        _store(no_gates_dir),
        flow_supervisor=SupervisorBlock(emit_follow_ups=True),
    )
    sup_no_gates.finalize(task_id=_TASK, task_title="T")
    assert "restate the evaluator findings" not in no_gates.requests[0].prompt.lower()


# -- surface sub-threshold evaluator findings ---------------------------------


def _verdict(
    findings: list[dict[str, Any]],
    *,
    verdict: str = "accept",
    node_id: str = "review",
    subtask_order: int | None = None,
):
    return EvaluationRow(
        task_id=_TASK,
        kind="in_flow_verdict",
        verdict=verdict,
        findings_json=json.dumps(findings),
        node_id=node_id,
        subtask_order=subtask_order,
    )


def test_finalize_surfaces_accepted_evaluator_findings(tmp_path: Path) -> None:
    # A sub-threshold finding an evaluator accepted reaches summary.json + the PR body even
    # when the flow did NOT opt into supervisor-authored follow-ups.
    store = _store(tmp_path)
    store.record_evaluation(
        _verdict(
            [
                {
                    "severity": "medium",
                    "reason": "`.worc/` in .prettierignore never matches anything",
                    "paths": [".prettierignore"],
                }
            ]
        )
    )
    router = FakeRouter([_ok("s1", _prose("Implemented the change."))])
    sup = _supervisor(tmp_path, router, store)  # no emit_follow_ups opt-in
    result = sup.finalize(task_id=_TASK, task_title="T")

    assert len(result.follow_ups) == 1
    assert result.follow_ups[0].severity == "medium"
    assert ".prettierignore" in result.follow_ups[0].paths
    md = result.summary_path.read_text("utf-8")  # type: ignore[union-attr]
    assert "## Technical debt / follow-ups" in md
    assert "never matches anything" in md
    summary_json = json.loads(result.summary_path.with_name("summary.json").read_text("utf-8"))  # type: ignore[union-attr]
    assert len(summary_json["follow_ups"]) == 1
    assert summary_json["follow_ups"][0]["severity"] == "medium"


def test_finalize_dedups_evaluator_findings_against_supervisor(tmp_path: Path) -> None:
    # When the supervisor already reported the same item, the evaluator finding is not
    # duplicated in the operator surface (exact-match dedup).
    store = _store(tmp_path)
    reason = "resolve_route mixes fallback and retry"
    store.record_evaluation(
        _verdict([{"severity": "medium", "reason": reason, "paths": ["src/routing/router.py"]}])
    )
    supervisor_follow_ups = [
        {
            "title": reason,
            "rationale": "",
            "paths": ["src/routing/router.py"],
            "evidence": ["router.py:120 mixes fallback + retry"],
            "severity": "medium",
        }
    ]
    router = FakeRouter([_structured_follow_ups(_prose("Summary."), supervisor_follow_ups)])
    sup = _supervisor(
        tmp_path, router, store, flow_supervisor=SupervisorBlock(emit_follow_ups=True)
    )
    result = sup.finalize(task_id=_TASK, task_title="T")

    assert len(result.follow_ups) == 1  # the evaluator duplicate merged away
    md = result.summary_path.read_text("utf-8")  # type: ignore[union-attr]
    assert md.count(reason) == 1


def test_finalize_prompt_carries_the_recorded_gate_verdicts(tmp_path: Path) -> None:
    # The finalize turn described the gates from session memory and wrote "three
    # independent verification gates … all of which passed" while four critic findings sat in
    # state.db. The recorded verdicts now ride the prompt, so "passed" is not writable about a gate
    # that emitted findings.
    store = _store(tmp_path)
    store.record_evaluation(_verdict([], node_id="fact_verification"))
    store.record_evaluation(
        _verdict(
            [{"severity": "medium", "reason": "uneven audit depth", "paths": ["report.md"]}],
            node_id="critical_review",
        )
    )
    router = FakeRouter([_ok("s1", _prose("Summary."))])
    sup = _supervisor(tmp_path, router, store)
    sup.finalize(task_id=_TASK, task_title="T")

    prompt = router.requests[-1].prompt
    assert "## Gate verdicts recorded for this task" in prompt
    assert "- fact_verification: verdict `accept`, no findings recorded" in prompt
    assert "- critical_review: verdict `accept`, 1 finding(s) recorded:" in prompt
    assert "  - [medium] uneven audit depth (report.md)" in prompt
    assert "did **not** simply pass" in prompt  # the instruction that makes it binding


def test_finalize_gate_section_absent_when_the_flow_has_no_evaluator(tmp_path: Path) -> None:
    # A flow with no in-flow evaluator gets no empty heading — the section is simply absent.
    router, store = FakeRouter([_ok("s1", _prose("Summary."))]), _store(tmp_path)
    _record_step(store, 1, node="implementation", outcome="done", note="wired it")
    sup = _supervisor(tmp_path, router, store)
    sup.finalize(task_id=_TASK, task_title="T")
    assert "Gate verdicts" not in router.requests[-1].prompt


def test_finalize_gate_digest_keeps_only_each_nodes_final_verdict(tmp_path: Path) -> None:
    # A rework round is superseded, exactly as the follow-up derivation treats it: the operator sees
    # what the gate concluded, not every intermediate round it spent.
    store = _store(tmp_path)
    store.record_evaluation(
        _verdict(
            [{"severity": "medium", "reason": "round one", "paths": []}],
            verdict="rework",
            node_id="critical_review",
        )
    )
    store.record_evaluation(_verdict([], node_id="critical_review"))
    router = FakeRouter([_ok("s1", _prose("Summary."))])
    sup = _supervisor(tmp_path, router, store)
    sup.finalize(task_id=_TASK, task_title="T")

    prompt = router.requests[-1].prompt
    assert "- critical_review: verdict `accept`, no findings recorded" in prompt
    assert "round one" not in prompt


def test_finalize_no_findings_leaves_no_section(tmp_path: Path) -> None:
    # No evaluator findings and no supervisor follow-ups → no empty heading (unchanged behavior).
    store = _store(tmp_path)
    store.record_evaluation(_verdict([]))  # a clean accept
    router = FakeRouter([_ok("s1", _prose("Clean summary."))])
    sup = _supervisor(tmp_path, router, store)
    result = sup.finalize(task_id=_TASK, task_title="T")
    assert result.follow_ups == ()
    assert "## Technical debt" not in result.summary_path.read_text("utf-8")  # type: ignore[union-attr]


# -- subtask handoff brief -----------------------------------------------------


def _structured_handoff(sections: dict[str, Any]) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="claude",
        node_id="supervisor",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message="",
        session_id="s1",
        structured_output=sections,
    )


def test_handoff_emits_three_section_brief(tmp_path: Path) -> None:
    sections = {
        "new_surface_area": "the predecessor added foo()",
        "locked_decisions": "keep the JSON schema stable",
        "open_edges": "bar() is stubbed — do not wire it yet",
    }
    router = FakeRouter([_structured_handoff(sections)])
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    brief = sup.handoff(task_id=_TASK, subtask_order=2, floor_context="THE DETERMINISTIC FLOOR")

    assert brief is not None
    assert "### New surface area" in brief and "foo()" in brief
    assert "### Locked decisions" in brief and "### Open edges" in brief
    assert "THE DETERMINISTIC FLOOR" in router.requests[0].prompt  # floor fed into the prompt
    assert router.requests[0].output_schema is not None  # structured turn


def test_handoff_best_effort_none_on_failure_or_free_text(tmp_path: Path) -> None:
    # No provider result → None (the orchestrator ships the floor alone); a free-text result with no
    # structured output → None. Never raises.
    none_router = _supervisor(tmp_path, FakeRouter([None]), _store(tmp_path))
    assert none_router.handoff(task_id=_TASK, subtask_order=2, floor_context="F") is None
    free = _supervisor(tmp_path, FakeRouter([_ok("s", _prose("prose"))]), _store(tmp_path))
    assert free.handoff(task_id=_TASK, subtask_order=2, floor_context="F") is None


def test_handoff_empty_sections_yield_none(tmp_path: Path) -> None:
    router = FakeRouter([_structured_handoff({"new_surface_area": "  ", "open_edges": ""})])
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    assert sup.handoff(task_id=_TASK, subtask_order=2, floor_context="F") is None


def test_handoff_uses_distinct_run_id_per_subtask(tmp_path: Path) -> None:
    # Each subtask boundary's handoff must namespace its artifact dir by subtask_order — a shared
    # node_run_id would make the second handoff's create_attempt_dir (exist_ok=False) raise and
    # silently degrade every boundary after the first to the floor alone.
    router = FakeRouter(
        [_structured_handoff({"new_surface_area": "a"}), _structured_handoff({"open_edges": "b"})]
    )
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    sup.handoff(task_id=_TASK, subtask_order=2, floor_context="F")
    sup.handoff(task_id=_TASK, subtask_order=3, floor_context="F")
    run_ids = [r.node_run_id for r in router.requests]
    assert run_ids[0] != run_ids[1]  # distinct dirs → no create_attempt_dir collision


def test_handoff_records_subtask_in_prompt_audit(tmp_path: Path) -> None:
    registered: list[Any] = []
    router = FakeRouter([_structured_handoff({"new_surface_area": "a"})])
    sup = _supervisor(
        tmp_path,
        router,
        _store(tmp_path),
        register_artifact=lambda t, k, p: registered.append((t, k, p)),
        prompt_audit=True,
    )
    sup.handoff(task_id=_TASK, subtask_order=2, floor_context="F")

    audit_dir = task_artifact_dir(str(tmp_path / "art"), _TASK) / "prompt-audit"
    step_path = audit_dir / f"{_HANDOFF_RUN_ID_BASE + 2:06d}-supervisor-sub02.json"
    assert step_path.exists()
    record = json.loads(step_path.read_text("utf-8"))
    assert record["subtask"] == 2


def test_handoff_uses_flow_handoff_role_file(tmp_path: Path) -> None:
    rf = _flow_lens(tmp_path, "handoff.md", "HANDOFF-LENS {task_id}")
    router = FakeRouter([_structured_handoff({"new_surface_area": "x"})])
    sup = _supervisor(
        tmp_path, router, _store(tmp_path), flow_supervisor=SupervisorBlock(handoff_role_file=rf)
    )
    sup.handoff(task_id=_TASK, subtask_order=2, floor_context="F")
    assert "HANDOFF-LENS" in router.requests[0].prompt


# -- config (validated under the node ceiling) --------------------------------


def _config_with_supervisor(packaged_config_text: str, block: str) -> Any:
    return loads_config(packaged_config_text + "\n" + block).config


def _without_supervisor_section(text: str) -> str:
    """Drop the top-level ``supervisor:`` block (header + its indented body) from a config YAML.

    The packaged example config ships a *populated* supervisor section, so this yields the
    genuinely-absent case — exercising the loader's ``SupervisorConfig()`` default path.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() and not line[0].isspace() and line.split(":", 1)[0].strip() == "supervisor":
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i][0].isspace()):
                i += 1  # skip the block's indented body and any trailing blank line
            continue
        out.append(line)
        i += 1
    return "".join(out)


def test_supervisor_config_from_config_yaml(packaged_config_text: str) -> None:
    # Model and effort are per phase; `role_file` and `provider` stay one-per-layer at the top.
    block = (
        "supervisor:\n"
        "  role_file: roles/supervisor.md\n"
        "  observe:\n    model: sonnet\n    reasoning: low\n"
        "  finalize:\n    model: opus\n    reasoning: high\n"
        "  handoff:\n    reasoning: medium\n"
    )
    config = _config_with_supervisor(packaged_config_text, block)
    assert (config.supervisor.observe.model, config.supervisor.observe.reasoning) == (
        "sonnet",
        "low",
    )
    assert (config.supervisor.finalize.model, config.supervisor.finalize.reasoning) == (
        "opus",
        "high",
    )
    assert config.supervisor.handoff.reasoning == "medium"
    assert config.supervisor.handoff.model is None  # absent key => the provider default
    assert config.supervisor.role_file == "roles/supervisor.md"
    assert config.supervisor.provider is None  # absent key => inherit the global primary
    validate_config(config)  # passes the ceiling (read-only forced in code, allowlist, containment)


def test_supervisor_observe_cadence_from_config_yaml(packaged_config_text: str) -> None:
    block = (
        "supervisor:\n"
        "  observe:\n"
        "    mode: selected\n"
        "    triggers: [failure]\n"
        "    include_nodes: [implementation, review]\n"
    )
    observe = _config_with_supervisor(packaged_config_text, block).supervisor.observe
    assert observe.mode is ObserveMode.SELECTED
    assert observe.triggers == ("failure",)
    assert observe.include_nodes == ("implementation", "review")


def test_supervisor_observe_mode_defaults_to_events(packaged_config_text: str) -> None:
    # The global default is the saving: a flow that declares no cadence of its own — including every
    # user-authored one — pays for deviations only, not for `all`.
    observe = _config_with_supervisor(packaged_config_text, "supervisor:\n  role_file: r.md\n")
    assert observe.supervisor.observe.mode is ObserveMode.EVENTS
    assert SupervisorObserveConfig().mode is ObserveMode.EVENTS


def test_supervisor_config_provider_parsed(packaged_config_text: str) -> None:
    block = "supervisor:\n  provider: codex\n  finalize:\n    model: gpt-5.4\n"
    config = _config_with_supervisor(packaged_config_text, block)
    assert config.supervisor.provider == ProviderId.CODEX


def test_supervisor_unknown_provider_rejected(packaged_config_text: str) -> None:
    with pytest.raises(ConfigError, match="provider"):
        _config_with_supervisor(packaged_config_text, "supervisor:\n  provider: gemini\n")


def test_supervisor_absent_section_defaults(packaged_config_text: str) -> None:
    config = loads_config(_without_supervisor_section(packaged_config_text)).config
    assert config.supervisor == SupervisorConfig()  # safe default when the section is absent
    validate_config(config)


def test_supervisor_bad_reasoning_rejected(packaged_config_text: str) -> None:
    with pytest.raises(ConfigError, match=r"supervisor\.observe\.reasoning"):
        _config_with_supervisor(
            packaged_config_text, "supervisor:\n  observe:\n    reasoning: turbo\n"
        )


def test_supervisor_unknown_observe_mode_rejected(packaged_config_text: str) -> None:
    with pytest.raises(ConfigError, match=r"supervisor\.observe\.mode"):
        _config_with_supervisor(
            packaged_config_text, "supervisor:\n  observe:\n    mode: sometimes\n"
        )


def test_supervisor_unknown_observe_trigger_rejected(packaged_config_text: str) -> None:
    # The trigger list is closed: a new trigger needs the facts that detect it, so an unknown name
    # is a typo, not an extension point.
    with pytest.raises(ConfigError, match="triggers"):
        _config_with_supervisor(
            packaged_config_text, "supervisor:\n  observe:\n    triggers: [hitl]\n"
        )


def test_supervisor_unknown_nested_key_rejected(packaged_config_text: str) -> None:
    with pytest.raises(ConfigError, match=r"supervisor\.finalize"):
        _config_with_supervisor(
            packaged_config_text, "supervisor:\n  finalize:\n    session: fresh\n"
        )


@pytest.mark.parametrize("key", ["model", "reasoning"])
def test_flat_supervisor_model_and_reasoning_are_rejected_by_name(
    packaged_config_text: str, key: str
) -> None:
    # v33 removed the flat pair. Rejected fail-closed rather than tolerated, and the message names
    # the two places the value can go — the operator has to choose, because copying one value into
    # both would put the expensive model back on the cheap per-step notes.
    with pytest.raises(ConfigError) as exc:
        _config_with_supervisor(packaged_config_text, f"supervisor:\n  {key}: sonnet\n")
    issue = next(i for i in exc.value.issues if f"supervisor.{key}" in i)
    assert f"supervisor.observe.{key}" in issue and f"supervisor.finalize.{key}" in issue
    assert "upgrade-config" in issue  # points at the command that strips it
    # Exactly one issue for the key: the named rejection, not also a vaguer "unknown key".
    assert len([i for i in exc.value.issues if f"supervisor.{key}" in i]) == 1


def test_supervisor_role_file_traversal_rejected(packaged_config_text: str) -> None:
    config = _config_with_supervisor(
        packaged_config_text, "supervisor:\n  role_file: ../escape.md\n"
    )
    with pytest.raises(ConfigError, match="role_file"):
        validate_config(config)


# -- evaluator primitive: single rework accounting + immutable counted verdicts ----------------


def test_record_rework_single_increment() -> None:
    rs = FlowRunState(flow_fingerprint="fp")
    assert record_rework(rs) == 1
    assert rs.fix_iterations == 1
    assert record_rework(rs) == 2
    assert rs.fix_iterations == 2
    # Only the single global fix counter is touched — no named-loop / edge double-count.
    assert set(rs.loop_counters) == {FlowRunState.GLOBAL_FIX_KEY}


def test_evaluation_immutable_and_counted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i, verdict in enumerate(("rework", "accept", "rework"), start=1):
        store.record_evaluation(
            EvaluationRow(
                task_id=_TASK,
                node_id="review",
                source_node_run_id=i,
                kind="in_flow_verdict",
                verdict=verdict,
                findings_json="[]",
            )
        )

    rows = store.get_evaluations(_TASK)
    assert len(rows) == 3  # append-only — every verdict is its own immutable row
    assert [r.verdict for r in rows] == ["rework", "accept", "rework"]
    # The per-instance rework limit is derived by COUNT, not a mutable counter.
    assert store.count_rework_verdicts(_TASK) == 2
    assert store.count_rework_verdicts(_TASK, node_id="review") == 2
    assert store.count_rework_verdicts(_TASK, node_id="other") == 0
    # Immutable: there is no update/delete API for evaluations.
    assert not hasattr(store, "update_evaluation")
    assert not hasattr(store, "delete_evaluation")


# -- provider-attempt audit for the supervisor layer --------------------------


class _AttemptsRouter:
    """Like ``FakeRouter`` but surfaces one provider ATTEMPT per call (the base returns none), so
    the supervisor's own ``provider_attempts`` recording has attempts to persist. Resolves to
    the given primary so a resumed cumulative (codex) session can be exercised."""

    def __init__(
        self, results: list[AgentRunResult], primary: ProviderId = ProviderId.CLAUDE
    ) -> None:
        self.requests: list[Any] = []
        self._results = list(results)
        self._primary = primary

    def resolve_route(self, node_id: str, provider: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            node_id=node_id, primary=self._primary, fallback=None, source=RouteSource.CONFIG
        )

    def route_grants_shell(
        self, route: ResolvedRoute, *, permission_profile: Any = None, git_evidence: bool = False
    ) -> bool:
        # The real Router asks the adapters whether this attempt gets a shell. The double answers
        # from the node's grant — a Claude-shaped answer — unless a test sets ``grants_shell`` to
        # model a provider whose profile carries a shell on its own (Codex ``read-only``) or a host
        # where it was dropped.
        override = getattr(self, "grants_shell", None)
        return git_evidence if override is None else bool(override)

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        self.requests.append(request)
        result = self._results.pop(0)
        pid = ProviderId(result.provider)
        attempt = ProviderAttempt(
            provider=pid, attempt=1, status=RunStatus.SUCCEEDED, error_class=None, result=result
        )
        return StageOutcome(
            route=route,
            result=result,
            provider_used=pid,
            stage_attempts=1,
            terminal_error=None,
            attempts=(attempt,),
        )


def _claude_turn(cost: float | None, output_total: int, message: str = "noted") -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="claude",
        node_id="supervisor",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message=message,
        session_id="sess-super",
        usage={"output_tokens": output_total},
        normalized_usage=NormalizedUsage(
            scope=UsageScope.PER_INVOCATION, output_total=output_total, cost=cost
        ),
    )


def _codex_turn(session_id: str, input_total: int, output_total: int) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="codex",
        node_id="supervisor",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        final_message="noted",
        session_id=session_id,
        usage={"input_tokens": input_total, "output_tokens": output_total},
        normalized_usage=NormalizedUsage(
            scope=UsageScope.SESSION_CUMULATIVE, input_total=input_total, output_total=output_total
        ),
    )


def test_supervisor_records_provider_attempt_with_cost(tmp_path: Path) -> None:
    # A supervisor turn's billable provider call earns a ``provider_attempts`` row with
    # ``node_run_id`` NULL and its cost, so a whole-task roll-up includes the supervisor spend.
    router = _AttemptsRouter([_claude_turn(cost=0.05, output_total=42)])
    store = _store(tmp_path)
    sup = _supervisor(tmp_path, router, store)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")

    rows = store.get_provider_attempts_for_task(_TASK)
    assert len(rows) == 1
    assert rows[0].node_run_id is None  # not a graph node
    assert rows[0].provider == "claude"
    assert rows[0].usage_cost == 0.05
    assert rows[0].usage_output_total == 42
    # The supervisor's synthetic artifact-namespacing id never leaks into the audit table: the
    # by-node getter for the observed step's id returns nothing.
    assert store.get_provider_attempts(5) == []


def test_supervisor_provider_attempt_usage_is_summation_safe_delta(tmp_path: Path) -> None:
    # The supervisor resumes its OWN session, so a cumulative (codex) provider counts
    # cumulatively; the recorded per-turn usage is the summation-safe delta, not the raw cumulative.
    router = _AttemptsRouter(
        [_codex_turn("s1", 100, 10), _codex_turn("s1", 150, 25)], primary=ProviderId.CODEX
    )
    store = _store(tmp_path)
    sup = _supervisor(tmp_path, router, store, provider=ProviderId.CODEX)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    sup.observe(task_id=_TASK, node_id="review", node_run_id=7, outcome_kind="accept")

    rows = store.get_provider_attempts_for_task(_TASK)
    assert len(rows) == 2
    assert all(r.node_run_id is None for r in rows)
    # First turn: fresh session, the cumulative is its own delta.
    assert rows[0].usage_output_total == 10
    # Second turn resumes s1, so the previous cumulative is subtracted: 25 - 10 = 15, not raw 25.
    assert rows[1].usage_output_total == 15
    assert rows[1].usage_input_total == 50
    assert rows[1].usage_delta_status == "ok"


def test_each_phase_labels_its_own_provider_calls(tmp_path: Path) -> None:
    # The phase cannot be inferred from the turn settings — the skill proposal deliberately shares
    # the observe phase's cheap model + effort — so each call site states which job it is.
    store = _store(tmp_path)
    router = _AttemptsRouter([_claude_turn(cost=0.01, output_total=1) for _ in range(4)])
    sup = _supervisor(tmp_path, router, store)
    sup.propose_skill_map(task_id=_TASK, agent_node_ids=["implementation"], inventory=_INV)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")
    sup.handoff(task_id=_TASK, subtask_order=1, floor_context="floor")
    sup.finalize(task_id=_TASK, task_title="T")

    labels = [r.supervisor_function for r in store.get_provider_attempts_for_task(_TASK)]
    assert labels == ["skill", "observe", "handoff", "finalize"]


def test_summary_json_reports_what_the_layer_spent_per_phase(tmp_path: Path) -> None:
    store = _store(tmp_path)
    router = _AttemptsRouter(
        [_claude_turn(cost=0.02, output_total=5), _claude_turn(cost=0.30, output_total=80)]
    )
    sup = _supervisor(tmp_path, router, store)
    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=5, outcome_kind="done")

    sup.finalize(task_id=_TASK, task_title="T")

    payload = json.loads(
        (Path(task_artifact_dir(tmp_path / "art", _TASK)) / "summary.json").read_text("utf-8")
    )
    usage = payload["supervisor_usage"]
    # Written after the finalize turn, so the most expensive call is in its own report.
    assert list(usage["by_function"]) == ["observe", "finalize"]
    assert usage["by_function"]["finalize"]["calls"] == 1
    assert usage["by_function"]["finalize"]["cost"] == 0.30
    assert usage["total"]["calls"] == 2
    assert usage["total"]["cost"] == 0.32


def test_summary_json_omits_the_spend_report_when_the_layer_made_no_calls(tmp_path: Path) -> None:
    # A cadence of `none` on a run whose finalize could not start: no row, so no report — rather
    # than a block of zeros that would read as "the layer ran and was free".
    store = _store(tmp_path)
    sup = _supervisor(tmp_path, FakeRouter([None]), store)

    sup.finalize(task_id=_TASK, task_title="T")

    payload = json.loads(
        (Path(task_artifact_dir(tmp_path / "art", _TASK)) / "summary.json").read_text("utf-8")
    )
    assert "supervisor_usage" not in payload


def test_summary_md_carries_no_spend_telemetry(tmp_path: Path) -> None:
    # summary.md becomes the pull-request body: the spend is the operator's, not the reviewer's, and
    # it must not travel to the remote with the change.
    store = _store(tmp_path)
    sup = _supervisor(
        tmp_path,
        _AttemptsRouter([_claude_turn(cost=0.30, output_total=80, message=_prose("Done."))]),
        store,
    )

    result = sup.finalize(task_id=_TASK, task_title="T")

    assert result.summary_path is not None
    body = result.summary_path.read_text("utf-8")
    for token in ("supervisor_usage", "cost", "duration_seconds", "0.30"):
        assert token not in body


# --- The layer's own attempt is bracketed like a graph node's (Пре1-2 / Пре3-9) ------------------


class _FakeSupervisorGit:
    """The three GitPort methods the layer's bracket uses; records what it was asked."""

    def __init__(self, *, drift: object | None = None) -> None:
        self.captures = 0
        self.resolved: list[str | None] = []
        self._drift = drift

    def resolve_control_paths(self, exchange_root: str | None = None) -> object:
        self.resolved.append(exchange_root)
        return ProviderWriteGuardPolicy(
            exchange_root=None,
            git_dir=Path("/repo/.git"),
            git_common_dir=Path("/repo/.git"),
            hooks_dir=Path("/repo/.git/hooks"),
            tasks_dir=Path("/repo/tasks"),
        )

    def capture_git_control_state(self) -> object:
        self.captures += 1
        return object()

    def compare_git_control_state(self, before: object) -> object | None:
        return self._drift


def test_a_shell_bearing_supervisor_turn_carries_the_write_deny_roots(tmp_path: Path) -> None:
    # П4.2 / Пре1-2: the layer is read-only by mandate, but the mandate is not a mechanism — Codex
    # runs commands on `read-only`, and in the advanced mode so does Claude. With the write guard on
    # the request the provider's pre-launch canary re-proves the `.git`/`.worc` denies around this
    # attempt too, which is what makes floor 1's "before every provider attempt" literally true.
    store = _store(tmp_path)
    router = FakeRouter([_ok("s", "note")])
    router.grants_shell = True
    git = _FakeSupervisorGit()
    sup = _supervisor(tmp_path, router, store, git=git, exchange_root=str(tmp_path / "io"))

    sup.observe(task_id=_TASK, node_id="n", node_run_id=1, outcome_kind="done")

    assert router.requests[0].write_guard is not None
    assert git.resolved == [str(tmp_path / "io")]
    assert git.captures == 1


def test_a_supervisor_turn_without_a_shell_is_not_bracketed(tmp_path: Path) -> None:
    # No attempt pays for a check that cannot apply to it: a Claude turn with no shell can neither
    # write nor run commands, so neither the guard nor the fingerprint is resolved.
    store = _store(tmp_path)
    router = FakeRouter([_ok("s", "note")])
    router.grants_shell = False
    git = _FakeSupervisorGit()
    sup = _supervisor(tmp_path, router, store, git=git)

    sup.observe(task_id=_TASK, node_id="n", node_run_id=1, outcome_kind="done")

    assert router.requests[0].write_guard is None
    assert git.captures == 0
    assert git.resolved == []


def test_git_control_drift_across_a_supervisor_turn_warns_and_the_run_continues(
    tmp_path: Path, package_log_text: Callable[[], str]
) -> None:
    # The advisory contract decides the verdict: the layer can flag but cannot rework, so drift here
    # is the same loud line every non-writing node class gets — never a park, which would let an
    # advisory layer stop a reviewed, passing change.
    store = _store(tmp_path)
    router = FakeRouter([_ok("s", "note")])
    router.grants_shell = True
    drift = GitControlDrift((GitControlDriftItem("hooks", "hook 'pre-push' added"),))
    sup = _supervisor(tmp_path, router, store, git=_FakeSupervisorGit(drift=drift))

    sup.observe(task_id=_TASK, node_id="n", node_run_id=1, outcome_kind="done")

    log = package_log_text()
    assert "git control state changed during a supervisor turn" in log
    assert "hook 'pre-push' added" in log
