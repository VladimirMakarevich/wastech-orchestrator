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
    FOLLOW_UPS_FILENAME,
    FollowUp,
    _finding_to_follow_up,
    _split_reason,
    append_task_follow_ups,
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
    # One unbroken token past the bound has no boundary to cut on, so the old ellipsis truncation is
    # the only option left and the full text still reaches the operator via the rationale.
    long_reason = "x" * 200
    fu = _finding_to_follow_up({"severity": "medium", "reason": long_reason, "paths": []}, "review")
    assert fu is not None
    assert fu.title.endswith("…") and len(fu.title) <= FINDING_TITLE_MAX + 1
    assert fu.rationale == long_reason
    # No usable reason, or a non-mapping, yields nothing.
    assert _finding_to_follow_up({"severity": "low", "reason": "", "paths": []}, "review") is None
    assert _finding_to_follow_up("not-a-mapping", "review") is None


def test_split_reason_gives_a_title_that_is_not_its_own_rationale() -> None:
    # The title used to be reason[:120] + "…" with the WHOLE reason repeated as the rationale, so
    # every long finding arrived as a mid-word truncation of the text printed right beside it — a
    # queue whose titles duplicate their own bodies cannot be triaged without opening every item.
    short = "The gate is not enforced."
    assert _split_reason(short) == (short, "")  # short reasons are unchanged: title only

    sentence = (
        "The checklist pairing is not actually enforced. `testing.md` claims the inventory is "
        "checked, but no test asserts it, so a stale entry ships silently and the reviewer trusts "
        "a guarantee that does not exist."
    )
    title, rationale = _split_reason(sentence)
    assert title == "The checklist pairing is not actually enforced."
    assert rationale.startswith("`testing.md` claims")
    assert title not in rationale  # not a prefix of its own body
    assert "…" not in title

    # No sentence boundary in range → cut on the last word boundary, remainder to the rationale.
    no_stop = " ".join(f"w{n:03d}" for n in range(40))
    title, rationale = _split_reason(no_stop)
    assert len(title) <= FINDING_TITLE_MAX
    assert title.split()[-1] == "w023" and rationale.split()[0] == "w024"  # never mid-word
    assert title not in rationale
    assert f"{title} {rationale}" == no_stop  # nothing dropped in the split


def test_evaluator_fix_becomes_the_action_hint() -> None:
    # The reviewer's `fix` is where the remedy lives. It used to be dropped by the typed
    # projection, so every mechanically derived follow-up reached the operator without its fix
    # (measured: null on all 98 follow-ups of a 20-run campaign).
    row = _verdict(
        [
            {
                "severity": "low",
                "reason": "the manifest is not asserted",
                "paths": ["a.py"],
                "fix": "assert the manifest in tests/test_a.py",
            }
        ]
    )
    (fu,) = evaluator_finding_follow_ups([row])
    assert fu.action_hint == "assert the manifest in tests/test_a.py"
    assert "Suggested: assert the manifest" in render_follow_ups_section((fu,))
    # A row without a usable fix keeps action_hint absent rather than empty.
    (no_fix,) = evaluator_finding_follow_ups(
        [_verdict([{"severity": "low", "reason": "x", "fix": "  "}])]
    )
    assert no_fix.action_hint is None


def test_persisted_severity_is_already_normalized_so_nothing_is_downgraded() -> None:
    # The evaluator collapses `blocking`/`critical` into `high` at write time (the `gating` flag
    # carries what that loses), so the mapping never sees those tokens and the `else "medium"`
    # branch below is reachable only for a malformed row — where it errs UPWARD, never down.
    rows = [_verdict([{"severity": "high", "reason": "was critical", "gating": True}])]
    (fu,) = evaluator_finding_follow_ups(rows)
    assert fu.severity == "high"
    assert "rework budget exhausted" in fu.evidence[0]  # the gating distinction survives
    malformed = _finding_to_follow_up({"reason": "no severity key"}, "review")
    assert malformed is not None and malformed.severity == "medium"


def test_gating_finding_on_a_rework_verdict_is_not_a_follow_up() -> None:
    # It gated, so it went through the fix loop; repeating it as technical debt would describe work
    # that was done. The row's own verdict is what says the loop is still open.
    rows = [
        _verdict(
            [{"severity": "high", "reason": "sent back", "paths": [], "gating": True}],
            verdict="rework",
        )
    ]
    assert evaluator_finding_follow_ups(rows) == ()


def test_let_past_finding_becomes_a_follow_up() -> None:
    rows = [_verdict([{"severity": "medium", "reason": "sub-threshold", "gating": False}])]
    (fu,) = evaluator_finding_follow_ups(rows)
    assert fu.title == "sub-threshold"
    assert fu.evidence == ("review evaluator finding (accepted with findings)",)


