"""The deterministic ``SupervisorPacket`` — the whole-task facts the finalize turn is grounded in.

The finalize turn is grounded in this packet — a small, bounded artifact assembled from state the
run already persisted — rather than in the supervisor's own warm session, the one that read the diff
and accumulated per-step observations during the run. Grounding it in that session would make the
summary depend on a live process (a revived task gets a thinner one) and make the finalize call's
input grow with every rework cycle, since the whole editing lineage is re-sent on each turn.

Determinism is the contract: building the packet is a **pure function of
``state.db`` plus the task's artifacts** — no clock, no environment, no absolute paths, no reliance
on filesystem traversal order. Steps come in ``node_runs.id`` order, paths inside are repo-relative
POSIX (the provider's working directory *is* the repo), and the serialization is canonical
(``sort_keys``), so two builds from the same state are byte-identical and a revive that re-executed
nothing yields the same bytes.

Bounded by construction: a packet is kilobytes, not the hundreds of kilobytes of history it
replaces. The full diff is inlined only while it is small — skipping it would force the model into
an extra tool round, and every round re-sends the whole prompt as input, which costs more than the
4 KB it saves.

Despite the module's name, the **assembly** here (:func:`build_packet_facts`, plus
:func:`summarize_diff` and :func:`split_check_runs`) belongs to no layer: it is a plain read of
``state.db`` and the task's artifacts. The packet is one consumer;
:mod:`~wastech_orchestrator.core.summary_report` renders the same facts as the committed
pull-request body when the oversight layer produces no prose, or does not run at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from wastech_orchestrator.core.flow.recorder import StepFacts, collect_step_facts, read_final_diff
from wastech_orchestrator.providers.artifacts import exchange_node_run_dir, exchange_task_dir
from wastech_orchestrator.state_store import CheckRunRow, EvaluationRow, NodeRunRow

# --- Bounds --------------------------------------------------------------------------------------
# Named constants, not config — nobody asked for a knob, and a packet whose size an operator can
# raise stops being the bounded thing the finalize budget relies on.

#: Longest ``current.diff`` still inlined verbatim; above it the packet carries changed paths + the
#: diff stat + the path to the full artifact.
_DIFF_INLINE_MAX = 4_000
#: Longest per-step message. The SAME cap applies to the observation prompt's ``final_message``
#: (:func:`bound_step_message`), so a chatty node cannot inflate every observe turn without limit.
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
    stated once rather than drifting between the two surfaces.
    """
    stripped = text.strip()
    if len(stripped) <= _STEP_MESSAGE_MAX:
        return stripped
    return stripped[: _STEP_MESSAGE_MAX - 1].rstrip() + _ELLIPSIS


@dataclass(frozen=True)
class PacketFacts:
    """Everything :func:`render_packet` needs — every field a durable fact, nothing live.

    ``steps`` is the run's deterministic step record, stated once by the flow recorder and only
    formatted here. It is built from the node runs and their own output files rather than from
    observations, so a packet stays complete when the observation cadence is turned down or off.

    ``diff_path`` / ``findings_path`` are repo-relative POSIX paths to the **exchange** copies — the
    only copies the provider may read. ``None`` when the run produced no such artifact.
    """

    task_id: str
    task_title: str
    task_type: str | None
    flow_name: str | None
    steps: tuple[StepFacts, ...]
    check_runs: tuple[CheckRunRow, ...]
    diff_text: str
    diff_path: str | None
    findings_path: str | None
    material_observations: str | None


class PacketStorePort(Protocol):
    """The two read-only tables the run's facts are assembled from.

    A narrow port rather than the whole store: the assembly is a pure read, and both callers — the
    oversight layer through its own store port and the orchestrator through the concrete store —
    satisfy it structurally without either knowing about the other.
    """

    def get_node_runs(self, task_id: str) -> list[NodeRunRow]:
        """Every node run of the task, in insertion order."""
        ...

    def get_check_runs(self, task_id: str) -> list[CheckRunRow]:
        """Every check run of the task, in insertion order."""
        ...


