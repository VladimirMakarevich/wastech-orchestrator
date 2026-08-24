"""Follow-up records and the deterministic derivations that produce them.

A follow-up is one evidence-gated technical-debt note that reaches the operator through
``summary.json`` and the ``## Technical debt / follow-ups`` section of the pull-request body. Two
sources feed it: the supervisor layer's own finalize turn (parsed by :func:`parse_follow_ups`) and
the evaluator findings a gate **let past** (derived by :func:`evaluator_finding_follow_ups`).

The second source is why this module exists on its own. Deriving follow-ups from the ``evaluations``
table is a pure function of durable state — no provider call, no LLM — so it must not sit inside the
supervisor layer, where it would die whenever the layer did not run: a task whose finalize turn
produced no prose would keep its accepted findings in ``summary.json`` and lose them from the
pull-request body. Here the derivation is reachable with the layer off, degraded, or absent, and an
import-linter contract keeps it from growing a dependency back on it.

Bounded on purpose: only each evaluator node's FINAL verdict is read (an intermediate rework round's
finding was fixed, and repeating it would describe work that was done), and a finding's reason is
cut at :data:`FINDING_TITLE_MAX` so a chatty evaluator cannot inflate the body.

The third sink is this module's only I/O: :func:`append_task_follow_ups` accumulates each finished
task's list into ``.worc/follow-ups.md``. The two surfaces above answer "what did *this* task leave
behind?"; ten tasks waving three findings past the gate each leave thirty items the operator would
otherwise collect by hand from thirty pull requests and from ``summary.json`` files a
``worc logs clean`` deletes. That file answers "what has this orchestrator not fixed in this
repository?" and is append-only on purpose — see the function.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wastech_orchestrator.state_store import EvaluationRow

# Longest a finding's reason may be before it is used verbatim as a follow-up title; a longer reason
# is split by :func:`_split_reason` into a title that ends on a real boundary and a rationale
# carrying only the remainder. The same bound caps a finding line in the observation prompt and in
# the gate digest, so a chatty evaluator cannot inflate every step turn.
FINDING_TITLE_MAX = 120

# Heading of the follow-ups section in ``summary.md`` (= the pull-request body). Named because the
# reused-chain-PR compactor has to find this section to keep it while eliding the prose around it,
# and it lives in an adapter that must not import the Core (a test pins the two spellings equal).
FOLLOW_UPS_HEADING = "## Technical debt / follow-ups"

# The accumulating file, at the root of the control home (``<repo>/.worc/``) rather than under
# ``logs/``: ``worc logs clean`` sweeps that root by shape, so a file placed there would be deleted
# with the artifacts it outlives.
FOLLOW_UPS_FILENAME = "follow-ups.md"

# Written once, when the file is created: there is no CLI surface for this file at all, so its own
# first lines are the only place it can explain itself. Concatenated rather than triple-quoted so
# the paragraph lands as ONE unwrapped line, the way every Markdown file here is written.
_FOLLOW_UPS_FILE_HEADER = (
    "# Follow-ups the orchestrator did not fix\n"
    "\n"
    "One section per finished task, appended as it finishes. Nothing rewrites, regenerates, or "
    "reconciles this file: the same item found by two tasks is listed twice, and **deleting an "
    "entry is how you close it**. Gitignored with the rest of `.worc/`, so it never reaches a "
    "commit or a pull-request diff.\n"
)


@dataclass(frozen=True)
class FollowUp:
    """One evidence-gated technical-debt / follow-up record. Minimal and grounded."""

    title: str
    rationale: str
    severity: str
    evidence: tuple[str, ...]
    paths: tuple[str, ...] = ()
    action_hint: str | None = None


def parse_follow_ups(raw: Any) -> tuple[FollowUp, ...]:
    """Parse the finalize turn's ``follow_ups`` array defensively — **evidence-gated**.

    Best-effort, mirroring ``_parse_skill_map``: a non-list yields ``()`` and any record without
    a non-empty ``title`` or ``evidence`` is dropped (never raised), so an ungrounded "refactor
    idea" the model invented cannot reach ``summary.{json,md}``.
    """
    if not isinstance(raw, list):
        return ()
    out: list[FollowUp] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        evidence = item.get("evidence")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(evidence, list):
            continue
        ev = tuple(e.strip() for e in evidence if isinstance(e, str) and e.strip())
        if not ev:  # evidence-gated: no evidence → dropped
            continue
        rationale = item.get("rationale")
        severity = item.get("severity")
        paths = item.get("paths")
        action_hint = item.get("action_hint")
        out.append(
            FollowUp(
                title=title.strip(),
                rationale=rationale if isinstance(rationale, str) else "",
                severity=severity if severity in ("low", "medium", "high") else "medium",
                evidence=ev,
                paths=tuple(p.strip() for p in paths if isinstance(p, str) and p.strip())
                if isinstance(paths, list)
                else (),
                action_hint=action_hint.strip()
                if isinstance(action_hint, str) and action_hint.strip()
                else None,
            )
        )
    return tuple(out)


def follow_up_json(follow_up: FollowUp) -> dict[str, Any]:
    """One follow-up as the ``summary.json`` record shape.

    Stated once because ``summary.json`` has two writers — the supervisor layer's finalize and the
    deterministic report — and a record shape defined twice is a contract that drifts silently.
    """
    return {
        "title": follow_up.title,
        "rationale": follow_up.rationale,
        "severity": follow_up.severity,
        "paths": list(follow_up.paths),
        "evidence": list(follow_up.evidence),
        "action_hint": follow_up.action_hint,
    }


# Boundaries a title may end on, each with its trailing space: :func:`_split_reason` collapses all
# whitespace before searching, so a newline-terminated variant would never match.
_SENTENCE_ENDS = (". ", "? ", "! ", "; ")


def _split_reason(reason: str) -> tuple[str, str]:
    """Split one finding's reason into a standalone ``(title, rationale)``.

    A short reason is its own title with no rationale. A long one is cut at the LAST sentence
    boundary that still fits the bound, else at the last word boundary, and the rationale carries
    only what is left over — so the title never repeats the body and never cuts mid-word. Both
    matter for triage: a blind ``reason[:FINDING_TITLE_MAX]`` gives every long finding a mid-word
    title that duplicates the text printed right next to it, and a queue like that cannot be triaged
    without opening each item.
    """
    reason = " ".join(reason.split())
    if len(reason) <= FINDING_TITLE_MAX:
        return reason, ""
    head = reason[: FINDING_TITLE_MAX + 1]
    cut = max((head.rfind(end) + 1 for end in _SENTENCE_ENDS if end in head), default=-1)
    if cut < 1:
        cut = head.rfind(" ")
    if cut < 1:  # one unbroken token longer than the bound — the old behavior is the only option
        return reason[:FINDING_TITLE_MAX].rstrip() + "…", reason
    return reason[:cut].rstrip(), reason[cut:].lstrip()


def _finding_to_follow_up(
    finding: Any, node_id: str | None, *, rework_exhausted: bool = False
) -> FollowUp | None:
    """Map one persisted evaluator finding (``{severity, reason, paths, fix}``) to a
    :class:`FollowUp`, or ``None`` when it carries no usable ``reason``.

    The reviewer's own ``fix`` becomes the ``action_hint``: it is where the remedy lives, and
    without it every mechanically derived follow-up reached the operator as a problem with no
    proposed solution. ``rework_exhausted`` marks the finding that gated and was still open when a
    non-blocking evaluator ran out of rework budget. It gets its own evidence line, so an operator
    reading the list can tell "below the gate, noted" from "above the gate, not fixed".
    """
    if not isinstance(finding, Mapping):
        return None
    reason = str(finding.get("reason") or "").strip()
    if not reason:
        return None
    severity = finding.get("severity")
    paths_raw = finding.get("paths")
    paths = (
        tuple(str(p).strip() for p in paths_raw if str(p).strip())
        if isinstance(paths_raw, list)
        else ()
    )
    fix = finding.get("fix")
    title, rationale = _split_reason(reason)
    where = node_id or "review"
    evidence = (
        f"{where} evaluator finding still open — rework budget exhausted"
        if rework_exhausted
        else f"{where} evaluator finding (accepted with findings)"
    )
    return FollowUp(
        title=title,
        rationale=rationale,
        # The persisted severity is already the evaluator's normalized ``low``/``medium``/``high``
        # projection (``blocking``/``critical`` collapse into ``high`` at write time, with the
        # ``gating`` flag carrying what that collapse loses), so this branch only catches a
        # malformed row — where erring upward is the safe direction.
        severity=severity if severity in ("low", "medium", "high") else "medium",
        evidence=(evidence,),
        paths=paths,
        action_hint=fix.strip() if isinstance(fix, str) and fix.strip() else None,
    )


def _last_verdict_per_node(
    evaluations: Sequence[EvaluationRow],
) -> dict[tuple[str | None, int | None], EvaluationRow]:
    """Each evaluator node's FINAL in-flow verdict row (earlier rework rounds are superseded).

    ``get_evaluations`` is insertion-ordered, so the last row seen per key is that node's final
    verdict. Keyed by ``(node_id, subtask_order)``, not by node alone: a decomposed task runs the
    same evaluator once per subtask, so keying on the node id let subtask N's verdict evict every
    earlier subtask's — silently, and worse the more the task was decomposed.
    """
    last_by_node: dict[tuple[str | None, int | None], EvaluationRow] = {}
    for row in evaluations:
        if row.kind == "in_flow_verdict":
            last_by_node[(row.node_id, row.subtask_order)] = row
    return last_by_node


def _row_findings(row: EvaluationRow) -> list[Mapping[str, Any]]:
    """The persisted ``{severity, reason, paths}`` findings of one verdict row (``[]`` if unusable).

    Defensive on purpose: this is an advisory layer, so a malformed row is skipped, never raised.
    """
    try:
        findings = json.loads(row.findings_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(findings, list):
        return []
    return [f for f in findings if isinstance(f, Mapping)]


def evaluator_finding_follow_ups(evaluations: Sequence[EvaluationRow]) -> tuple[FollowUp, ...]:
    """Follow-ups from the LAST in-flow verdict per evaluator node — the findings a gate let past.

    An evaluator that accepts *with* findings persists them to the ``evaluations`` table and they
    otherwise reach no operator surface, so they are converted here and land in
    ``summary.{json,md}`` and the pull-request body.

    A finding that **gated** is excluded: it has already been through rework, so repeating it as
    technical debt would describe work that was done. The one exception is a gating finding sitting
    in a final ``accept`` — that is a non-blocking evaluator that spent its per-instance rework
    budget and continued with the issue still open, which is the single most important thing to
    carry into the pull request. It is included with its own evidence line so it does not read as an
    ordinary sub-threshold nit.

    A row written without the ``gating`` key reads as not-gating, so the error lands on the benign
    side: an extra follow-up, never a lost one.
    """
    out: list[FollowUp] = []
    for (node_id, _subtask), row in _last_verdict_per_node(evaluations).items():
        for finding in _row_findings(row):
            gating = bool(finding.get("gating"))
            if gating and row.verdict != "accept":
                continue
            follow_up = _finding_to_follow_up(finding, node_id, rework_exhausted=gating)
            if follow_up is not None:
                out.append(follow_up)
    return tuple(out)


def _follow_up_key(follow_up: FollowUp) -> tuple[str, tuple[str, ...]]:
    """Exact-match dedup key for a follow-up: its normalized text plus its paths."""
    text = " ".join(f"{follow_up.title} {follow_up.rationale}".lower().split())
    return (text, tuple(sorted(follow_up.paths)))


def merge_follow_ups(
    primary: tuple[FollowUp, ...], extra: tuple[FollowUp, ...]
) -> tuple[FollowUp, ...]:
    """Append *extra* follow-ups whose exact-match key is not already in *primary*.

    *primary* (the supervisor's own list) wins on a collision, so an evaluator finding the
    supervisor already reported is not duplicated; *extra* is also deduped against itself.

    Exact match is deliberate, and it does leave one gap: a supervisor that *paraphrases* an
    accepted finding produces a near-duplicate this cannot see (measured once at 10 bullets for ~6
    issues, two pairs disagreeing on severity). The fix is upstream — the finalize prompt says that
    accepted findings are merged in deterministically and must not be restated — because the
    alternative keys are lossy: on ``(paths, severity)`` two genuinely distinct findings in one file
    at one severity collapse into a single bullet, and losing an actionable item is worse than
    printing one twice.
    """
    seen = {_follow_up_key(fu) for fu in primary}
    merged = list(primary)
    for follow_up in extra:
        key = _follow_up_key(follow_up)
        if key in seen:
            continue
        seen.add(key)
        merged.append(follow_up)
    return tuple(merged)


def _follow_up_bullet(follow_up: FollowUp) -> str:
    """One follow-up as a Markdown bullet — the shape both sinks below print."""
    parts = [f"- **[{follow_up.severity}] {follow_up.title}**"]
    if follow_up.rationale:
        parts.append(f" — {follow_up.rationale}")
    if follow_up.paths:
        parts.append(f" Paths: {', '.join(follow_up.paths)}.")
    if follow_up.action_hint:
        parts.append(f" Suggested: {follow_up.action_hint}")
    return "".join(parts)


def render_follow_ups_section(follow_ups: tuple[FollowUp, ...]) -> str:
    """Render the ``## Technical debt / follow-ups`` section appended to ``summary.md``."""
    lines = [FOLLOW_UPS_HEADING, ""] + [_follow_up_bullet(fu) for fu in follow_ups]
    return "\n".join(lines) + "\n"


def append_task_follow_ups(
    path: Path,
    *,
    task_id: str,
    task_title: str,
    finished_at: str,
    follow_ups: tuple[FollowUp, ...],
) -> None:
    """Append one finished task's *follow_ups* to the accumulating file at *path*.

    **Append-only, and that is the whole design.** The file is never read back, never regenerated
    from ``state.db``, and never deduped across tasks: rebuilding it would overwrite the operator's
    edits, and with no ``resolve`` verb anywhere that would leave no way to ever close an item. Ten
    tasks with three findings each therefore leave thirty entries, including repeats — the operator
    curates by deleting, which a writer that reconciles would silently undo.

    A task with no follow-ups writes **nothing** (not an empty section, not even the file), so the
    file's existence means there is something in it. The one-time header is written when the file is
    created; ``newline=""`` keeps the separator LF on every host, because the daemon may run
    anywhere and the file must not change line endings with it.

    Raises ``OSError`` — the caller decides, and for the orchestrator this is best-effort: an
    unwritable control home must not change a task's terminal status.
    """
    if not follow_ups:
        return
    header = "" if path.exists() else _FOLLOW_UPS_FILE_HEADER
    # The leading "" is the blank line that separates this section from whatever precedes it — the
    # header on the first append, the previous task's last bullet on every later one.
    lines = ["", f"## {task_id} — {task_title}", "", f"Finished {finished_at}.", ""]
    lines += [_follow_up_bullet(fu) for fu in follow_ups]
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(header + "\n".join(lines) + "\n")


def render_gate_digest(evaluations: Sequence[EvaluationRow]) -> str | None:
    """Render every evaluator node's final verdict + findings for the finalize turn.

    Without it the finalize turn writes about the gates from session memory: the run this came from
    produced "three independent verification gates … all of which passed" with four critic findings
    sitting in ``state.db``. Each line states the node, its verdict, and how many findings it
    recorded, so "passed" is not writable about a gate that emitted any. Finding reasons are cut at
    :data:`FINDING_TITLE_MAX`, the same bound the observation digest and the follow-up titles use.

    ``None`` when the task ran no in-flow evaluator (a flow with no gates), so the section is simply
    absent rather than an empty heading.
    """
    lines: list[str] = []
    for (node_id, subtask), row in _last_verdict_per_node(evaluations).items():
        findings = _row_findings(row)
        subtask_label = f" (subtask {subtask})" if subtask is not None else ""
        where = f"{node_id or 'evaluator'}{subtask_label}"
        if not findings:
            lines.append(f"- {where}: verdict `{row.verdict}`, no findings recorded")
            continue
        lines.append(f"- {where}: verdict `{row.verdict}`, {len(findings)} finding(s) recorded:")
        for finding in findings:
            reason = " ".join(str(finding.get("reason") or "").split())
            if len(reason) > FINDING_TITLE_MAX:
                reason = reason[:FINDING_TITLE_MAX].rstrip() + "…"
            paths_raw = finding.get("paths")
            paths = (
                f" ({', '.join(str(p) for p in paths_raw)})"
                if isinstance(paths_raw, list) and paths_raw
                else ""
            )
            lines.append(f"  - [{finding.get('severity') or 'unknown'}] {reason}{paths}")
    return "\n".join(lines) if lines else None
