"""Completed-tasks ledger + failure report + minimal summary (spec §10).

The ledger is an **append-only** file (``logs/completed.jsonl``) outside SQLite: one JSON record per
terminal transition (``done`` / ``failed`` / ``manual_action_required``), never rewritten. SQLite
remains the authoritative state; the ledger is a convenience index of what has been done, and the
duplicate-id source for the §19 gate.

This module also writes the two stuck artifacts — ``failure_report.json`` (machine) and ``stuck.md``
(human) — and the deterministic minimal summary the Core falls back to when no provider can produce
the ``summary`` stage (§5.2).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wastech_orchestrator.providers.artifacts import task_artifact_dir

COMPLETED_FILENAME = "completed.jsonl"
FAILURE_REPORT_FILENAME = "failure_report.json"
STUCK_FILENAME = "stuck.md"
SUMMARY_MD_FILENAME = "summary.md"
SUMMARY_JSON_FILENAME = "summary.json"


@dataclass(frozen=True)
class LedgerRecord:
    """One terminal-transition record (§10). Fields beyond the core set apply when relevant."""

    id: str
    title: str
    final_status: str
    finished_at: str
    branch: str | None = None
    pr_url: str | None = None
    # Auto-merge audit (§ auto-merge bypass): ``auto_merged`` is true iff the orchestrator merged
    # the PR without review; ``merge_outcome`` is the merge SHA, "merged", or "armed" (native
    # --auto). Part of the append-only, tamper-evident audit trail.
    auto_merged: bool = False
    merge_outcome: str | None = None
    fix_iterations: int = 0
    terminal_cleanup: str | None = None
    failure_report: str | None = None
    validation_reason: str | None = None
    decomposed: bool = False
    subtask_count: int | None = None
    subtasks_completed: int | None = None
    # Re-run linkage (``rerun`` command): ``attempt`` is 1 for the original run and increments per
    # re-run; ``rerun_of`` is the task id this record re-attempts (set only when ``attempt`` > 1) so
    # the failure → retry chain is auditable. Old records omit both keys harmlessly.
    attempt: int = 1
    rerun_of: str | None = None
    # Operator-finalized marker (``finalize`` command): ``manual`` is true for a record the operator
    # recorded out-of-band (vs. a pipeline-produced one); ``outcome`` distinguishes a deliberately
    # dropped task (``"abandoned"``) from a plain failure; ``note`` carries the operator's reason.
    # Old records omit all three harmlessly.
    manual: bool = False
    note: str | None = None
    outcome: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "final_status": self.final_status,
            "auto_merged": self.auto_merged,
            "merge_outcome": self.merge_outcome,
            "fix_iterations": self.fix_iterations,
            "terminal_cleanup": self.terminal_cleanup,
            "finished_at": self.finished_at,
            "failure_report": self.failure_report,
            "validation_reason": self.validation_reason,
            "decomposed": self.decomposed,
            "subtask_count": self.subtask_count,
            "subtasks_completed": self.subtasks_completed,
            "attempt": self.attempt,
            "rerun_of": self.rerun_of,
            "manual": self.manual,
            "note": self.note,
            "outcome": self.outcome,
        }


class Ledger:
    """Append-only access to ``logs/completed.jsonl``."""

    def __init__(self, logs_root: str | Path) -> None:
        # ``logs_root`` is the directory that holds ``completed.jsonl`` and the per-task dirs — i.e.
        # ``<artifacts_root>/logs``.
        self._path = Path(logs_root) / COMPLETED_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: LedgerRecord) -> None:
        """Append exactly one record. Created on first use; never rewritten (§10)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_json(), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def records(self) -> list[dict[str, Any]]:
        """Read all records (skipping blank lines). Used for recovery/inspection and tests."""
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def has_task_id(self, task_id: str) -> bool:
        """True iff ``task_id`` appears in the ledger (the §19 duplicate-id ledger half)."""
        return any(rec.get("id") == task_id for rec in self.records())


@dataclass(frozen=True)
class DecomposedFailureInfo:
    """Extra failure-report fields for a decomposed task (§10)."""

    subtask_count: int
    subtasks_completed: int
    failing_subtask: int
    committed_shas: tuple[str, ...] = field(default_factory=tuple)


