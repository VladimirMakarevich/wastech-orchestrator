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
from wastech_orchestrator.ledger import Ledger, LedgerRecord
from wastech_orchestrator.providers.artifacts import (
    TOOL_STDOUT_FILENAME,
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


def _art(tmp_path: Path) -> Path:
    """The private artifact root, inside the repo exactly as ``<repo>/.worc`` is in a real run."""
    return tmp_path / "repo" / ".worc"


def _facts(tmp_path: Path, store: StateStore, **kwargs):
    """Assemble facts against a repo whose private artifact tree lives inside it."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return build_packet_facts(
        store,
        task_id=_TASK,
        task_title="T",
        task_type="implementation",
        flow_name="implementation",
        evaluations=list(store.get_evaluations(_TASK)),
        artifacts_root=str(_art(tmp_path)),
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
    task_dir = task_artifact_dir(_art(tmp_path), _TASK)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "current.diff").write_text(text, encoding="utf-8")


# -- the assembly -------------------------------------------------------------


def test_build_packet_facts_is_a_pure_function_of_durable_state(tmp_path: Path) -> None:
    # The reproducibility contract, asserted on the assembly itself now that two surfaces
    # are built from it: the same state.db must yield equal facts and byte-identical rendered bytes.
    store = _store(tmp_path)
    run_id = _run_row(store, "implementation", "agent", provider_used="claude")
    run_dir = node_run_dir(_art(tmp_path), _TASK, "implementation", run_id)
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
    assert facts.diff_path == ".worc/logs/task-1/current.diff"
    assert "\\" not in facts.diff_path and not Path(facts.diff_path).is_absolute()


def test_build_packet_facts_names_the_last_verdicts_private_findings(tmp_path: Path) -> None:
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
        run_dir = node_run_dir(_art(tmp_path), _TASK, "review", run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "findings.json").write_text('{"findings": []}', encoding="utf-8")
    # The LAST verdict's findings, not the first: that is the one `fixing` and the summary act on.
    findings_path = _facts(tmp_path, store).findings_path
    assert findings_path is not None
    assert findings_path.endswith("run-000009/findings.json")
    # The private copy under logs/, which survives terminal cleanup — the exchange one does not.
    assert findings_path.startswith(".worc/logs/task-1/")


def test_build_packet_facts_paths_are_the_copies_that_survive_terminal_cleanup(
    tmp_path: Path,
) -> None:
    # The exchange is removed at terminal, so a packet naming `.worc-io/...` names nothing after
    # the run it describes. Every path in a saved packet points into the private tree instead.
    store = _store(tmp_path)
    _seed_diff(tmp_path)
    store.record_evaluation(
        EvaluationRow(
            task_id=_TASK,
            kind="in_flow_verdict",
            verdict="accept",
            node_id="review",
            source_node_run_id=3,
        )
    )
    run_dir = node_run_dir(_art(tmp_path), _TASK, "review", 3)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "findings.json").write_text('{"findings": []}', encoding="utf-8")
    facts = _facts(tmp_path, store)
    for path in (facts.diff_path, facts.findings_path):
        assert path is not None and ".worc-io" not in path
        assert (tmp_path / "repo" / path).is_file()


def test_build_packet_facts_has_no_paths_outside_the_repository(tmp_path: Path) -> None:
    # A private tree an operator moved outside the clone cannot be named repo-relative, and an
    # absolute path would make the rendered bytes machine-dependent.
    store = _store(tmp_path)
    _seed_diff(tmp_path)
    facts = build_packet_facts(
        store,
        task_id=_TASK,
        task_title="T",
        task_type=None,
        flow_name=None,
        evaluations=[],
        artifacts_root=str(_art(tmp_path)),
        repo_dir=str(tmp_path / "elsewhere"),
    )
    assert facts.diff_path is None and facts.findings_path is None
    assert facts.diff_text == _DIFF  # the private artifact is still read


# -- what the gates said (P1.8) -----------------------------------------------


def _seed_tool_stdout(tmp_path: Path, node: str, run_id: int, stdout: str) -> None:
    run_dir = node_run_dir(_art(tmp_path), _TASK, node, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / TOOL_STDOUT_FILENAME).write_text(stdout, encoding="utf-8")


def _record_verdict(store: StateStore, node: str, run_id: int, findings_json: str) -> None:
    store.record_evaluation(
        EvaluationRow(
            task_id=_TASK,
            kind="in_flow_verdict",
            verdict="rework",
            node_id=node,
            source_node_run_id=run_id,
            findings_json=findings_json,
        )
    )


def test_a_tool_step_carries_its_data_and_a_head_of_its_stdout(tmp_path: Path) -> None:
    # The gate's verdict reached the packet as timing and an outcome word only, so the finalize
    # turn could describe what the gate decided but not what it measured.
    store = _store(tmp_path)
    run_id = _run_row(store, "form_gate", "tool", status="failed", outcome="fail")
    _seed_tool_stdout(
        tmp_path,
        "form_gate",
        run_id,
        '{"outcome": "fail", "data": {"paragraphs": 7, "over_limit": 2}}',
    )
    step = json.loads(render_packet(_facts(tmp_path, store)))["steps"][0]
    assert '"over_limit": 2' in step["data"]
    assert "over_limit" in step["stdout_head"]


def test_each_evaluator_step_carries_its_own_findings(tmp_path: Path) -> None:
    # One `findings_path` for the whole run names the last lens only; a flow with two lenses and
    # ten rework rounds then reports nine finding sets nowhere at all.
    store = _store(tmp_path)
    first = _run_row(store, "fidelity_critic", "evaluator", outcome="rework")
    second = _run_row(store, "voice_critic", "evaluator", outcome="rework")
    _record_verdict(store, "fidelity_critic", first, '[{"title": "meaning drifted"}]')
    _record_verdict(store, "voice_critic", second, '[{"title": "register too formal"}]')
    steps = json.loads(render_packet(_facts(tmp_path, store)))["steps"]
    assert steps[0]["findings"] == [{"title": "meaning drifted"}]
    assert steps[1]["findings"] == [{"title": "register too formal"}]


def test_an_empty_verdict_adds_no_findings_key(tmp_path: Path) -> None:
    # An accept with nothing to say must read as "nothing found", not as an empty recorded field.
    store = _store(tmp_path)
    run_id = _run_row(store, "review", "evaluator", outcome="accept")
    _record_verdict(store, "review", run_id, "[]")
    assert "findings" not in json.loads(render_packet(_facts(tmp_path, store)))["steps"][0]


def test_a_chatty_tool_and_a_long_finding_set_stay_inside_the_packets_bounds(
    tmp_path: Path,
) -> None:
    # The finalize turn's budget rests on the packet being bounded, so every inline field a node
    # controls needs its own cap — otherwise one chatty tool inflates every packet of the run.
    store = _store(tmp_path)
    tool_run = _run_row(store, "form_gate", "tool", status="failed", outcome="fail")
    _seed_tool_stdout(
        tmp_path,
        "form_gate",
        tool_run,
        json.dumps({"outcome": "fail", "data": {"note": "x" * 50_000}}),
    )
    eval_run = _run_row(store, "review", "evaluator", outcome="rework")
    _record_verdict(store, "review", eval_run, json.dumps([{"title": "z" * 50_000}]))

    rendered = render_packet(_facts(tmp_path, store))
    steps = json.loads(rendered)["steps"]
    assert all(len(steps[0][field]) <= 1_000 for field in ("data", "stdout_head"))
    assert len(steps[1]["findings"]) <= 2_000
    assert all(steps[0][f].endswith("\u2026") for f in ("data", "stdout_head"))
    assert len(rendered) < 10_000


def test_an_unfinished_publish_step_is_labelled_rather_than_left_running(tmp_path: Path) -> None:
    # The packet is built by the publish node's own finalize hook, so this row is ALWAYS open —
    # left as `running` the finalize turn narrated it in the pull-request body as an incident.
    store = _store(tmp_path)
    store.record_node_run(
        NodeRunRow(
            task_id=_TASK,
            node_id="publish",
            node_kind="publish",
            status="running",
            started_at="2026-01-01T00:02:00+00:00",
        )
    )
    step = json.loads(render_packet(_facts(tmp_path, store)))["steps"][0]
    assert step["status"] == "pending" and step["finished_at"] is None
    assert "expected" in step["note"]


def test_a_finished_publish_step_keeps_its_own_status(tmp_path: Path) -> None:
    # The label is for the one row the packet is built inside, never a blanket rewrite of the kind.
    store = _store(tmp_path)
    _run_row(store, "publish", "publish", status="published", outcome="done")
    step = json.loads(render_packet(_facts(tmp_path, store)))["steps"][0]
    assert step["status"] == "published" and "note" not in step


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


# -- what a run that stopped and resumed leaves in the record (enrich-07b) -------------------------


def _timed_run(store: StateStore, node: str, kind: str, start: str, outcome: str) -> int:
    return store.record_node_run(
        NodeRunRow(
            task_id=_TASK,
            node_id=node,
            node_kind=kind,
            status="succeeded",
            outcome=outcome,
            started_at=start,
            finished_at=start,
        )
    )


def test_a_long_verdict_reaches_the_packet_whole(tmp_path: Path) -> None:
    # sources_gate's last pass reported 30 findings; the packet cut their JSON at 2 000 characters,
    # and the summary wrote "the findings list in the packet is truncated" into the PR body.
    store = _store(tmp_path)
    run_id = _run_row(store, "sources_gate", "evaluator", outcome="accept")
    findings = [
        {"severity": "medium", "reason": f"finding {n}: " + "x" * 400, "paths": [f"r.md:{n}"]}
        for n in range(30)
    ]
    _record_verdict(store, "sources_gate", run_id, json.dumps(findings))
    step = json.loads(render_packet(_facts(tmp_path, store)))["steps"][0]
    assert step["findings_total"] == 30
    assert "findings_omitted" not in step
    assert [item["reason"].split(":")[0] for item in step["findings"]] == [
        f"finding {n}" for n in range(30)
    ]
    assert step["findings"][7]["paths"] == ["r.md:7"]


def test_a_superseded_verdict_is_compact_but_still_counts_every_finding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _run_row(store, "gate", "evaluator", outcome="rework")
    second = _run_row(store, "gate", "evaluator", outcome="accept")
    many = [{"severity": "low", "reason": "y" * 1_000} for _ in range(40)]
    _record_verdict(store, "gate", first, json.dumps(many))
    _record_verdict(store, "gate", second, json.dumps(many[:2]))
    steps = json.loads(render_packet(_facts(tmp_path, store)))["steps"]
    assert steps[0]["findings_superseded"] is True
    assert steps[0]["findings_total"] == 40
    # Cut, and saying so — never silently.
    assert steps[0]["findings_omitted"] == 40 - len(steps[0]["findings"])
    assert "findings_superseded" not in steps[1]


def test_a_stop_and_resume_between_two_passes_is_on_the_record(tmp_path: Path) -> None:
    # sources_gate reworked, the loop budget was spent, the task parked, and `rerun --continue` ran
    # the gate again. The record showed rework → gate with nothing between, which the summary read
    # as "the fix step between the second and third pass is not recorded".
    store = _store(tmp_path)
    _timed_run(store, "sources_gate", "evaluator", "2026-10-19T15:38:37+00:00", "rework")
    _timed_run(store, "sources_gate", "evaluator", "2026-10-19T16:01:58+00:00", "accept")
    ledger = Ledger(_art(tmp_path) / "logs")
    ledger.append(
        LedgerRecord(
            id=_TASK,
            title="T",
            final_status="manual_action_required",
            finished_at="2026-10-19T15:55:39+00:00",
            stop_node="sources_gate",
            stuck_loop="sources_fix",
            stuck_limit="max_fix_cycles",
        )
    )
    ledger.append(  # a different task's stop is not this task's business
        LedgerRecord(
            id="other",
            title="O",
            final_status="manual_action_required",
            finished_at="2026-10-19T15:56:00+00:00",
        )
    )
    steps = json.loads(render_packet(_facts(tmp_path, store)))["steps"]
    assert [step.get("kind") for step in steps] == ["evaluator", "interruption", "evaluator"]
    note = steps[1]["note"]
    assert "'sources_gate'" in note and "'sources_fix'" in note and "max_fix_cycles" in note
    assert "not because a step is missing" in note


def test_an_interruption_older_than_the_record_is_left_out(tmp_path: Path) -> None:
    # A fresh rerun deleted the steps the older attempt ran; its stop has nothing to sit between.
    store = _store(tmp_path)
    _timed_run(store, "review", "evaluator", "2026-10-19T16:00:00+00:00", "accept")
    Ledger(_art(tmp_path) / "logs").append(
        LedgerRecord(
            id=_TASK,
            title="T",
            final_status="failed",
            finished_at="2026-10-19T12:00:00+00:00",
        )
    )
    steps = json.loads(render_packet(_facts(tmp_path, store)))["steps"]
    assert [step.get("kind") for step in steps] == ["evaluator"]


def test_superseded_verdicts_share_one_budget_across_the_packet(tmp_path: Path) -> None:
    # A fifteen-round loop must not grow the packet fifteen-fold: past the shared budget a
    # superseded verdict keeps its counts and nothing else.
    store = _store(tmp_path)
    verdict = json.dumps([{"severity": "low", "reason": "z" * 250} for _ in range(30)])
    for _ in range(12):
        run_id = _run_row(store, "review", "evaluator", outcome="rework")
        _record_verdict(store, "review", run_id, verdict)
    steps = json.loads(render_packet(_facts(tmp_path, store)))["steps"]
    superseded = [step for step in steps if step.get("findings_superseded")]
    assert len(superseded) == 11
    assert sum(len(json.dumps(step["findings"])) for step in superseded) <= 24_000 + 11 * 2
    assert superseded[-1]["findings"] == []
    assert superseded[-1]["findings_omitted"] == 30
    assert "findings_superseded" not in steps[-1]


def test_an_entry_that_is_not_a_finding_is_not_reported_as_cut(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = _run_row(store, "gate", "evaluator", outcome="accept")
    _record_verdict(store, "gate", run_id, json.dumps(["stray note", {"severity": "low"}]))
    step = json.loads(render_packet(_facts(tmp_path, store)))["steps"][0]
    assert step["findings_total"] == 1
    assert "findings_omitted" not in step
    assert step["findings_malformed"] == 1


def test_an_older_infra_stop_is_not_called_a_fix_loop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _timed_run(store, "thinkers", "agent", "2026-10-19T14:30:00+00:00", "done")
    _timed_run(store, "thinkers", "agent", "2026-10-19T16:00:00+00:00", "done")
    Ledger(_art(tmp_path) / "logs").append(
        LedgerRecord(
            id=_TASK,
            title="T",
            final_status="failed",
            finished_at="2026-10-19T15:00:00+00:00",
            stop_node="thinkers",
            stuck_loop="infra",
            stuck_limit="background_work_lost",
        )
    )
    note = json.loads(render_packet(_facts(tmp_path, store)))["steps"][1]["note"]
    assert "fix loop" not in note
    assert "background_work_lost" in note
