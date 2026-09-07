"""The deterministic whole-task report — the pull-request body when no prose was written.

One writer for every terminal that has no provider-authored synthesis: the oversight layer is
switched off, the terminal has no prose by design (``failed`` / ``manual_action_required``), or a
synthesis was expected and could not be produced. It replaces a four-field stub whose ``## How``
literally read "No provider-authored summary was available", and which dropped the evaluator
findings a gate had let past — they reached ``summary.json`` and never the pull request.

Rendered from the same :class:`~wastech_orchestrator.core.supervisor_packet.PacketFacts` the
finalize turn is grounded in, so the two bodies cannot disagree about what the run did. Same
determinism contract as the packet: a pure function of ``state.db`` plus the task's
artifacts, so two renders of one run are byte-identical.

Bounded by the run's shape, not by content volume: no per-step message, no observation digest, and
**no diff body**. A pull request already *is* its diff, and inlining it is what once produced a
~580-line committed summary that was almost entirely raw patch. The report names the changed paths
and points at ``logs/<task-id>/current.diff``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.recorder import StepFacts
from wastech_orchestrator.core.follow_ups import (
    FollowUp,
    follow_up_json,
    render_follow_ups_section,
)
from wastech_orchestrator.core.supervisor_packet import (
    PacketFacts,
    split_check_runs,
    summarize_diff,
)
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.state_store import CheckRunRow

SUMMARY_MD_FILENAME = "summary.md"
SUMMARY_JSON_FILENAME = "summary.json"
#: The skipped-nodes heading. Also the idempotency key when the section is appended to a body the
#: oversight layer wrote, so it is stated once and both callers use the same renderer.
SKIPPED_NODES_HEADING = "## Pipeline nodes skipped"

_DEGRADED_CALLOUT = (
    "> ⚠️ **Fallback summary — not a provider-authored synthesis.** The whole-task summary could "
    "not be produced for this run; what follows is the deterministic report derived from the run's "
    "recorded facts.\n"
)


def render_summary_report(
    facts: PacketFacts,
    *,
    follow_ups: tuple[FollowUp, ...],
    gates: str | None,
    skipped_nodes: Iterable[str],
    task_ref: str | None,
    degraded: bool,
) -> str:
    """Render the run's durable facts as the pull-request body.

    Every block below ends with exactly one newline and no trailing blank line, and the blocks are
    joined with a newline — so there is one blank line between sections and a single terminal
    newline, and a new section cannot silently change the spacing of its neighbours.

    A section whose data is empty is **absent**, not an empty heading — except ``## Changes``, which
    is always emitted: on a failed terminal "nothing was changed" is the single most load-bearing
    fact, and dropping the heading would read as "we did not look".

    ``degraded`` is a parameter rather than an inference because three states must read differently:
    the layer was switched off (an ordinary artifact, no warning), the layer was expected and
    produced no prose (the callout belongs), and a terminal that has no prose by design (no
    warning). Only the caller knows which one it is in.
    """
    blocks = [f"# {facts.task_title}\n"]
    if degraded:
        blocks.append(_DEGRADED_CALLOUT)
    context = _context_line(task_ref, facts.flow_name)
    if context:
        blocks.append(context)
    blocks.append(_changes_section(facts))
    if facts.steps:
        blocks.append(_steps_section(facts.steps))
    checks = _checks_section(facts.check_runs)
    if checks:
        blocks.append(checks)
    if gates:
        blocks.append(f"## Gates\n\n{gates}\n")
    if follow_ups:
        blocks.append(render_follow_ups_section(follow_ups))
    skipped = tuple(skipped_nodes)
    if skipped:
        blocks.append(render_skipped_nodes_section(skipped))
    return "\n".join(blocks)


def render_skipped_nodes_section(skipped_nodes: Iterable[str]) -> str:
    """The ``## Pipeline nodes skipped`` block.

    Sorted here rather than by the caller: the skip set is a ``frozenset``, whose iteration order
    varies with the hash seed, so an unsorted render would differ between processes while passing
    every in-process test — and byte-identical output is the contract.
    """
    lines = "\n".join(f"- `{node_id}`" for node_id in sorted(skipped_nodes))
    return f"{SKIPPED_NODES_HEADING}\n\n{lines}\n"


def write_summary_report(
    artifacts_root: str | Path,
    facts: PacketFacts,
    *,
    follow_ups: tuple[FollowUp, ...],
    gates: str | None,
    skipped_nodes: Iterable[str],
    task_ref: str | None,
    degraded: bool,
    supervisor_usage: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """Write the deterministic ``summary.md`` + ``summary.json`` pair; return both paths.

    ``summary.md`` becomes the pull-request body and is committed, so it is written with
    ``newline=""`` — the platform's line separator must not leak into the repository.
    ``summary.json`` is local-only metadata (never committed): the operator's copy of the follow-ups
    plus what the oversight layer spent, which belongs to whoever owns the bill rather than to the
    reviewer reading the change.
    """
    task_dir = task_artifact_dir(artifacts_root, facts.task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_summary_json(
        task_dir,
        what=facts.task_title,
        follow_ups=follow_ups,
        supervisor_usage=supervisor_usage,
        degraded=degraded,
    )
    body = render_summary_report(
        facts,
        follow_ups=follow_ups,
        gates=gates,
        skipped_nodes=skipped_nodes,
        task_ref=task_ref,
        degraded=degraded,
    )
    md_path = task_dir / SUMMARY_MD_FILENAME
    md_path.write_text(body, encoding="utf-8", newline="")
    return str(md_path), str(task_dir / SUMMARY_JSON_FILENAME)


def _write_summary_json(
    task_dir: Path,
    *,
    what: str,
    follow_ups: tuple[FollowUp, ...],
    supervisor_usage: Mapping[str, Any] | None,
    degraded: bool,
) -> None:
    """Write the deterministic ``summary.json``, one key set with the layer's own writer.

    ``summary`` is the provider-authored prose and is empty here by definition — on this path the
    report in ``summary.md`` *is* the artifact. ``supervisor_usage`` is present exactly when the
    layer made calls, so the pair answers both questions on its own: usage present means the layer
    ran, ``degraded`` means it ran and could not finish, and neither present means it never ran.

    Best-effort like the rest of the summary path: a write error costs the metadata, never the
    terminal.
    """
    payload: dict[str, Any] = {"what": what, "summary": ""}
    if supervisor_usage is not None:
        payload["supervisor_usage"] = dict(supervisor_usage)
    if follow_ups:
        payload["follow_ups"] = [follow_up_json(fu) for fu in follow_ups]
    if degraded:
        payload["degraded"] = True
    try:
        (task_dir / SUMMARY_JSON_FILENAME).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        return


def _context_line(task_ref: str | None, flow_name: str | None) -> str:
    """A one-line pointer to the task file and the flow, or ``""`` when neither is known.

    The task file is *named*, never pasted: inlining the description is what bloated the committed
    summary, and the file is a sibling of it in the same commit.
    """
    parts = []
    if task_ref:
        parts.append(f"Task file: `{task_ref}`")
    if flow_name:
        parts.append(f"Flow: `{flow_name}`")
    return f"_{'. '.join(parts)}._\n" if parts else ""


def _count(n: int, singular: str) -> str:
    """``"1 file"`` / ``"3 files"`` — the stat line reads as prose, not as a field dump."""
    return f"{n} {singular}" if n == 1 else f"{n} {singular}s"


def _changes_section(facts: PacketFacts) -> str:
    """The changed paths and the diff stat, with a pointer to the full patch — never the patch."""
    summary = summarize_diff(facts.diff_text)
    if not summary.paths:
        # No pointer: a link to an artifact that does not exist is worse than no link.
        return "## Changes\n\nNo file changes were recorded for this task.\n"
    lines = [
        "## Changes",
        "",
        (
            f"{_count(len(summary.paths), 'file')} changed, "
            f"{_count(summary.insertions, 'insertion')}, {_count(summary.deletions, 'deletion')}"
        ),
        "",
        *(f"- `{path}`" for path in summary.paths),
        "",
        # The committed body names the private artifact, not the exchange copy: the exchange is the
        # provider's read surface and does not outlive the task, while this file is committed.
        f"_Full diff: `logs/{facts.task_id}/current.diff`._",
    ]
    return "\n".join(lines) + "\n"


def _steps_section(steps: Sequence[StepFacts]) -> str:
    """Every executed node in order with what it did — the run's record, not an interpretation."""
    return "\n".join(["## Steps", "", *(_step_line(step) for step in steps)]) + "\n"