def write_failure_report(
    artifacts_root: str | Path,
    task_id: str,
    *,
    loop: str,
    limit_name: str,
    counters: Mapping[str, int],
    last_check_log: str | None,
    last_review_findings: Sequence[Mapping[str, Any]] | None,
    final_diff: str,
    decomposed: DecomposedFailureInfo | None = None,
    node_id: str | None = None,
) -> tuple[str, str]:
    """Write ``failure_report.json`` + ``stuck.md``; return both paths (§8.1, §10).

    Flow-neutral (flow-engine P1.2): the base fields (``task_id``/``node_id``/``loop``/``counters``)
    are always written; the implementation-specific sections (``last_check_log``,
    ``last_review_findings``, ``final_diff``) stay empty when the flow has no such nodes.
    """
    task_dir = task_artifact_dir(artifacts_root, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "task_id": task_id,
        "node_id": node_id,
        "loop": loop,
        "limit_exhausted": limit_name,
        "counters": dict(counters),
        "last_check_log": last_check_log,
        "last_review_findings": [dict(f) for f in (last_review_findings or [])],
        "final_diff": final_diff,
    }
    if decomposed is not None:
        report["decomposed"] = {
            "subtask_count": decomposed.subtask_count,
            "subtasks_completed": decomposed.subtasks_completed,
            "failing_subtask": decomposed.failing_subtask,
            "committed_shas": list(decomposed.committed_shas),
        }

    report_path = task_dir / FAILURE_REPORT_FILENAME
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    findings_lines = "\n".join(f"- {f.get('title', f)}" for f in (last_review_findings or []))
    decomposed_md = ""
    if decomposed is not None:
        decomposed_md = (
            f"\n## Decomposition\n\n"
            f"- failing subtask: {decomposed.failing_subtask} of {decomposed.subtask_count}\n"
            f"- subtasks committed: {decomposed.subtasks_completed} "
            f"({', '.join(decomposed.committed_shas) or 'none'})\n"
        )
    stuck_md = (
        f"# Task {task_id} stuck\n\n"
        f"The **{loop}** fix loop exhausted its limit (`{limit_name}`).\n\n"
        f"## Counters\n\n"
        + "\n".join(f"- {k}: {v}" for k, v in counters.items())
        + "\n"
        + decomposed_md
        + f"\n## Last failing check output\n\n```\n{last_check_log or '(none)'}\n```\n"
        + f"\n## Last blocking review findings\n\n{findings_lines or '(none)'}\n"
        + f"\n## Final diff\n\n```diff\n{final_diff}\n```\n"
    )
    stuck_path = task_dir / STUCK_FILENAME
    stuck_path.write_text(stuck_md, encoding="utf-8")
    return str(report_path), str(stuck_path)


def write_minimal_summary(
    artifacts_root: str | Path,
    task_id: str,
    *,
    title: str,
    diff_stat: str,
    task_ref: str | None = None,
) -> tuple[str, str]:
    """Write a *compact* deterministic ``summary.md`` + ``summary.json`` (§5.2).

    The Core's fallback when no provider can produce the ``summary`` stage — a reviewed, passing
    change is never blocked by the prose step. Deliberately small: it links to the task file and
    shows a ``git diff --stat`` (files + line counts) instead of inlining the full task description
    and the entire patch. Inlining bloated the committed summary (a real run produced a ~580-line
    file that was almost all raw diff) and risked an unredacted diff landing in git; the full,
    already-redacted patch stays in ``logs/<task-id>/current.diff``.

    ``task_ref`` is a short pointer to the task file (e.g. ``<id>.md``, a sibling of the committed
    summary); when ``None`` a generic line is used.
    """
    task_dir = task_artifact_dir(artifacts_root, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    what = title
    how = "No provider-authored summary was available; see the changed-files summary below."
    integration = "Derived deterministically from the task and the committed diff stat."
    why = (
        f"See the task file `{task_ref}` for the full description."
        if task_ref
        else "See the task file for the full description."
    )

    summary_json = {"what": what, "how": how, "integration": integration, "why": why}
    json_path = task_dir / SUMMARY_JSON_FILENAME
    json_path.write_text(
        json.dumps(summary_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    changes = diff_stat.strip() or "(no committed changes)"
    md = (
        f"# {what}\n\n"
        f"## What\n\n{what}\n\n"
        f"## How\n\n{how}\n\n"
        f"## Integration\n\n{integration}\n\n"
        f"## Why\n\n{why}\n\n"
        f"## Changes\n\n```\n{changes}\n```\n\n"
        f"_Full diff: `logs/{task_id}/current.diff`._\n"
    )
    md_path = task_dir / SUMMARY_MD_FILENAME
    md_path.write_text(md, encoding="utf-8")
    return str(md_path), str(json_path)
