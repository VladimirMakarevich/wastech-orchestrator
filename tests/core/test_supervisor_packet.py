"""Unit tests for the run-facts assembly and the packet rendered from it.

These exercise the assembly directly, without a :class:`Supervisor`: nothing in it depends on the
oversight layer, which is what lets the deterministic pull-request body be rendered from the same
facts when that layer does not run. The layer's own use of it is covered in ``test_supervisor.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.core.supervisor_packet import (
    build_packet_facts,
    render_packet,
    split_check_runs,
    summarize_diff,
)
from wastech_orchestrator.providers.artifacts import (
    exchange_node_run_dir,
    node_run_dir,
    task_artifact_dir,
)
from wastech_orchestrator.state_store import (
    CheckRunRow,
    EvaluationRow,
    NodeRunRow,
    StateStore,
    TaskRow,
)

_TASK = "task-1"

_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 keep
+added one
+added two
-removed one
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-was here
"""


def _store(tmp_path: Path) -> StateStore:
    store = StateStore.open(tmp_path / "state.db")
    store.insert_task(TaskRow(task_id=_TASK, title="T", status=Status.RUNNING))
    return store


def _facts(tmp_path: Path, store: StateStore, **kwargs):
    """Assemble facts against a repo whose exchange lives inside it, so paths are resolvable."""
    repo = tmp_path / "repo"
    (repo / ".worc-io").mkdir(parents=True, exist_ok=True)
    return build_packet_facts(
        store,
        task_id=_TASK,
        task_title="T",
        task_type="implementation",
        flow_name="implementation",
        evaluations=list(store.get_evaluations(_TASK)),
        artifacts_root=str(tmp_path / "art"),
        exchange_root=str(repo / ".worc-io"),
        repo_dir=str(repo),
        **kwargs,
    )


def _run_row(store: StateStore, node: str, kind: str, **kwargs) -> int:
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


def _seed_diff(tmp_path: Path, text: str = _DIFF) -> None:
    task_dir = task_artifact_dir(tmp_path / "art", _TASK)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "current.diff").write_text(text, encoding="utf-8")
    exchange_task = tmp_path / "repo" / ".worc-io" / _TASK
    exchange_task.mkdir(parents=True, exist_ok=True)
    (exchange_task / "current.diff").write_text(text, encoding="utf-8")


# -- the assembly -------------------------------------------------------------


def test_build_packet_facts_is_a_pure_function_of_durable_state(tmp_path: Path) -> None:
    # The reproducibility contract (P0-D2), asserted on the assembly itself now that two surfaces
    # are built from it: the same state.db must yield equal facts and byte-identical rendered bytes.
    store = _store(tmp_path)
    run_id = _run_row(store, "implementation", "agent", provider_used="claude")
    run_dir = node_run_dir(tmp_path / "art", _TASK, "implementation", run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "implementation.out.md").write_text("wired the parser", encoding="utf-8")
    _seed_diff(tmp_path)

    first, second = _facts(tmp_path, store), _facts(tmp_path, store)
    assert first == second
    assert render_packet(first) == render_packet(second)
    assert (
        render_packet(first)
        == json.dumps(json.loads(render_packet(first)), indent=2, sort_keys=True) + "\n"
    )


def test_build_packet_facts_omits_observations_by_default(tmp_path: Path) -> None:
    # The observation digest is the one layer-authored field, so every caller that has no
    # observations to carry — the deterministic report — gets `None` without asking for it.
    facts = _facts(tmp_path, _store(tmp_path))
    assert facts.material_observations is None
    assert _facts(tmp_path, _store(tmp_path), material_observations="- noted").material_observations


def test_build_packet_facts_paths_are_repo_relative_posix(tmp_path: Path) -> None:
    # Absolute or backslashed paths inside the facts would make the rendered bytes
    # machine-dependent, which is what the byte-identity contract above rests on.
    store = _store(tmp_path)
    _seed_diff(tmp_path)
    facts = _facts(tmp_path, store)
    assert facts.diff_path == ".worc-io/task-1/current.diff"
    assert "\\" not in facts.diff_path and not Path(facts.diff_path).is_absolute()


def test_build_packet_facts_names_the_last_verdicts_published_findings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for run_id in (7, 9):
        store.record_evaluation(
            EvaluationRow(
                task_id=_TASK,
                kind="in_flow_verdict",
                verdict="accept",
                node_id="review",
                source_node_run_id=run_id,
            )
        )
        exchange_run = exchange_node_run_dir(
            tmp_path / "repo" / ".worc-io", _TASK, "review", run_id
        )
        exchange_run.mkdir(parents=True, exist_ok=True)
        (exchange_run / "findings.json").write_text('{"findings": []}', encoding="utf-8")
    # The LAST verdict's findings, not the first: that is the one `fixing` and the summary act on.
    findings_path = _facts(tmp_path, store).findings_path
    assert findings_path is not None
    assert findings_path.endswith("run-000009/findings.json")
    assert findings_path.startswith(".worc-io/task-1/")


def test_build_packet_facts_has_no_paths_without_an_exchange(tmp_path: Path) -> None:
    # Only the exchange copy is ever named, because it is the only copy a provider may read.
    store = _store(tmp_path)
    _seed_diff(tmp_path)
    facts = build_packet_facts(
        store,
        task_id=_TASK,
        task_title="T",
        task_type=None,
        flow_name=None,
        evaluations=[],
        artifacts_root=str(tmp_path / "art"),
        exchange_root="",
        repo_dir=str(tmp_path / "repo"),
    )
    assert facts.diff_path is None and facts.findings_path is None
    assert facts.diff_text == _DIFF  # the private artifact is still read


# -- the shared derivations ---------------------------------------------------


def test_summarize_diff_counts_paths_and_lines(tmp_path: Path) -> None:
    summary = summarize_diff(_DIFF)
    # A deletion names its file on the `---` line (`+++ /dev/null`), so it is not lost.
    assert summary.paths == ("src/app.py", "gone.py")
    # The `---`/`+++` headers are not counted as ± lines — only hunk bodies are.
    assert (summary.insertions, summary.deletions) == (2, 2)


def test_summarize_diff_of_nothing_is_empty(tmp_path: Path) -> None:
    summary = summarize_diff("")
    assert summary.paths == () and summary.insertions == 0 and summary.deletions == 0


def test_split_check_runs_keeps_skipped_out_of_failed(tmp_path: Path) -> None:
    rows = (
        CheckRunRow(task_id=_TASK, command="ruff check .", passed=True, log_path="a"),
        CheckRunRow(task_id=_TASK, command="pytest -q", passed=False, log_path="b"),
        CheckRunRow(task_id=_TASK, command="npm test", passed=False, log_path="c", skipped=True),
    )
    outcomes = split_check_runs(rows)
    assert outcomes.passed == ("ruff check .",)
    # A check whose toolchain was absent did not fail, and saying it did is wrong in the direction
    # that matters — it reads as a quality verdict the run never reached.
    assert outcomes.failed == ("pytest -q",)
    assert outcomes.skipped == ("npm test",)