def _step_line(step: StepFacts) -> str:
    """One step as a line: where it ran, how it ended, and every deviation it recorded."""
    where = f"`{step.node_id}` ({step.node_kind}"
    if step.subtask_order is not None:
        where += f", subtask {step.subtask_order}"
    where += ")"
    if step.skipped:
        reason = f" — {step.skip_reason}" if step.skip_reason else ""
        return f"- {where}: skipped{reason}"
    parts = [f"- {where}: {step.status or 'unknown'}"]
    if step.outcome:
        parts.append(f" → {step.outcome}")
    if step.provider_used:
        parts.append(f", provider `{step.provider_used}`")
        if step.fallback_from:
            parts.append(f" (fell back from `{step.fallback_from}`)")
    if step.stage_attempts > 1:
        parts.append(f", {step.stage_attempts} attempts")
    if step.error_class:
        parts.append(f", error `{step.error_class}`")
    return "".join(parts)


def _checks_section(check_runs: Sequence[CheckRunRow]) -> str:
    """The check commands and their results, or ``""`` when the task ran none.

    Reduced to the **last** run of each command, the same "earlier rounds are superseded" rule the
    final-verdict-per-evaluator derivation uses. Listing every run would put a command that failed
    and was then fixed under both Failed and Passed, which in the body of a green pull request is
    not a history — it is a contradiction.
    """
    latest = {(row.command, row.subtask_order): row for row in check_runs}
    outcomes = split_check_runs(tuple(latest.values()))
    lines = []
    for label, bucket, note in (
        ("Passed", outcomes.passed, ""),
        ("Failed", outcomes.failed, ""),
        ("Skipped", outcomes.skipped, ", toolchain absent"),
    ):
        commands = tuple(dict.fromkeys(bucket))  # one line per command, not per subtask
        if commands:
            joined = ", ".join(f"`{command}`" for command in commands)
            lines.append(f"- {label} ({len(commands)}{note}): {joined}")
    return "\n".join(["## Checks", "", *lines]) + "\n" if lines else ""