def test_open_gating_finding_on_an_accept_is_included_with_its_own_evidence() -> None:
    # A gating finding inside a FINAL accept is a non-blocking evaluator that spent its rework
    # budget and continued anyway (rework_exhausted). Losing it is the worst outcome of the filter,
    # so it is included — but not worded like an ordinary sub-threshold nit.
    rows = [_verdict([{"severity": "high", "reason": "still broken", "gating": True}])]
    (fu,) = evaluator_finding_follow_ups(rows)
    assert fu.title == "still broken"
    assert fu.evidence == ("review evaluator finding still open — rework budget exhausted",)
    assert "accepted with findings" not in fu.evidence[0]


def test_row_without_the_gating_key_reads_as_let_past() -> None:
    # The benign default, stated explicitly: an absent flag costs an extra follow-up, never a lost
    # one. This is also the shape every row written before the flag existed has.
    rows = [_verdict([{"severity": "low", "reason": "no flag recorded"}], verdict="rework")]
    (fu,) = evaluator_finding_follow_ups(rows)
    assert fu.title == "no flag recorded"


def test_mixed_row_keeps_only_the_let_past_findings() -> None:
    rows = [
        _verdict(
            [
                {"severity": "high", "reason": "gated", "gating": True},
                {"severity": "low", "reason": "let past", "gating": False},
            ],
            verdict="rework",
        )
    ]
    assert [fu.title for fu in evaluator_finding_follow_ups(rows)] == ["let past"]


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


# -- the accumulating .worc/follow-ups.md -------------------------------------


def _append(path: Path, task_id: str, *titles: str) -> None:
    append_task_follow_ups(
        path,
        task_id=task_id,
        task_title=f"Title of {task_id}",
        finished_at="2026-08-06T10:00:00+00:00",
        follow_ups=tuple(FollowUp(t, "", "low", ("e",)) for t in titles),
    )


def test_append_task_follow_ups_accumulates_across_tasks(tmp_path: Path) -> None:
    # The point of the file: ten tasks with three findings each leave thirty entries. Nothing is
    # rewritten, so every task's block survives every later append and the explanatory header — the
    # file's only self-documentation, since there is no CLI for it — is written exactly once.
    path = tmp_path / FOLLOW_UPS_FILENAME
    _append(path, "task-a", "first", "second")
    _append(path, "task-b", "third")
    _append(path, "task-c", "fourth")

    text = path.read_text("utf-8")
    assert text.count("# Follow-ups the orchestrator did not fix") == 1
    assert [line for line in text.splitlines() if line.startswith("## ")] == [
        "## task-a — Title of task-a",
        "## task-b — Title of task-b",
        "## task-c — Title of task-c",
    ]
    assert text.count("- **[low] ") == 4
    assert "Finished 2026-08-06T10:00:00+00:00." in text
    # Each block is separated from the previous one by a blank line, so the Markdown is readable
    # after an arbitrary number of appends rather than only after the first.
    assert "\n\n## task-b" in text


def test_append_task_follow_ups_repeats_an_item_two_tasks_both_found(tmp_path: Path) -> None:
    # Dedup is within a task only (`merge_follow_ups`, upstream). There is deliberately no
    # cross-task dedup here: the file is never read back, because a writer that reconciles would
    # silently undo the operator's deletions — and deleting an entry is the only way to close one.
    path = tmp_path / FOLLOW_UPS_FILENAME
    _append(path, "task-a", "the same debt")
    _append(path, "task-b", "the same debt")
    assert path.read_text("utf-8").count("- **[low] the same debt**") == 2


def test_append_task_follow_ups_writes_nothing_without_follow_ups(tmp_path: Path) -> None:
    # A task with no follow-ups leaves no empty section — and no file at all, so the file's
    # existence means it has something in it.
    path = tmp_path / FOLLOW_UPS_FILENAME
    _append(path, "task-clean")
    assert not path.exists()
    # Nor does it touch a file later tasks created.
    _append(path, "task-a", "real debt")
    before = path.read_bytes()
    _append(path, "task-clean-2")
    assert path.read_bytes() == before


def test_append_task_follow_ups_writes_lf_on_every_host(tmp_path: Path) -> None:
    # The daemon may run on any host; the file must not change line endings with it.
    path = tmp_path / FOLLOW_UPS_FILENAME
    _append(path, "task-a", "first")
    _append(path, "task-b", "second")
    assert b"\r\n" not in path.read_bytes()


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
