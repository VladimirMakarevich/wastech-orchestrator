"""Unit tests for the constant supervisor layer + evaluator primitive (flow-engine P2.1).

The supervisor is the orchestrator-level oversight layer above any flow: per-step read-only
observation in its own resume_own_lineage session, advisory-only (never reworks/routes), and a
single whole-task summary at close. The evaluator primitive is the immutable ``evaluations`` table
plus the single ``record_rework`` accounting path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import SupervisorConfig
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.loop_control import record_rework
from wastech_orchestrator.core.skills import SkillInventory, SkillRef
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.core.supervisor import Supervisor
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import AgentRunResult, ProviderId, RunStatus
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome
from wastech_orchestrator.state_store import (
    EditingLineageRow,
    EvaluationRow,
    StateStore,
    TaskRow,
)

_TASK = "task-1"


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
        self._results = list(results) if results is not None else None

    def resolve_route(self, node_id: str, provider: Any = None) -> ResolvedRoute:
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
) -> Supervisor:
    (tmp_path / "roles").mkdir(exist_ok=True)
    (tmp_path / "roles" / "supervisor.md").write_text("Observe {task_id} in {repo}.", "utf-8")
    return Supervisor(
        settings=SupervisorConfig(
            role_file="roles/supervisor.md", model=model, reasoning=reasoning
        ),
        router=router,
        store=store,
        repo_dir="/repo",
        artifacts_root=str(tmp_path / "art"),
        flow_dir=tmp_path,
    )


# -- per-step observation -----------------------------------------------------


def test_supervisor_observes_each_completed_step(tmp_path: Path) -> None:
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
        task_spec_text="do x",
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
        task_spec_text="x",
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
        task_spec_text="x",
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
        EditingLineageRow(task_id=_TASK, provider="claude", raw_session_id="author-session")
    )
    sup = _supervisor(tmp_path, router, store)

    sup.observe(task_id=_TASK, node_id="implementation", node_run_id=1, outcome_kind="done")
    sup.observe(task_id=_TASK, node_id="fixing", node_run_id=2, outcome_kind="done")
    sup.finalize(task_id=_TASK, task_title="T")

    assert router.requests[0].session_id is None  # fresh own session
    assert router.requests[1].session_id == "sess-super"  # resumes its OWN session, not an author's
    assert all(r.permission_profile == "read-only" for r in router.requests)
    # The author's editing lineage is never read into the supervisor's session nor overwritten.
    row = store.get_editing_lineage(_TASK)
    assert row is not None and row.raw_session_id == "author-session"


# -- whole-task finalize ------------------------------------------------------


def test_supervisor_runs_above_any_flow_writes_summary(tmp_path: Path) -> None:
    # Independent of flow shape: finalize synthesizes and writes the summary (the PR body) + the
    # local summary.json, and records exactly one supervisor_final row.
    router, store = FakeRouter([_ok("s1", "The whole task summary.")]), _store(tmp_path)
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
    # AC-W1: with memory enabled, the SAME finalize turn yields the summary + the candidate delta.
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
    router = FakeRouter([_structured("The summary.", memory_delta)])
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    result = sup.finalize(task_id=_TASK, task_title="T", emit_delta=True)
    assert len(router.requests) == 1  # exactly one turn — zero extra LLM calls
    assert result.summary_path is not None
    assert "The summary." in result.summary_path.read_text("utf-8")
    assert result.candidate_delta is not None
    assert result.candidate_delta.lessons[0].subject == "cfg"


def test_finalize_call_count_identical_with_memory_on_or_off(tmp_path: Path) -> None:
    # AC-W1: enabling memory adds no provider turns.
    counts: list[int] = []
    cases = (("off", FakeRouter([_ok("s", "sum")])), ("on", FakeRouter([_structured("sum", {})])))
    for name, router in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        sup = _supervisor(case_dir, router, _store(case_dir))
        sup.finalize(task_id=_TASK, task_title="T", emit_delta=(name == "on"))
        counts.append(len(router.requests))
    assert counts == [1, 1]


def test_finalize_malformed_delta_still_writes_summary(tmp_path: Path) -> None:
    # Best-effort: an unusable memory_delta never blocks the summary / publish.
    router = FakeRouter([_structured("Good summary.", {"lessons": "not-a-list"})])
    sup = _supervisor(tmp_path, router, _store(tmp_path))
    result = sup.finalize(task_id=_TASK, task_title="T", emit_delta=True)
    assert result.summary_path is not None  # summary still written
    assert result.candidate_delta is None  # unusable delta -> None, never an exception


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
    block = "supervisor:\n  model: sonnet\n  reasoning: high\n  role_file: roles/supervisor.md\n"
    config = _config_with_supervisor(packaged_config_text, block)
    assert config.supervisor.model == "sonnet"
    assert config.supervisor.reasoning == "high"
    assert config.supervisor.role_file == "roles/supervisor.md"
    validate_config(config)  # passes the ceiling (read-only forced in code, allowlist, containment)


def test_supervisor_absent_section_defaults(packaged_config_text: str) -> None:
    config = loads_config(_without_supervisor_section(packaged_config_text)).config
    assert config.supervisor == SupervisorConfig()  # safe default when the section is absent
    validate_config(config)


def test_supervisor_bad_reasoning_rejected(packaged_config_text: str) -> None:
    with pytest.raises(ConfigError):
        _config_with_supervisor(packaged_config_text, "supervisor:\n  reasoning: turbo\n")


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
