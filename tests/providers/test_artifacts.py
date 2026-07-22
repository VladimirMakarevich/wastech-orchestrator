"""Tests for the artifact writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator.providers.artifacts import (
    EXCHANGE_HOME,
    ArtifactPaths,
    PathIdentityError,
    append_node_history,
    assert_contained_path,
    create_attempt_dir,
    exchange_latest_run_file,
    exchange_node_run_dir,
    exchange_task_dir,
    latest_run_file,
    node_history_path,
    node_run_dir,
    prune_attempt_artifacts,
    task_artifact_dir,
    task_artifact_relpath,
    write_request_artifact,
    write_result_artifact,
)
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    ErrorClass,
    NormalizedError,
    RunStatus,
)


def test_attempt_dir_layout(tmp_path: Path) -> None:
    paths = create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=42)
    expected = tmp_path / "logs" / "task-001" / "stages" / "planning" / "run-000042" / "1-codex"
    assert Path(paths.attempt_dir) == expected
    assert expected.is_dir()
    assert Path(paths.request_path) == expected / "request.json"
    assert Path(paths.events_path) == expected / "events.jsonl"


def test_subtask_dir_is_zero_padded(tmp_path: Path) -> None:
    paths = create_attempt_dir(
        tmp_path,
        "t",
        "implementation",
        2,
        "codex",
        node_run_id=7,
        subtask=2,
    )
    expected = (
        tmp_path / "logs" / "t" / "stages" / "implementation" / "sub-02" / "run-000007" / "2-codex"
    )
    assert Path(paths.attempt_dir) == expected
    assert expected.is_dir()


def test_exchange_task_dir_has_no_logs_segment(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    td = exchange_task_dir(exchange_root, "add-http-retry")
    assert td == exchange_root / "add-http-retry"
    assert "logs" not in td.parts
    # The exchange dir must never be the private-artifact layout.
    assert td != task_artifact_dir(exchange_root, "add-http-retry")
    # Pure builder: nothing is created on disk.
    assert not td.exists()


def test_exchange_node_run_dir_layout(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    run_dir = exchange_node_run_dir(exchange_root, "t", "implementation", 3)
    assert run_dir == exchange_root / "t" / "stages" / "implementation" / "run-000003"
    sub = exchange_node_run_dir(exchange_root, "t", "implementation", 3, subtask=2)
    assert sub == exchange_root / "t" / "stages" / "implementation" / "sub-02" / "run-000003"
    assert not run_dir.exists()


def test_exchange_latest_run_file_picks_newest_with_content(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    for run_id, has_file in ((1, True), (2, False)):
        d = exchange_node_run_dir(exchange_root, "t", "impl", run_id)
        d.mkdir(parents=True)
        if has_file:
            (d / "impl.out.md").write_text("body", encoding="utf-8")
    # run-000002 exists but is empty → the newest run *containing* the file (run-000001) wins.
    found = exchange_latest_run_file(exchange_root, "t", "impl", "impl.out.md")
    assert found == exchange_node_run_dir(exchange_root, "t", "impl", 1) / "impl.out.md"
    assert exchange_latest_run_file(exchange_root, "t", "impl", "absent.md") is None
    assert exchange_latest_run_file(exchange_root, "t", "never-ran", "x.md") is None


def test_never_overwrites_existing_attempt_dir(tmp_path: Path) -> None:
    create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)
    with pytest.raises(FileExistsError):
        create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)


def test_distinct_attempts_get_distinct_dirs(tmp_path: Path) -> None:
    a = create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)
    b = create_attempt_dir(tmp_path, "task-001", "planning", 2, "codex", node_run_id=1)
    assert a.attempt_dir != b.attempt_dir


def test_distinct_stage_runs_get_distinct_dirs_when_attempt_resets(tmp_path: Path) -> None:
    a = create_attempt_dir(tmp_path, "task-001", "fixing", 1, "claude", node_run_id=10)
    b = create_attempt_dir(tmp_path, "task-001", "fixing", 1, "claude", node_run_id=11)
    assert a.attempt_dir != b.attempt_dir


def test_write_request_artifact_roundtrip(tmp_path: Path) -> None:
    paths = create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)
    write_request_artifact(paths, {"task_id": "task-001", "stage": "planning", "extra_args": []})
    loaded = json.loads(Path(paths.request_path).read_text(encoding="utf-8"))
    assert loaded == {"task_id": "task-001", "stage": "planning", "extra_args": []}


def test_write_result_artifact_serializes_enums_and_error(tmp_path: Path) -> None:
    paths = create_attempt_dir(tmp_path, "task-001", "review", 1, "codex", node_run_id=1)
    result = AgentRunResult(
        status=RunStatus.FAILED,
        provider="codex",
        node_id="review",
        attempt=1,
        exit_code=0,
        started_at="2026-06-11T00:00:00+00:00",
        finished_at="2026-06-11T00:01:00+00:00",
        error=NormalizedError(ErrorClass.TASK_FAILURE, "did not satisfy the task"),
    )
    write_result_artifact(paths, result)
    loaded = json.loads(Path(paths.result_path).read_text(encoding="utf-8"))
    assert loaded["status"] == "failed"
    assert loaded["node_id"] == "review"
    assert loaded["error"]["error_class"] == "task_failure"
    assert loaded["error"]["message"] == "did not satisfy the task"


_FULL_FILE_SET = frozenset(
    {
        "request.json",
        "stdout.log",
        "stderr.log",
        "events.jsonl",
        "result.json",
        "output-schema.json",  # provider-specific extra (typed-output nodes)
    }
)


def _seed_attempt(tmp_path: Path, task_id: str = "task-001") -> ArtifactPaths:
    """Create an attempt dir with the full per-attempt file set and return its paths."""
    paths = create_attempt_dir(tmp_path, task_id, "planning", 1, "codex", node_run_id=1)
    for name in _FULL_FILE_SET:
        (Path(paths.attempt_dir) / name).write_text("x", encoding="utf-8")
    return paths


def test_prune_minimal_keeps_only_result_json(tmp_path: Path) -> None:
    paths = _seed_attempt(tmp_path)
    prune_attempt_artifacts(paths, "minimal")
    survivors = {entry.name for entry in Path(paths.attempt_dir).iterdir()}
    assert survivors == {"result.json"}


def test_prune_standard_keeps_stdout_stderr_result(tmp_path: Path) -> None:
    paths = _seed_attempt(tmp_path)
    prune_attempt_artifacts(paths, "standard")
    survivors = {entry.name for entry in Path(paths.attempt_dir).iterdir()}
    assert survivors == {"result.json", "stdout.log", "stderr.log"}


@pytest.mark.parametrize("level", ["full", "weird-unknown-level"])
def test_prune_full_and_unknown_keep_everything(tmp_path: Path, level: str) -> None:
    paths = _seed_attempt(tmp_path, task_id=f"task-{level}")
    prune_attempt_artifacts(paths, level)
    survivors = {entry.name for entry in Path(paths.attempt_dir).iterdir()}
    assert survivors == set(_FULL_FILE_SET)


# -- per-run history helpers (preserve-node-run-artifact-history) -------------


def test_node_run_dir_is_parent_of_attempt_dirs(tmp_path: Path) -> None:
    # The per-run payload dir is exactly the parent of create_attempt_dir's <attempt>-<provider>/,
    # so a run's findings/prompt/output sit next to its provider attempts. Pure path (no mkdir).
    run_dir = node_run_dir(tmp_path, "t", "review", 42)
    assert run_dir == tmp_path / "logs" / "t" / "stages" / "review" / "run-000042"
    assert not run_dir.exists()  # builder does not create the directory
    attempt = create_attempt_dir(tmp_path, "t", "review", 1, "codex", node_run_id=42)
    assert Path(attempt.attempt_dir).parent == run_dir


def test_per_run_payloads_survive_minimal_pruning(tmp_path: Path) -> None:
    # Retention-by-placement: payloads live at the run-<id>/ level, outside the leaf
    # <attempt>-<provider>/ dir that prune_attempt_artifacts iterates — so minimal never deletes
    # run history. This is why the ADR needs no change to prune_attempt_artifacts.
    paths = _seed_attempt(tmp_path)  # creates .../run-000001/1-codex/ with the full file set
    run_dir = Path(paths.attempt_dir).parent
    findings = run_dir / "findings.json"
    findings.write_text("{}", encoding="utf-8")
    prune_attempt_artifacts(paths, "minimal")
    assert findings.is_file()  # the per-run payload is untouched
    assert {e.name for e in Path(paths.attempt_dir).iterdir()} == {"result.json"}


def test_append_node_history_appends_one_line_per_call(tmp_path: Path) -> None:
    append_node_history(tmp_path, "t", "review", {"run_id": 1, "outcome": "rework"})
    append_node_history(tmp_path, "t", "review", {"run_id": 2, "outcome": "accept"})
    lines = node_history_path(tmp_path, "t", "review").read_text("utf-8").splitlines()
    assert [json.loads(line)["run_id"] for line in lines] == [1, 2]
    assert json.loads(lines[1])["outcome"] == "accept"


def test_latest_run_file_picks_newest_run_containing_the_file(tmp_path: Path) -> None:
    # Content-aware: an empty newer run (a provider attempt made run-000003/ but no payload) must
    # NOT shadow an older run's real output — the resolver returns the newest run WITH the file.
    for run_id, body in ((1, "OLD"), (2, "NEW")):
        d = node_run_dir(tmp_path, "t", "scan", run_id)
        d.mkdir(parents=True)
        (d / "scan.out.md").write_text(body, "utf-8")
    node_run_dir(tmp_path, "t", "scan", 3).mkdir(parents=True)  # empty newest run, no payload
    found = latest_run_file(tmp_path, "t", "scan", "scan.out.md")
    assert found is not None and found.read_text("utf-8") == "NEW"


def test_latest_run_file_none_when_absent(tmp_path: Path) -> None:
    assert latest_run_file(tmp_path, "t", "scan", "scan.out.md") is None  # no stages/scan/ at all
    node_run_dir(tmp_path, "t", "scan", 1).mkdir(parents=True)  # run dir but no payload file
    assert latest_run_file(tmp_path, "t", "scan", "scan.out.md") is None


def test_task_artifact_relpath_is_repo_relative_posix(tmp_path: Path) -> None:
    # F36: a memory episode stores .worc/logs/<task-id>, not the absolute host path — no /Users/…
    # prefix to leak or to collide with a run-harvested redaction literal.
    repo = tmp_path / "repo"
    artifacts_root = repo / ".worc"
    rel = task_artifact_relpath(artifacts_root, "t-42", repo)
    assert rel == ".worc/logs/t-42"
    assert not Path(rel).is_absolute()


def test_task_artifact_relpath_falls_back_to_absolute_when_outside_repo(tmp_path: Path) -> None:
    # An artifact root outside the repo (unusual) degrades to the absolute POSIX form, not a crash.
    repo = tmp_path / "repo"
    artifacts_root = tmp_path / "elsewhere"
    rel = task_artifact_relpath(artifacts_root, "t-1", repo)
    assert rel == task_artifact_dir(artifacts_root, "t-1").resolve().as_posix()


# -- containment belt (WRI-008) ----------------------------------------------


def test_assert_contained_path_returns_a_child_path(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    target = root / "t" / "stages" / "a" / "run-000001"
    assert assert_contained_path(root, target) == target


def test_assert_contained_path_rejects_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    with pytest.raises(PathIdentityError):
        assert_contained_path(root, root / ".." / ".." / "evil")


def test_assert_contained_path_rejects_absolute_outside_root(tmp_path: Path) -> None:
    # An absolute target outside the root escapes on every OS (POSIX ``/etc`` / Windows ``C:\``).
    root = tmp_path / "logs"
    outside = Path(tmp_path.anchor) / "etc" / "evil"
    with pytest.raises(PathIdentityError):
        assert_contained_path(root, outside)


def test_create_attempt_dir_refuses_a_traversing_node_id(tmp_path: Path) -> None:
    # Defense in depth: even if node-id validation were bypassed, a traversing id that would escape
    # the artifact root is refused at the write boundary before any directory is created.
    escape = "../" * 8 + "evil"
    with pytest.raises(PathIdentityError):
        create_attempt_dir(tmp_path, "t", escape, 1, "codex", node_run_id=1)
    assert not (tmp_path / "evil").exists()