def build_packet_facts(
    store: PacketStorePort,
    *,
    task_id: str,
    task_title: str,
    task_type: str | None,
    flow_name: str | None,
    evaluations: Sequence[EvaluationRow],
    artifacts_root: str | Path,
    exchange_root: str | Path,
    repo_dir: str | Path,
    material_observations: str | None = None,
) -> PacketFacts:
    """Assemble the run's facts from durable state — no live inputs.

    Every source here is either a ``state.db`` table or an already-written task artifact, which is
    what makes two builds from the same state byte-identical and a revive that re-executed nothing
    reproduce the same summary input. The per-step facts are not assembled here either: they are
    read from the flow recorder, so the facts a summary is written from do not depend on the layer
    that writes prose about them.

    A module function rather than a method for the same reason: the assembly needs nothing from the
    oversight layer, so it stays reachable when that layer does not run at all and the pull-request
    body has to be rendered from these facts directly. ``material_observations`` is that layer's own
    observation digest — the one genuinely layer-authored field, hence a parameter defaulting to
    ``None`` for every caller that has no observations to carry.
    """
    node_runs = tuple(store.get_node_runs(task_id))
    return PacketFacts(
        task_id=task_id,
        task_title=task_title,
        task_type=task_type,
        flow_name=flow_name,
        steps=collect_step_facts(node_runs, artifacts_root, task_id),
        check_runs=tuple(store.get_check_runs(task_id)),
        diff_text=read_final_diff(artifacts_root, task_id),
        diff_path=_exchange_relpath(exchange_root, repo_dir, task_id, "current.diff"),
        findings_path=_findings_relpath(exchange_root, repo_dir, task_id, evaluations),
        material_observations=material_observations,
    )


