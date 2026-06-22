"""Unit tests for the ledger, failure report, and minimal summary."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.ledger import (
    DecomposedFailureInfo,
    Ledger,
    LedgerRecord,
    write_failure_report,
    write_minimal_summary,
)


def test_append_is_append_only(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(
        LedgerRecord(id="a", title="A", final_status="done", finished_at="t1", pr_url="http://pr/1")
    )
    ledger.append(LedgerRecord(id="b", title="B", final_status="failed", finished_at="t2"))
    records = ledger.records()
    assert [r["id"] for r in records] == ["a", "b"]
    assert records[0]["final_status"] == "done"
    assert records[0]["pr_url"] == "http://pr/1"
    # The file holds exactly two lines (append-only, never rewritten).
    assert len(ledger.path.read_text(encoding="utf-8").splitlines()) == 2


def test_has_task_id(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    assert ledger.has_task_id("a") is False
    ledger.append(LedgerRecord(id="a", title="A", final_status="done", finished_at="t1"))
    assert ledger.has_task_id("a") is True
    assert ledger.has_task_id("missing") is False


def test_records_empty_when_no_file(tmp_path: Path) -> None:
    assert Ledger(tmp_path / "logs").records() == []


def test_rerun_linkage_round_trips(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(
        LedgerRecord(
            id="a", title="A", final_status="done", finished_at="t2", attempt=2, rerun_of="a"
        )
    )
    rec = ledger.records()[0]
    assert rec["attempt"] == 2
    assert rec["rerun_of"] == "a"


def test_records_tolerate_missing_rerun_keys(tmp_path: Path) -> None:
    # A record written before the rerun fields existed omits the keys; reading is unaffected, and a
    # fresh record defaults to attempt 1 / no rerun_of.
    ledger = Ledger(tmp_path)
    ledger.append(LedgerRecord(id="a", title="A", final_status="done", finished_at="t"))
    rec = ledger.records()[0]
    assert rec["attempt"] == 1
    assert rec["rerun_of"] is None


def test_finalize_marker_round_trips(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(
        LedgerRecord(
            id="a",
            title="A",
            final_status="manual_action_required",
            finished_at="t",
            manual=True,
            note="dropped, obsolete",
            outcome="abandoned",
        )
    )
    rec = ledger.records()[0]
    assert rec["manual"] is True
    assert rec["note"] == "dropped, obsolete"
    assert rec["outcome"] == "abandoned"


def test_pipeline_record_defaults_to_not_manual(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(LedgerRecord(id="a", title="A", final_status="done", finished_at="t"))
    rec = ledger.records()[0]
    assert rec["manual"] is False
    assert rec["note"] is None and rec["outcome"] is None


def test_decomposition_fields_in_record(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(
        LedgerRecord(
            id="big",
            title="Big",
            final_status="manual_action_required",
            finished_at="t",
            decomposed=True,
            subtask_count=3,
            subtasks_completed=1,
            failure_report="logs/big/failure_report.json",
        )
    )
    rec = ledger.records()[0]
    assert rec["decomposed"] is True
    assert rec["subtask_count"] == 3
    assert rec["subtasks_completed"] == 1
    assert rec["failure_report"].endswith("failure_report.json")


def test_write_failure_report(tmp_path: Path) -> None:
    report_path, stuck_path = write_failure_report(
        tmp_path,
        "task-001",
        loop="review",
        limit_name="max_total_fix_iterations",
        counters={"fix_iterations": 5, "review_fix_cycles": 2},
        last_check_log="FAILED: 1 test",
        last_review_findings=[{"title": "missing null check", "severity": "blocking"}],
        final_diff="diff --git a b",
    )
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert data["limit_exhausted"] == "max_total_fix_iterations"
    assert data["counters"]["fix_iterations"] == 5
    assert data["last_review_findings"][0]["title"] == "missing null check"
    stuck = Path(stuck_path).read_text(encoding="utf-8")
    assert "review" in stuck
    assert "max_total_fix_iterations" in stuck


def test_write_failure_report_decomposed(tmp_path: Path) -> None:
    report_path, _ = write_failure_report(
        tmp_path,
        "big",
        loop="test",
        limit_name="max_fix_cycles",
        counters={"fix_iterations": 4},
        last_check_log=None,
        last_review_findings=None,
        final_diff="",
        decomposed=DecomposedFailureInfo(
            subtask_count=3, subtasks_completed=1, failing_subtask=2, committed_shas=("abc",)
        ),
    )
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert data["decomposed"]["failing_subtask"] == 2
    assert data["decomposed"]["committed_shas"] == ["abc"]


def test_write_minimal_summary(tmp_path: Path) -> None:
    md_path, json_path = write_minimal_summary(
        tmp_path,
        "task-001",
        title="Add validation",
        diff_stat=" src/app.py | 4 +++-\n 1 file changed, 3 insertions(+), 1 deletion(-)",
        task_ref="task-001.md",
    )
    summary = json.loads(Path(json_path).read_text(encoding="utf-8"))
    # The summary.json contract keeps exactly these four keys.
    assert set(summary) == {"what", "how", "integration", "why"}
    assert summary["what"] == "Add validation"
    assert "task-001.md" in summary["why"]  # links to the task file, never the pasted description
    md = Path(md_path).read_text(encoding="utf-8")
    assert "## What" in md and "## Why" in md
    assert "## Changes" in md and "1 file changed" in md
    assert "logs/task-001/current.diff" in md  # pointer to the full (redacted) patch
    assert md_path.endswith("summary.md")


def test_minimal_summary_is_compact_and_inlines_no_patch(tmp_path: Path) -> None:
    """The fallback must stay small: no full diff body, no pasted task description."""
    md_path, _ = write_minimal_summary(
        tmp_path,
        "task-002",
        title="Big change",
        diff_stat=" a.py | 2 +-\n 1 file changed",
        task_ref="task-002.md",
    )
    md = Path(md_path).read_text(encoding="utf-8")
    assert "diff --git" not in md and "@@" not in md  # no raw patch body inlined
    assert md.count("\n") < 30  # compact, unlike the old ~580-line fallback


def test_minimal_summary_without_task_ref(tmp_path: Path) -> None:
    md_path, _ = write_minimal_summary(tmp_path, "t", title="X", diff_stat="")
    md = Path(md_path).read_text(encoding="utf-8")
    assert "See the task file for the full description." in md
    assert "(no committed changes)" in md
