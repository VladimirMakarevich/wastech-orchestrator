"""Unit tests for the follow-up records and their deterministic derivations.

This module is reachable with the supervisor layer off, degraded, or absent, so its tests construct
``evaluations`` rows directly and never build a :class:`Supervisor`. The layer's own use of these
functions (the finalize prompt's gate digest, the ``summary.md`` section) is covered in
``test_supervisor.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.follow_ups import (
    FINDING_TITLE_MAX,
    FollowUp,
    _finding_to_follow_up,
    evaluator_finding_follow_ups,
    follow_up_json,
    merge_follow_ups,
    parse_follow_ups,
    render_follow_ups_section,
    render_gate_digest,
)
from wastech_orchestrator.state_store import EvaluationRow

_TASK = "task-1"


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


# -- evaluator findings -> follow-ups -----------------------------------------


def test_evaluator_finding_follow_ups_uses_last_verdict_per_node() -> None:
    rows = [
        _verdict([{"severity": "high", "reason": "blocking issue", "paths": []}], verdict="rework"),
        _verdict([{"severity": "low", "reason": "minor nit remains", "paths": ["a.py"]}]),
        EvaluationRow(task_id=_TASK, kind="supervisor_step", verdict="advisory", node_id=None),
    ]
    fus = evaluator_finding_follow_ups(rows)
    # Only the LAST review verdict's findings — the rework-superseded round is ignored, and the
    # supervisor_step row is not an evaluator verdict.
    assert len(fus) == 1
    assert fus[0].title == "minor nit remains"
    assert fus[0].severity == "low" and fus[0].paths == ("a.py",)
    assert "review" in fus[0].evidence[0]


def test_evaluator_finding_follow_ups_keeps_each_subtask(tmp_path: Path) -> None:
    # A decomposed task runs the same evaluator once per subtask, so the "last verdict per node" key
    # has to include the subtask: keyed on node_id alone, subtask 3's verdict evicted subtasks 1 and
    # 2, and their accepted findings reached no operator surface at all — silently, and worse the
    # more the task was decomposed.
    rows = [
        _verdict([{"severity": "low", "reason": "nit in subtask 1", "paths": []}], subtask_order=1),
        _verdict([{"severity": "low", "reason": "nit in subtask 2", "paths": []}], subtask_order=2),
        _verdict([{"severity": "low", "reason": "superseded", "paths": []}], subtask_order=3),
        _verdict([{"severity": "low", "reason": "nit in subtask 3", "paths": []}], subtask_order=3),
    ]
    titles = [fu.title for fu in evaluator_finding_follow_ups(rows)]
    assert titles == ["nit in subtask 1", "nit in subtask 2", "nit in subtask 3"]
    assert "superseded" not in titles  # the per-(node, subtask) last-verdict rule still holds


def test_finding_to_follow_up_truncates_long_reason_and_drops_empty() -> None:
    long_reason = "x" * 200
    fu = _finding_to_follow_up({"severity": "medium", "reason": long_reason, "paths": []}, "review")
    assert fu is not None
    assert fu.title.endswith("…") and len(fu.title) <= FINDING_TITLE_MAX + 1
    assert fu.rationale == long_reason  # full text preserved when the title is truncated
    # No usable reason, or a non-mapping, yields nothing.
    assert _finding_to_follow_up({"severity": "low", "reason": "", "paths": []}, "review") is None
    assert _finding_to_follow_up("not-a-mapping", "review") is None


def test_evaluator_finding_follow_ups_skips_a_malformed_row() -> None:
    # Advisory by construction: a row whose findings do not parse costs its follow-ups, never the
    # summary. Both shapes a corrupt blob can take are skipped, not raised.
    def row(findings_json: str, node_id: str) -> EvaluationRow:
        return EvaluationRow(
            task_id=_TASK,
            kind="in_flow_verdict",
            verdict="accept",
            findings_json=findings_json,
            node_id=node_id,
        )

    assert evaluator_finding_follow_ups([row("{not json", "a"), row('{"a": 1}', "b")]) == ()


def test_merge_follow_ups_exact_match_dedup() -> None:
    primary = FollowUp("Same issue", "", "medium", evidence=("e",), paths=("p.py",))
    dup = FollowUp("same   ISSUE", "", "low", evidence=("x",), paths=("p.py",))  # normalizes equal
    fresh = FollowUp("Different", "", "low", evidence=("y",), paths=())
    merged = merge_follow_ups((primary,), (dup, fresh))
    assert len(merged) == 2  # the duplicate is dropped, the new one kept
    assert merged[0] is primary  # the supervisor's own list wins on a collision
    assert merged[1].title == "Different"


def test_parse_follow_ups_is_evidence_gated() -> None:
    raw = [
        {"title": "keep", "rationale": "r", "evidence": ["e1"], "severity": "high"},
        {"title": "drop-no-evidence", "rationale": "r", "evidence": [], "severity": "low"},
        {"title": "", "rationale": "r", "evidence": ["e"], "severity": "low"},  # blank title
        "not-a-mapping",
    ]
    parsed = parse_follow_ups(raw)
    assert [f.title for f in parsed] == ["keep"]
    assert parsed[0].evidence == ("e1",)
    assert parse_follow_ups("not-a-list") == ()


# -- rendering (the text that becomes the PR body) ----------------------------


def test_follow_up_json_round_trips_every_field() -> None:
    # One definition of the record shape, because summary.json has two writers.
    fu = FollowUp(
        title="t",
        rationale="why",
        severity="medium",
        evidence=("e1", "e2"),
        paths=("a.py",),
        action_hint="do x",
    )
    assert follow_up_json(fu) == {
        "title": "t",
        "rationale": "why",
        "severity": "medium",
        "paths": ["a.py"],
        "evidence": ["e1", "e2"],
        "action_hint": "do x",
    }
    # An absent hint is recorded as null, not dropped: the key set stays the same on every record.
    assert follow_up_json(FollowUp("t", "", "low", ("e",)))["action_hint"] is None


def test_render_follow_ups_section_shape() -> None:
    section = render_follow_ups_section(
        (
            FollowUp("Bare", "", "low", ("e",)),
            FollowUp("Full", "because", "high", ("e",), ("a.py", "b.py"), "extract a helper"),
        )
    )
    assert section == (
        "## Technical debt / follow-ups\n"
        "\n"
        "- **[low] Bare**\n"
        "- **[high] Full** — because Paths: a.py, b.py. Suggested: extract a helper\n"
    )


def test_render_gate_digest_is_none_without_evaluators() -> None:
    # A flow with no gates gets no section at all, rather than an empty heading.
    assert render_gate_digest([]) is None
    assert render_gate_digest([EvaluationRow(_TASK, "supervisor_step", "advisory")]) is None


def test_render_gate_digest_names_each_verdict_and_its_findings() -> None:
    rows = [
        _verdict([], node_id="fact_verification"),
        _verdict(
            [{"severity": "medium", "reason": "uneven audit depth", "paths": ["report.md"]}],
            node_id="critical_review",
        ),
    ]
    digest = render_gate_digest(rows)
    assert digest == (
        "- fact_verification: verdict `accept`, no findings recorded\n"
        "- critical_review: verdict `accept`, 1 finding(s) recorded:\n"
        "  - [medium] uneven audit depth (report.md)"
    )


def test_render_gate_digest_bounds_a_long_reason_and_labels_subtasks() -> None:
    rows = [
        _verdict(
            [{"severity": "low", "reason": "y\n" * 200, "paths": []}],
            node_id="review",
            subtask_order=2,
        )
    ]
    digest = render_gate_digest(rows)
    assert digest is not None
    header, finding = digest.splitlines()
    assert header.startswith("- review (subtask 2): verdict `accept`, 1 finding(s)")
    assert finding.endswith("…") and len(finding) <= FINDING_TITLE_MAX + len("  - [low] ") + 1


def test_render_gate_digest_keeps_only_each_nodes_final_verdict() -> None:
    rows = [
        _verdict([{"severity": "high", "reason": "sent back", "paths": []}], verdict="rework"),
        _verdict([]),
    ]
    digest = render_gate_digest(rows)
    assert digest == "- review: verdict `accept`, no findings recorded"
    assert "sent back" not in digest