def _exchange_relpath(
    exchange_root: str | Path, repo_dir: str | Path, task_id: str, relname: str
) -> str | None:
    """A repo-relative POSIX path to an existing exchange artifact, or ``None``.

    Repo-relative because the provider's working directory *is* the repository, and because an
    absolute path inside the packet would make the bytes machine-dependent.
    Only the exchange copy is ever named — it is the only copy the provider may read.
    """
    if not exchange_root:
        return None
    path = exchange_task_dir(exchange_root, task_id) / relname
    if not path.is_file():
        return None
    try:
        return path.resolve().relative_to(Path(repo_dir).resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _findings_relpath(
    exchange_root: str | Path,
    repo_dir: str | Path,
    task_id: str,
    evaluations: Sequence[EvaluationRow],
) -> str | None:
    """The latest in-flow evaluator verdict's published ``findings.json``, or ``None``.

    The verdict rows are insertion-ordered, so the last one is the most recent; its
    ``(node_id, source_node_run_id)`` rebuilds the per-run path the evaluator published under.
    """
    verdicts = [row for row in evaluations if row.kind == "in_flow_verdict"]
    if not verdicts or not exchange_root:
        return None
    last = verdicts[-1]
    if last.node_id is None or last.source_node_run_id is None:
        return None
    run_dir = exchange_node_run_dir(exchange_root, task_id, last.node_id, last.source_node_run_id)
    task_dir = exchange_task_dir(exchange_root, task_id)
    relname = (run_dir.relative_to(task_dir) / "findings.json").as_posix()
    return _exchange_relpath(exchange_root, repo_dir, task_id, relname)


def render_packet(facts: PacketFacts) -> str:
    """Render *facts* as the canonical packet JSON (``sort_keys``, trailing newline).

    Canonical on purpose: byte-identical output for identical facts is what makes the packet — and
    therefore the summary synthesized from it — reproducible across a normal run and a revive.
    """
    payload: dict[str, Any] = {
        "task": {"id": facts.task_id, "title": facts.task_title, "type": facts.task_type},
        "flow": {"name": facts.flow_name},
        "changes": _changes(facts.diff_text, facts.diff_path),
        "steps": _steps(facts.steps),
        "checks": _checks(facts.check_runs),
        "findings_path": facts.findings_path,
        "material_observations": _observations(facts.material_observations),
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class DiffSummary:
    """What a unified diff touched: the paths, in first-seen order, and the line counts."""

    paths: tuple[str, ...]
    insertions: int
    deletions: int


def summarize_diff(diff_text: str) -> DiffSummary:
    """Parse a unified diff into changed paths + line counts.

    Derived from the ``current.diff`` **artifact** rather than from a live ``git diff --stat``: a
    fresh git invocation reads the working tree, which is not durable state, so it would break the
    pure-function contract the reproducibility criterion rests on — for the packet and equally for
    the committed report rendered from the same facts.
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
    return DiffSummary(paths=tuple(paths), insertions=insertions, deletions=deletions)


def _changes(diff_text: str, diff_path: str | None) -> dict[str, Any]:
    """The change block: changed paths, the diff stat, the artifact path, a small diff inline."""
    summary = summarize_diff(diff_text)
    changes: dict[str, Any] = {
        "paths": list(summary.paths),
        "diff_stats": {
            "files": len(summary.paths),
            "insertions": summary.insertions,
            "deletions": summary.deletions,
        },
        "diff_path": diff_path,
    }
    if 0 < len(diff_text) <= _DIFF_INLINE_MAX:
        changes["diff"] = diff_text
    return changes


def _steps(steps: Sequence[StepFacts]) -> list[dict[str, Any]]:
    """The step record rendered in execution order, each run with its closing message if it has one.

    Timestamps, ``stage_attempts``, the provider actually used, and the fallback/retry facts are
    kept, not scrubbed: they are durable, and they are the material the summary's caveats are
    written from. Only the keys a run actually has are emitted, so a clean step stays short — a
    blanket ``null`` per absent fact would inflate every packet and read as a recorded absence.

    The message cap is applied here rather than in the record because it is a property of this
    surface's size budget, not of what the node said.
    """
    rendered: list[dict[str, Any]] = []
    for facts in steps:
        step: dict[str, Any] = {
            "node": facts.node_id,
            "kind": facts.node_kind,
            "status": facts.status,
            "outcome": facts.outcome,
            "stage_attempts": facts.stage_attempts,
            "started_at": facts.started_at,
            "finished_at": facts.finished_at,
        }
        if facts.subtask_order is not None:
            step["subtask"] = facts.subtask_order
        if facts.provider_used:
            step["provider_used"] = facts.provider_used
            if facts.fallback_from:
                step["fallback_from"] = facts.fallback_from
        if facts.error_class:
            step["error_class"] = facts.error_class
        if facts.skipped:
            step["skipped"] = True
            if facts.skip_reason:
                step["skip_reason"] = facts.skip_reason
        if facts.message and facts.message.strip():
            step["message"] = bound_step_message(facts.message)
        rendered.append(step)
    return rendered


@dataclass(frozen=True)
class CheckOutcomes:
    """The task's check commands split by result, in the order they ran.

    ``skipped`` is its own field, never folded into ``failed``: a check whose toolchain was absent
    did not fail, and a summary that says otherwise is wrong in the direction that matters.
    """

    passed: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]


def split_check_runs(check_runs: Sequence[CheckRunRow]) -> CheckOutcomes:
    """Split check runs by result. One entry per *run*, so a re-run command appears once per run."""
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
    return CheckOutcomes(passed=tuple(passed), failed=tuple(failed), skipped=tuple(skipped))


def _checks(check_runs: Sequence[CheckRunRow]) -> dict[str, list[str]]:
    """The check commands this task ran, split by result.

    Every run is listed, including a command that failed and was later fixed: the packet is the
    run's record, and the finalize turn is expected to read the step order alongside it. This block
    keeps "which checks passed" writable, since the ``checks`` node itself is not observed.
    """
    outcomes = split_check_runs(check_runs)
    return {
        "passed": list(outcomes.passed),
        "failed": list(outcomes.failed),
        "skipped": list(outcomes.skipped),
    }


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
