"""Wiring builders (P1.4) — turn the orchestrator's collaborators + ``_Pipeline`` into the node
data bundles. The mapping is exercised with the parity fixture + a duck-typed pipeline so it stays
verifiable without standing up the whole orchestrator.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.wiring import (
    build_node_inputs,
    build_node_services,
    build_stage_map,
)
from wastech_orchestrator.providers.base import ProviderId, Stage

_PARITY = Path(__file__).parent / "flows" / "implementation_parity.yaml"


def test_build_stage_map_only_routed_nodes() -> None:
    # Agent + evaluator nodes get a routing Stage; checks/publish nodes are never routed and are
    # absent — even `testing`, whose id *is* a Stage value but whose kind is `checks`.
    snapshot = load_flow(_PARITY)
    assert build_stage_map(snapshot) == {
        "refinement": Stage.REFINEMENT,
        "planning": Stage.PLANNING,
        "implementation": Stage.IMPLEMENTATION,
        "review": Stage.REVIEW,
        "fixing": Stage.FIXING,
        "summary": Stage.SUMMARY,
    }


def test_build_node_services_sets_collaborators_and_map() -> None:
    snapshot = load_flow(_PARITY)
    router, checks, store, git = object(), object(), object(), object()
    services = build_node_services(
        router=router,  # type: ignore[arg-type]
        check_runner=checks,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        repo_dir="/repo",
        artifacts_root="/art",
        snapshot=snapshot,
        clock=lambda: "ts",
        git=git,  # type: ignore[arg-type]
        snapshot_hook=git,  # type: ignore[arg-type]
        default_timeout_seconds=123,
        ask_timeout_s=45,
    )
    assert services.router is router
    assert services.git is git
    assert services.snapshot is git  # the git manager is also the provider-observability hook
    assert services.repo_dir == "/repo"
    assert services.artifacts_root == "/art"
    assert services.default_timeout_seconds == 123
    assert services.ask_timeout_s == 45
    assert services.stage_for_node["implementation"] is Stage.IMPLEMENTATION


def _fake_pipeline(**over: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "task_file": "/t/task.md",
        "plan_path": "/a/plan.md",
        "diff_path": "/a/current.diff",
        "check_log": "/a/check.log",
        "review_findings_path": "/a/review/findings.json",
        "selected_skills": (SimpleNamespace(path="/skills/a/SKILL.md"),),
        "decomposition": SimpleNamespace(accepted=True, n=3),
        "branch": "agent/task-1-x",
        "task": SimpleNamespace(
            contacts=("@me",),
            model_for=lambda s: None,
            reasoning_for=lambda s: None,
            agents={Stage.REVIEW: ProviderId.CLAUDE},
        ),
        "session_ids": {"codex": "sess-1"},
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_build_node_inputs_maps_pipeline_paths(tmp_path: Path) -> None:
    p = _fake_pipeline()
    inputs = build_node_inputs(
        p,  # type: ignore[arg-type]
        flow_dir=tmp_path,
        resolved_checks=(),
        pr_title="My PR",
        summary_body_path="/s/summary.md",
        commit_message="feat: x",
    )
    assert inputs.flow_dir == tmp_path
    assert inputs.task_path == "/t/task.md"
    assert inputs.plan_path == "/a/plan.md"
    assert inputs.diff_path == "/a/current.diff"
    assert inputs.checks_path == "/a/check.log"  # p.check_log -> {checks_path}
    assert inputs.review_path == "/a/review/findings.json"
    assert inputs.skill_paths == ("/skills/a/SKILL.md",)
    assert inputs.subtask_count == 3  # decomposition accepted -> n surfaced
    assert inputs.branch == "agent/task-1-x"
    assert inputs.pr_title == "My PR"
    assert inputs.summary_body_path == "/s/summary.md"
    assert inputs.commit_message == "feat: x"
    assert inputs.contacts == ("@me",)
    assert inputs.session_ids is p.session_ids  # shared by reference for session continuity
    # task.agents -> route override
    assert inputs.route_override == {Stage.REVIEW: ProviderId.CLAUDE}


def test_build_node_inputs_no_decomposition_leaves_subtask_count_none(tmp_path: Path) -> None:
    p = _fake_pipeline(decomposition=SimpleNamespace(accepted=False, n=1))
    inputs = build_node_inputs(p, flow_dir=tmp_path)  # type: ignore[arg-type]
    assert inputs.subtask_count is None
