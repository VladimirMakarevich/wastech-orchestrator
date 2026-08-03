"""The deterministic ``SupervisorPacket`` — the whole-task facts the finalize turn is grounded in.

The supervisor's finalize turn used to be grounded in its own **warm session**: the session that had
read the diff and accumulated per-step observations during the run. That made the summary depend on
a live process (a revived task got a thinner one) and made the finalize call's input grow with every
rework cycle, because the whole editing lineage was re-sent as input on each turn.

This module replaces that with a small, bounded artifact assembled from state the run already
persisted. Determinism is the contract (P0-D2): building the packet is a **pure function of
``state.db`` plus the task's artifacts** — no clock, no environment, no absolute paths, no reliance
on filesystem traversal order. Steps come in ``node_runs.id`` order, paths inside are repo-relative
POSIX (the provider's working directory *is* the repo), and the serialization is canonical
(``sort_keys``), so two builds from the same state are byte-identical and a revive that re-executed
nothing yields the same bytes.

Bounded by construction (P0-D3): a packet is kilobytes, not the hundreds of kilobytes of history it
replaces. The full diff is inlined only while it is small — skipping it would force the model into
an extra tool round, and every round re-sends the whole prompt as input, which costs more than the
4 KB it saves.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wastech_orchestrator.state_store import CheckRunRow, NodeRunRow

# --- Bounds (P0-D3) ------------------------------------------------------------------------------
# Named constants, not config — nobody asked for a knob, and a packet whose size an operator can
# raise stops being the bounded thing the finalize budget relies on.

#: Longest ``current.diff`` still inlined verbatim; above it the packet carries changed paths + the
#: diff stat + the path to the full artifact.
_DIFF_INLINE_MAX = 4_000
#: Longest per-step message. The SAME cap applies to the observation prompt's ``final_message``
#: (:func:`bound_step_message`) — a chatty node used to inflate every observe turn without limit.
_STEP_MESSAGE_MAX = 500
#: Longest rendered observation digest; the oldest lines are dropped and the remainder is marked.
_OBSERVATIONS_MAX = 8_000

_ELLIPSIS = "…"
_OBSERVATIONS_TRUNCATED = "(older observations dropped — digest bound reached)"

# A unified diff's header lines. ``+++ b/<path>`` carries the path literally (spaces included), so
# it is preferred over the ambiguous ``diff --git`` line — the same reasoning the packaged check
# tools use. Both are read ONLY between a ``diff --git`` line and the file's first ``@@`` hunk
# header: an unanchored match would read a deleted body line like ``--- x`` as a file header.
_DIFF_OLD_PATH_RE = re.compile(r"^--- a/(.*)$")
_DIFF_NEW_PATH_RE = re.compile(r"^\+\+\+ b/(.*)$")


def bound_step_message(text: str) -> str:
    """Truncate a node's own closing message to the recorded per-step cap, with an ellipsis.

    Shared by the packet's ``steps[].message`` and the per-step observation prompt so the cap is
    stated once (P0-D3) rather than drifting between the two surfaces.
    """
    stripped = text.strip()
    if len(stripped) <= _STEP_MESSAGE_MAX:
        return stripped
    return stripped[: _STEP_MESSAGE_MAX - 1].rstrip() + _ELLIPSIS


@dataclass(frozen=True)
class PacketFacts:
    """Everything :func:`render_packet` needs — every field a durable fact, nothing live.

    ``step_messages`` maps a ``node_runs.id`` to that run's own closing message (the
    ``<node_id>.out.md`` the orchestrator already writes per run). It is deliberately keyed off the
    node run rather than off an observation: a packet must stay complete when the observation
    cadence is turned down, which is exactly what the next phase does.

    ``diff_path`` / ``findings_path`` are repo-relative POSIX paths to the **exchange** copies — the
    only copies the provider may read. ``None`` when the run produced no such artifact.
    """

    task_id: str
    task_title: str
    task_type: str | None
    flow_name: str | None
    node_runs: tuple[NodeRunRow, ...]
    check_runs: tuple[CheckRunRow, ...]
    step_messages: Mapping[int, str]
    diff_text: str
    diff_path: str | None
    findings_path: str | None
    material_observations: str | None


def render_packet(facts: PacketFacts) -> str:
    """Render *facts* as the canonical packet JSON (``sort_keys``, trailing newline).

    Canonical on purpose: byte-identical output for identical facts is what makes the packet — and
    therefore the summary synthesized from it — reproducible across a normal run and a revive.
    """
    payload: dict[str, Any] = {
        "task": {"id": facts.task_id, "title": facts.task_title, "type": facts.task_type},
        "flow": {"name": facts.flow_name},
        "changes": _changes(facts.diff_text, facts.diff_path),
        "steps": _steps(facts.node_runs, facts.step_messages),
        "checks": _checks(facts.check_runs),
        "findings_path": facts.findings_path,
        "material_observations": _observations(facts.material_observations),
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _changes(diff_text: str, diff_path: str | None) -> dict[str, Any]:
    """The change block: changed paths, the diff stat, the artifact path, and a small diff inline.

    Derived from the ``current.diff`` **artifact** rather than from a live ``git diff --stat``: a
    fresh git invocation reads the working tree, which is not durable state, so it would break the
    pure-function contract the reproducibility criterion rests on.
    """
    paths: list[str] = []
    insertions = 0
    deletions = 0
    in_header = False
    old_path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_header, old_path = True, None
            continue
        if in_header:
            if line.startswith("--- "):
                match = _DIFF_OLD_PATH_RE.match(line)
                old_path = match.group(1).strip() if match else None
            elif line.startswith("+++ "):
                match = _DIFF_NEW_PATH_RE.match(line)
                # ``+++ /dev/null`` is a deletion: the file it names is on the ``---`` line.
                path = match.group(1).strip() if match else old_path
                if path and path not in paths:
                    paths.append(path)
            elif line.startswith("@@"):
                in_header = False
            continue
        if line.startswith("+"):
            insertions += 1
        elif line.startswith("-"):
            deletions += 1
    changes: dict[str, Any] = {
        "paths": paths,
        "diff_stats": {"files": len(paths), "insertions": insertions, "deletions": deletions},
        "diff_path": diff_path,
    }
    if 0 < len(diff_text) <= _DIFF_INLINE_MAX:
        changes["diff"] = diff_text
    return changes


def _steps(
    node_runs: Sequence[NodeRunRow], step_messages: Mapping[int, str]
) -> list[dict[str, Any]]:
    """One record per flow node run, in execution order, with its closing message when it has one.

    Timestamps, ``stage_attempts``, the provider actually used, and the fallback/retry facts are
    kept, not scrubbed (P0-D2): they are durable, and they are the material the summary's caveats
    are written from. Only the keys a run actually has are emitted, so a clean step stays short.
    """
    steps: list[dict[str, Any]] = []
    for row in node_runs:
        step: dict[str, Any] = {
            "node": row.node_id,
            "kind": row.node_kind,
            "status": row.status,
            "outcome": row.outcome,
            "stage_attempts": row.stage_attempts,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }
        if row.subtask_order is not None:
            step["subtask"] = row.subtask_order
        if row.provider_used:
            step["provider_used"] = row.provider_used
            if row.route_primary and row.provider_used != row.route_primary:
                # The attempt landed on a provider other than the resolved primary — a fallback.
                step["fallback_from"] = row.route_primary
        if row.error_class:
            step["error_class"] = row.error_class
        if row.skipped:
            step["skipped"] = True
            if row.skip_reason:
                step["skip_reason"] = row.skip_reason
        message = step_messages.get(row.id) if row.id is not None else None
        if message and message.strip():
            step["message"] = bound_step_message(message)
        steps.append(step)
    return steps


def _checks(check_runs: Sequence[CheckRunRow]) -> dict[str, list[str]]:
    """The check commands this task ran, split by result.

    ``skipped`` is its own list, never folded into ``failed``: a check whose toolchain was absent
    did not fail, and a summary that says otherwise is wrong in the direction that matters. This
    block keeps "which checks passed" writable now that the ``checks`` node is no longer observed.
    """
    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for row in check_runs:
        if row.skipped:
            skipped.append(row.command)
        elif row.passed:
            passed.append(row.command)
        else:
            failed.append(row.command)
    return {"passed": passed, "failed": failed, "skipped": skipped}


def _observations(digest: str | None) -> str | None:
    """The observation digest bounded to :data:`_OBSERVATIONS_MAX`, oldest lines dropped first.

    The newest observations are the ones the synthesis needs most, so the cut is taken from the
    front and the remainder is marked — a silent truncation would read as "that is all there was".
    """
    if not digest:
        return None
    if len(digest) <= _OBSERVATIONS_MAX:
        return digest
    budget = _OBSERVATIONS_MAX - len(_OBSERVATIONS_TRUNCATED) - 1
    kept: list[str] = []
    for line in reversed(digest.splitlines()):
        if len(line) + 1 > budget:
            break
        kept.append(line)
        budget -= len(line) + 1
    if not kept:  # a single line longer than the whole budget — hard-cut its tail
        return f"{_OBSERVATIONS_TRUNCATED}\n{digest[-budget:]}"
    kept.reverse()
    return "\n".join([_OBSERVATIONS_TRUNCATED, *kept])
