"""Unit tests for the ledger and the failure report.

The whole-task summary is not here: when no provider authored one it is rendered by
``core/summary_report.py``, covered in ``test_summary_report.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.ledger import (
    COMPLETED_FILENAME,
    INFRA_LOOP,
    DecomposedFailureInfo,
    Ledger,
    LedgerRecord,
    NodeFailureEvidence,
    write_failure_report,
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


def test_governance_changed_field_round_trips(tmp_path: Path) -> None:
    # The completed-ledger record carries the governance/instruction paths a run edited.
    ledger = Ledger(tmp_path)
    ledger.append(
        LedgerRecord(
            id="g",
            title="G",
            final_status="done",
            finished_at="t",
            governance_changed=("AGENTS.md", ".agents/rules/security.md"),
        )
    )
    # The default is empty and serializes to [] (ordinary runs / old records carry no governance).
    ledger.append(LedgerRecord(id="p", title="P", final_status="done", finished_at="t"))
    records = ledger.records()
    assert records[0]["governance_changed"] == ["AGENTS.md", ".agents/rules/security.md"]
    assert records[1]["governance_changed"] == []


def test_has_task_id(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    assert ledger.has_task_id("a") is False
    ledger.append(LedgerRecord(id="a", title="A", final_status="done", finished_at="t1"))
    assert ledger.has_task_id("a") is True
    assert ledger.has_task_id("missing") is False


def test_only_validation_rejects(tmp_path: Path) -> None:
    # An id whose only record carries a validation_reason is a gate reject, not a real attempt.
    ledger = Ledger(tmp_path)
    assert ledger.only_validation_rejects("a") is False  # absent → not a reject-only id
    ledger.append(
        LedgerRecord(
            id="a",
            title="A",
            final_status="failed",
            finished_at="t1",
            validation_reason="injection_suspected",
        )
    )
    assert ledger.only_validation_rejects("a") is True
    # A subsequent real attempt (no validation_reason) makes it no longer reject-only.
    ledger.append(LedgerRecord(id="a", title="A", final_status="failed", finished_at="t2"))
    assert ledger.only_validation_rejects("a") is False


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


def test_the_advanced_mode_marker_round_trips_and_defaults_for_older_records(
    tmp_path: Path,
) -> None:
    """ТA.6.3: the durable record of the run's security posture, readable next to older lines.

    The ledger is append-only and never rewritten, which is exactly why the field needs a default: a
    record written before it existed has to keep reading, and it has to read as "not in the mode" —
    which is what it was. "The key is absent" and "the run was ordinary" must not be the same answer
    for a reader treating this file as evidence, so both forms are pinned here in one file.
    """
    ledger = Ledger(tmp_path)
    ledger.append(LedgerRecord(id="old", title="Old", final_status="done", finished_at="t"))
    ledger.append(
        LedgerRecord(
            id="new", title="New", final_status="done", finished_at="t", advanced_mode=True
        )
    )
    # A line hand-written by an older version, i.e. one that has no such key at all.
    with (tmp_path / COMPLETED_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write('{"id": "ancient", "title": "Ancient", "final_status": "done"}\n')

    old, fresh, ancient = ledger.records()
    assert old["advanced_mode"] is False
    assert fresh["advanced_mode"] is True
    assert ancient.get("advanced_mode", False) is False  # readable, and reads as it was
    assert ledger.has_task_id("ancient")  # the older line is still a record, not a parse casualty


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
    # A fix-loop terminal spent a budget, not a provider: its artifact carries no attempt evidence
    # and gains no orphan heading, so the machine key is present-but-empty rather than absent.
    assert data["provider_attempts"] == []
    assert "## Provider attempts" not in stuck


def test_write_failure_report_infra_names_every_attempt(tmp_path: Path) -> None:
    # An infra terminal exhausted no fix-loop budget, so the opening line must not claim one — and
    # the attempts are the only evidence it has (counters empty, no check log, no findings).
    report_path, stuck_path = write_failure_report(
        tmp_path,
        "task-002",
        loop=INFRA_LOOP,
        limit_name="agent node 'implementation': no provider could complete it (auth_failed)",
        counters={},
        last_check_log=None,
        last_review_findings=None,
        final_diff="",
        failing_node=NodeFailureEvidence(
            node_id="implementation",
            provider_attempts=(
                {
                    "provider": "claude",
                    "attempt": 1,
                    "error_class": "rate_limited",
                    "exit_code": None,
                    "started_at": "2026-08-06T06:36:23+00:00",
                },
                {
                    "provider": "codex",
                    "attempt": 2,
                    "error_class": "authentication_failed",
                    "exit_code": 1,
                    "started_at": "2026-08-06T06:36:40+00:00",
                },
            ),
        ),
    )
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert data["node_id"] == "implementation"
    assert [a["provider"] for a in data["provider_attempts"]] == ["claude", "codex"]
    assert data["provider_attempts"][0]["error_class"] == "rate_limited"

    stuck = Path(stuck_path).read_text(encoding="utf-8")
    assert stuck.startswith("# Task task-002 stuck\n\nThis task could not run: agent node")
    assert "fix loop exhausted" not in stuck
    assert "## Provider attempts" in stuck
    assert "claude · attempt 1 · rate_limited · exit n/a · 2026-08-06T06:36:23+00:00" in stuck
    assert "codex · attempt 2 · authentication_failed · exit 1 · 2026-08-06T06:36:40+00:00" in stuck


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
