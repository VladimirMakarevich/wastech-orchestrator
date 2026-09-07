"""Unit tests for the deterministic whole-task report — the pull-request body without prose."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.core.flow.recorder import StepFacts
from wastech_orchestrator.core.follow_ups import FollowUp
from wastech_orchestrator.core.summary_report import (
    SUMMARY_JSON_FILENAME,
    render_skipped_nodes_section,
    render_summary_report,
    write_summary_report,
)
from wastech_orchestrator.core.supervisor_packet import PacketFacts
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.state_store import CheckRunRow

_TASK = "task-001"

_DIFF = """\
diff --git a/src/api/validation.py b/src/api/validation.py
--- a/src/api/validation.py
+++ b/src/api/validation.py
@@ -1,2 +1,3 @@
 keep
+added
-dropped
"""


def _step(node_id: str, kind: str = "agent", **kwargs) -> StepFacts:
    defaults = {
        "node_id": node_id,
        "node_kind": kind,
        "status": "completed",
        "outcome": "ok",
        "stage_attempts": 1,
        "subtask_order": None,
        "provider_used": "claude",
        "fallback_from": None,
        "error_class": None,
        "skipped": False,
        "skip_reason": None,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "message": None,
    }
    return StepFacts(**{**defaults, **kwargs})


def _check(
    command: str, *, passed: bool = True, skipped: bool = False, subtask=None
) -> CheckRunRow:
    return CheckRunRow(
        task_id=_TASK,
        command=command,
        passed=passed,
        log_path="log",
        skipped=skipped,
        subtask_order=subtask,
    )


def _facts(**kwargs) -> PacketFacts:
    defaults = {
        "task_id": _TASK,
        "task_title": "Add request validation",
        "task_type": "implementation",
        "flow_name": "implementation",
        "steps": (),
        "check_runs": (),
        "diff_text": "",
        "diff_path": None,
        "findings_path": None,
        "material_observations": None,
    }
    return PacketFacts(**{**defaults, **kwargs})


def _render(facts: PacketFacts, **kwargs) -> str:
    defaults = {
        "follow_ups": (),
        "gates": None,
        "skipped_nodes": (),
        "task_ref": None,
        "degraded": False,
    }
    return render_summary_report(facts, **{**defaults, **kwargs})


# -- determinism and bounds ---------------------------------------------------


def test_two_renders_of_the_same_facts_are_byte_identical() -> None:
    # The same contract the packet keeps. `skipped_nodes` is a frozenset, whose iteration
    # order varies with the hash seed, so an unsorted render would differ ACROSS processes while
    # passing every in-process comparison — hence several names here, and the sort in the renderer.
    facts = _facts(diff_text=_DIFF, steps=(_step("implementation"), _step("review", "evaluator")))
    skipped = frozenset({"deep_research", "documentation", "planning", "refinement"})
    first = _render(facts, skipped_nodes=skipped, gates="- review: verdict `accept`")
    second = _render(facts, skipped_nodes=skipped, gates="- review: verdict `accept`")
    assert first == second
    assert "- `deep_research`\n- `documentation`\n- `planning`\n- `refinement`" in first


def test_report_points_at_the_diff_and_inlines_no_patch() -> None:
    # A pull request already IS its diff. Inlining it once produced a ~580-line committed summary
    # that was almost entirely raw patch. Note the diff here is SMALL enough that the packet would
    # inline it — the report must still refuse.
    assert len(_DIFF) < 4_000
    md = _render(_facts(diff_text=_DIFF))
    assert "diff --git" not in md and "@@" not in md
    assert _DIFF not in md
    assert "1 file changed, 1 insertion, 1 deletion" in md
    assert "- `src/api/validation.py`" in md
    assert f"_Full diff: `logs/{_TASK}/current.diff`._" in md


def test_report_names_the_task_file_rather_than_pasting_it() -> None:
    md = _render(_facts(), task_ref="task-001.md")
    assert "_Task file: `task-001.md`. Flow: `implementation`._" in md


# -- section presence --------------------------------------------------------


def test_every_section_is_present_and_in_order_for_a_full_run() -> None:
    md = _render(
        _facts(
            diff_text=_DIFF,
            steps=(_step("implementation"),),
            check_runs=(_check("pytest -q"),),
        ),
        follow_ups=(FollowUp("nit", "", "low", ("e",)),),
        gates="- review: verdict `accept`, no findings recorded",
        skipped_nodes=("deep_research",),
        task_ref="task-001.md",
    )
    headings = [
        "## Changes",
        "## Steps",
        "## Checks",
        "## Gates",
        "## Technical debt",
        "## Pipeline",
    ]
    positions = [md.index(h) for h in headings]
    assert positions == sorted(positions)
    assert md.endswith("\n") and "\n\n\n" not in md


def test_empty_run_renders_only_the_changes_section() -> None:
    # A section with no data is absent, not an empty heading — except Changes, because on a failed
    # terminal "nothing was changed" is the most load-bearing fact there is.
    md = _render(_facts(flow_name=None))
    assert md == (
        "# Add request validation\n\n## Changes\n\nNo file changes were recorded for this task.\n"
    )
    for absent in ("## Steps", "## Checks", "## Gates", "## Technical debt", "## Pipeline"):
        assert absent not in md
    assert "current.diff" not in md  # no pointer to an artifact that does not exist


def test_steps_report_every_deviation_a_run_recorded() -> None:
    md = _render(
        _facts(
            steps=(
                _step("refinement", skipped=True, skip_reason="task already complete"),
                _step(
                    "implementation",
                    provider_used="codex",
                    fallback_from="claude",
                    stage_attempts=2,
                ),
                _step("checks", "checks", outcome="fail", provider_used=None),
                _step(
                    "review",
                    "evaluator",
                    outcome="accept",
                    subtask_order=2,
                    error_class="transient",
                ),
            )
        )
    )
    assert "- `refinement` (agent): skipped — task already complete" in md
    assert (
        "- `implementation` (agent): completed → ok, provider `codex` "
        "(fell back from `claude`), 2 attempts" in md
    )
    assert "- `checks` (checks): completed → fail" in md
    assert "- `review` (evaluator, subtask 2): completed → accept" in md
    assert "error `transient`" in md


# -- checks ------------------------------------------------------------------


def test_checks_report_the_last_result_per_command() -> None:
    # `check_runs` holds one row per RUN, so a command that failed and was then fixed would land
    # under both Failed and Passed. In the body of a green pull request that is not a history — it
    # is a contradiction.
    md = _render(_facts(check_runs=(_check("pytest -q", passed=False), _check("pytest -q"))))
    assert "- Passed (1): `pytest -q`" in md
    assert "Failed" not in md


def test_skipped_checks_are_never_reported_as_failed() -> None:
    md = _render(
        _facts(
            check_runs=(
                _check("ruff check ."),
                _check("mypy src", passed=False),
                _check("npm test", passed=False, skipped=True),
            )
        )
    )
    assert "- Passed (1): `ruff check .`" in md
    assert "- Failed (1): `mypy src`" in md
    assert "- Skipped (1, toolchain absent): `npm test`" in md


def test_a_command_run_per_subtask_is_listed_once() -> None:
    md = _render(
        _facts(check_runs=(_check("pytest -q", subtask=1), _check("pytest -q", subtask=2)))
    )
    assert "- Passed (1): `pytest -q`" in md


# -- the degradation callout --------------------------------------------------


def test_degraded_prepends_the_callout_and_is_absent_otherwise() -> None:
    # Three states must read differently: layer switched off (no warning), layer expected and no
    # prose (this callout), terminal with no prose by design (no warning).
    md = _render(_facts(), degraded=True)
    assert "Fallback summary" in md
    assert md.index("Fallback summary") < md.index("## Changes")
    assert "Fallback summary" not in _render(_facts(), degraded=False)


def test_skipped_nodes_section_is_sorted_and_shared_with_the_appended_form() -> None:
    assert render_skipped_nodes_section({"b", "a"}) == (
        "## Pipeline nodes skipped\n\n- `a`\n- `b`\n"
    )


# -- the written pair --------------------------------------------------------


def test_written_body_keeps_lf_bytes_on_every_platform(tmp_path: Path) -> None:
    # `summary.md` is committed as the PR description, so the host's line separator must not decide
    # what lands in the repository. A read-back cannot show this — universal newlines hide it.
    md_path, _ = write_summary_report(
        tmp_path,
        _facts(diff_text=_DIFF),
        follow_ups=(),
        gates=None,
        skipped_nodes=(),
        task_ref=None,
        degraded=False,
        supervisor_usage=None,
    )
    raw = Path(md_path).read_bytes()
    assert b"\r\n" not in raw
    assert raw == _render(_facts(diff_text=_DIFF)).encode("utf-8")


def _write(tmp_path: Path, **kwargs) -> dict:
    defaults = {
        "follow_ups": (),
        "gates": None,
        "skipped_nodes": (),
        "task_ref": None,
        "degraded": False,
        "supervisor_usage": None,
    }
    write_summary_report(tmp_path, _facts(), **{**defaults, **kwargs})
    path = task_artifact_dir(tmp_path, _TASK) / SUMMARY_JSON_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def test_summary_json_has_one_key_set_with_no_prose(tmp_path: Path) -> None:
    data = _write(tmp_path)
    assert set(data) == {"what", "summary"}
    assert data["what"] == "Add request validation"
    # Empty by definition on this path: the report in summary.md IS the artifact.
    assert data["summary"] == ""
    # The old four-field contract is gone everywhere, not merely unused.
    for dead in ("how", "integration", "why"):
        assert dead not in data


def test_summary_json_carries_no_spend_when_the_layer_never_ran(tmp_path: Path) -> None:
    # This is how an operator tells "the layer is switched off" from "it ran and could not finish".
    assert "supervisor_usage" not in _write(tmp_path)
    assert "degraded" not in _write(tmp_path)


def test_summary_json_records_the_layers_spend_and_the_degradation(tmp_path: Path) -> None:
    data = _write(
        tmp_path,
        degraded=True,
        supervisor_usage={"total": {"input_tokens": 10}},
        follow_ups=(FollowUp("nit", "why", "low", ("e",), ("a.py",), "fix it"),),
    )
    assert data["supervisor_usage"] == {"total": {"input_tokens": 10}}
    assert data["degraded"] is True
    assert data["follow_ups"] == [
        {
            "title": "nit",
            "rationale": "why",
            "severity": "low",
            "paths": ["a.py"],
            "evidence": ["e"],
            "action_hint": "fix it",
        }
    ]
