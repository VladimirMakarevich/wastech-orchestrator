"""P3.1 — the core ``checks`` checkers: citation (deterministic, gating) + dependency_scan.

Two layers: the pure checker functions (``validate_citations`` / ``run_dependency_scan``) and the
``ChecksNodeRunner`` dispatch that maps each to the engine ``pass`` / ``fail`` outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wastech_orchestrator.core.flow.checkers.citation import (
    CitationStatus,
    validate_citations,
)
from wastech_orchestrator.core.flow.checkers.dependency_scan import (
    DEFAULT_DEPENDENCY_SCANNERS,
    run_dependency_scan,
)
from wastech_orchestrator.core.flow.contracts import (
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
)
from wastech_orchestrator.core.flow.engine import NodeContext
from wastech_orchestrator.core.flow.nodes import ChecksNodeRunner, NodeInputs, NodeServices
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import ChecksNode, FlowDoc
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.providers.artifacts import node_run_dir
from wastech_orchestrator.providers.process import ProcessResult

# -- fakes / helpers ----------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []
        self.check_runs: list[Any] = []
        self._next = 1

    def record_node_run(self, run: Any, conn: Any = None) -> int:
        rid = self._next
        self._next += 1
        return rid

    def complete_node_run(self, run_id: int, **kwargs: Any) -> None:
        self.completed.append({"run_id": run_id, **kwargs})

    def record_check_run(self, run: Any, conn: Any = None) -> None:
        self.check_runs.append(run)


def _snapshot(node: ChecksNode, output_policy: OutputPolicy) -> FlowSnapshot:
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=output_policy,
        publishing=PublishingPolicy.NONE,
        nodes=(node,),
        edges=(),
        budgets=MappingProxyType({}),
    )
    return FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({node.id: node}),
        adjacency=MappingProxyType({}),
        flow_fingerprint="fp",
    )


def _run_checks_node(
    node: ChecksNode,
    *,
    repo_dir: Path,
    artifacts_root: Path,
    output_policy: OutputPolicy,
    run_process: Any = None,
    task_id: str = "t",
) -> tuple[Any, _Store]:
    store = _Store()
    services = NodeServices(
        router=None,  # type: ignore[arg-type]
        check_runner=None,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        repo_dir=str(repo_dir),
        artifacts_root=str(artifacts_root),
        clock=lambda: "ts",
        run_process=run_process or _default_unused_runner,
    )
    ctx = NodeContext(
        snapshot=_snapshot(node, output_policy),
        run_state=FlowRunState(flow_fingerprint="fp"),
        node=node,
        task_id=task_id,
    )
    result = ChecksNodeRunner(services, NodeInputs(flow_dir=repo_dir)).run(node, ctx)
    return result, store


def _default_unused_runner(*args: Any, **kwargs: Any) -> ProcessResult:  # pragma: no cover - guard
    raise AssertionError("run_process should not be called by the citation checker")


def _write_sources(report_dir: Path, sources: list[dict[str, Any]]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "sources.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")


# -- citation: pure validator -------------------------------------------------


def test_citation_verified_pass(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
    manifest = tmp_path / "sources.json"
    manifest.write_text(
        json.dumps(
            {"sources": [{"id": "s1", "path": "src/mod.py", "line": 1, "snippet": "def foo("}]}
        ),
        encoding="utf-8",
    )
    report = validate_citations(tmp_path, manifest)
    assert report.passed is True
    assert report.manifest_status == "ok"
    assert report.entries[0].status is CitationStatus.VERIFIED


def test_citation_hallucinated_to_broken(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.json"
    manifest.write_text(
        json.dumps({"sources": [{"id": "ghost", "path": "src/nope.py", "line": 9}]}),
        encoding="utf-8",
    )
    report = validate_citations(tmp_path, manifest)
    assert report.passed is False
    assert report.entries[0].status is CitationStatus.BROKEN


def test_citation_snippet_mismatch_is_broken(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    manifest = tmp_path / "sources.json"
    manifest.write_text(
        json.dumps({"sources": [{"id": "s", "path": "a.py", "snippet": "y = 2"}]}),
        encoding="utf-8",
    )
    report = validate_citations(tmp_path, manifest)
    assert report.passed is False
    assert report.entries[0].status is CitationStatus.BROKEN


def test_citation_external_url_uncheckable_passes(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.json"
    manifest.write_text(
        json.dumps({"sources": [{"id": "u", "url": "https://example.com/paper"}]}),
        encoding="utf-8",
    )
    report = validate_citations(tmp_path, manifest)
    assert report.passed is True
    assert report.entries[0].status is CitationStatus.UNCHECKABLE


def test_citation_malformed_manifest_uncheckable_no_crash(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.json"
    manifest.write_text("{not valid json", encoding="utf-8")
    report = validate_citations(tmp_path, manifest)  # must not raise
    assert report.passed is True
    assert report.manifest_status == "malformed"
    assert report.entries[0].status is CitationStatus.UNCHECKABLE


def test_citation_missing_manifest_passes(tmp_path: Path) -> None:
    report = validate_citations(tmp_path, tmp_path / "absent.json")
    assert report.passed is True
    assert report.manifest_status == "missing"


def test_citation_path_traversal_is_broken(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    manifest = repo / "sources.json"
    manifest.write_text(
        json.dumps({"sources": [{"id": "esc", "path": "../outside.txt"}]}), encoding="utf-8"
    )
    report = validate_citations(repo, manifest)
    assert report.passed is False
    assert report.entries[0].status is CitationStatus.BROKEN


# -- citation: node dispatch --------------------------------------------------


def test_citation_node_passes_for_verified_manifest(tmp_path: Path) -> None:
    repo, art = tmp_path / "repo", tmp_path / "art"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text("CONST = 7\n", encoding="utf-8")
    _write_sources(
        repo / "docs" / "research" / "t", [{"id": "s", "path": "src/m.py", "snippet": "CONST = 7"}]
    )
    node = ChecksNode(id="citation_check", kind="checks", checker="citation")
    result, store = _run_checks_node(
        node, repo_dir=repo, artifacts_root=art, output_policy=OutputPolicy.REPOSITORY_DOCUMENT
    )
    assert result.outcome.kind == "pass"
    # Per-run: report under stages/<node>/run-<id>/ (node ran first → node_run_id 1).
    assert (node_run_dir(art, "t", "citation_check", 1) / "citation.json").is_file()
    assert store.completed[-1]["outcome"] == "pass"


def test_citation_node_fails_for_hallucinated_manifest(tmp_path: Path) -> None:
    repo, art = tmp_path / "repo", tmp_path / "art"
    repo.mkdir()
    _write_sources(repo / "docs" / "research" / "t", [{"id": "x", "path": "src/ghost.py"}])
    node = ChecksNode(id="citation_check", kind="checks", checker="citation")
    result, _ = _run_checks_node(
        node, repo_dir=repo, artifacts_root=art, output_policy=OutputPolicy.REPOSITORY_DOCUMENT
    )
    assert result.outcome.kind == "fail"


# -- dependency_scan: pure runner ---------------------------------------------


def _fake_runner(per_call: dict[str, ProcessResult]):
    calls: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> ProcessResult:
        calls.append({"argv": argv, **kwargs})
        return per_call.get(
            argv[0], ProcessResult(0, False, None, 0.1, str(kwargs["stdout_path"]), "")
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_dependency_scan_argv_with_timeout(tmp_path: Path) -> None:
    runner = _fake_runner({})
    report = run_dependency_scan(
        repo_dir=tmp_path,
        logs_dir=tmp_path / "scan",
        env={"PATH": "/usr/bin"},
        timeout_seconds=123,
        run_process=runner,
    )
    # Every core scanner launched as an argv list (never a shell string), each with the timeout.
    assert len(runner.calls) == len(DEFAULT_DEPENDENCY_SCANNERS)
    for call in runner.calls:
        assert isinstance(call["argv"], list)
        assert call["timeout_seconds"] == 123
        assert call["env"] == {"PATH": "/usr/bin"}
    assert {r.name for r in report.runs} == {name for name, _ in DEFAULT_DEPENDENCY_SCANNERS}


def test_dependency_scan_records_real_wall_clock_interval(tmp_path: Path) -> None:
    # VF-12: each scanner carries the wall-clock bracket around its subprocess, not two identical
    # row-write stamps — so its check_runs row has a measurable duration.
    runner = _fake_runner({})
    ticks = iter([f"2026-07-25T00:00:{s:02d}+00:00" for s in range(60)])
    report = run_dependency_scan(
        repo_dir=tmp_path,
        logs_dir=tmp_path / "scan",
        env={},
        timeout_seconds=60,
        run_process=runner,
        clock=lambda: next(ticks),
    )
    for scan in report.runs:
        assert scan.started_at < scan.finished_at  # a real interval, never a zero-width stamp


def test_dependency_scan_emits_pass_not_gate(tmp_path: Path) -> None:
    # A scanner that finds vulnerabilities (nonzero exit) AND one that is not installed (launch
    # error) both still yield a passing scan: dependency_scan is evidence, not a gate.
    vulns = ProcessResult(1, False, None, 0.1, str(tmp_path / "scan" / "pip-audit.json"), "")
    missing = ProcessResult(None, False, "could not launch 'osv-scanner'", 0.0, "x", "")
    runner = _fake_runner({"pip-audit": vulns, "osv-scanner": missing})
    report = run_dependency_scan(
        repo_dir=tmp_path,
        logs_dir=tmp_path / "scan",
        env={},
        timeout_seconds=60,
        run_process=runner,
    )
    assert report.passed is True
    by_name = {r.name: r for r in report.runs}
    assert by_name["pip-audit"].launched is True and by_name["pip-audit"].exit_code == 1
    assert by_name["osv-scanner"].launched is False


def test_dependency_scan_node_passes_and_records_evidence(tmp_path: Path) -> None:
    repo, art = tmp_path / "repo", tmp_path / "art"
    repo.mkdir()
    runner = _fake_runner({})  # all scanners "ran clean"
    node = ChecksNode(id="dependency_scan", kind="checks", checker="dependency_scan")
    result, store = _run_checks_node(
        node,
        repo_dir=repo,
        artifacts_root=art,
        output_policy=OutputPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT,
        run_process=runner,
    )
    assert result.outcome.kind == "pass"
    assert len(store.check_runs) == len(DEFAULT_DEPENDENCY_SCANNERS)  # one row per scanner
    assert (node_run_dir(art, "t", "dependency_scan", 1) / "dependency_scan.json").is_file()
