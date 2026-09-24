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
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
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
#: Longest rendered ``data`` object a ``tool`` step carries. Its own cap, not the message's: a gate
#: reports measurements the summary has to name, and a tool free to report anything is exactly the
#: thing that would otherwise inflate every packet in the run.
_STEP_DATA_MAX = 1_000
#: Longest head of a ``tool`` step's redacted stdout. Smaller than the others because it is the
#: least structured of the three and the one a chatty program grows without bound; the full stream
#: stays on disk under the run directory.
_STEP_STDOUT_MAX = 800
#: Longest per-step evaluator findings. The largest cap of the three: a lens with ten rework rounds
#: is the case this exists for, and its findings are what the pull-request body is written from.
_STEP_FINDINGS_MAX = 2_000
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


def _bounded(text: str, limit: int) -> str:
    """*text* stripped and truncated to *limit* characters, marked with an ellipsis when cut.

    The one truncation used by every bounded step field, so each field differs only in the number
    beside :data:`_STEP_MESSAGE_MAX` that it passes here.
    """
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + _ELLIPSIS


def bound_step_message(text: str) -> str:
    """Truncate a node's own closing message to the recorded per-step cap, with an ellipsis.

    Shared by the packet's ``steps[].message`` and the per-step observation prompt so the cap is
    stated once rather than drifting between the two surfaces.
    """
    return _bounded(text, _STEP_MESSAGE_MAX)


@dataclass(frozen=True)
class PacketFacts:
    """Everything :func:`render_packet` needs — every field a durable fact, nothing live.

    ``steps`` is the run's deterministic step record, stated once by the flow recorder and only
    formatted here. It is built from the node runs and their own output files rather than from
    observations, so a packet stays complete when the observation cadence is turned down or off.

    ``diff_path`` / ``findings_path`` are repo-relative POSIX paths to the **private** copies under
    ``.worc/logs/<task>/``, and they are for the human reading the post-mortem, not for the agent:
    the private read-deny projection keeps that tree closed to a provider at either value of
    ``security.disable_read_isolation``, while the exchange copies these once named are removed by
    terminal cleanup. What an agent can act on is inline on the steps. ``None`` when the run
    produced no such artifact, or when the private tree lies outside the repository.
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
        steps=collect_step_facts(node_runs, artifacts_root, task_id, evaluations),
        check_runs=tuple(store.get_check_runs(task_id)),
        diff_text=read_final_diff(artifacts_root, task_id),
        diff_path=_private_relpath(
            repo_dir, task_artifact_dir(artifacts_root, task_id) / "current.diff"
        ),
        findings_path=_findings_relpath(artifacts_root, repo_dir, task_id, evaluations),
        material_observations=material_observations,
    )


def _private_relpath(repo_dir: str | Path, path: Path) -> str | None:
    """A repo-relative POSIX path to an existing private artifact, or ``None``.

    Repo-relative and never absolute, because an absolute path inside the packet would make the
    bytes machine-dependent and break the byte-identity contract. The private tree normally lives at
    ``<repo>/.worc``; an operator who moved it outside the repository gets ``None`` rather than a
    machine-specific string, and the inline step fields carry the content either way.
    """
    if not path.is_file():
        return None
    try:
        return path.resolve().relative_to(Path(repo_dir).resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _findings_relpath(
    artifacts_root: str | Path,
    repo_dir: str | Path,
    task_id: str,
    evaluations: Sequence[EvaluationRow],
) -> str | None:
    """The latest in-flow evaluator verdict's private ``findings.json``, or ``None``.

    The verdict rows are insertion-ordered, so the last one is the most recent; its
    ``(node_id, source_node_run_id)`` rebuilds the per-run directory the evaluator wrote under. One
    path for a whole run is deliberately not the record of what the lenses found — that is on each
    step — it is the entry point a person opens when they want more than the step carries.
    """
    verdicts = [row for row in evaluations if row.kind == "in_flow_verdict"]
    if not verdicts:
        return None
    last = verdicts[-1]
    if last.node_id is None or last.source_node_run_id is None:
        return None
    run_dir = node_run_dir(artifacts_root, task_id, last.node_id, last.source_node_run_id)
    return _private_relpath(repo_dir, run_dir / "findings.json")


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

    Every cap is applied here rather than in the record because each is a property of this surface's
    size budget, not of what the node said — see the constants beside :data:`_STEP_MESSAGE_MAX`.

    ``data`` / ``stdout_head`` / ``findings`` are what a tool gate and an evaluator lens actually
    reported, inline. They are the only form in which those verdicts reach the finalize turn: the
    exchange copies are gone by the time anyone reads the packet, and the private tree the paths
    name is read-denied to a provider at either value of ``security.disable_read_isolation``. Each
    is rendered as bounded text rather than as a nested object, because the cap is a character cap
    and half of a JSON object is not JSON.
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
        _label_unfinished_publish(step, facts)
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
        if facts.tool_data and facts.tool_data.strip():
            step["data"] = _bounded(facts.tool_data, _STEP_DATA_MAX)
        if facts.tool_stdout and facts.tool_stdout.strip():
            step["stdout_head"] = _bounded(facts.tool_stdout, _STEP_STDOUT_MAX)
        if facts.findings and facts.findings.strip() not in ("", "[]", "{}"):
            step["findings"] = _bounded(facts.findings, _STEP_FINDINGS_MAX)
        rendered.append(step)
    return rendered


def _label_unfinished_publish(step: dict[str, Any], facts: StepFacts) -> None:
    """Relabel the ``publish`` step the packet is built inside, so it is not read as a defect.

    The packet is assembled by the publish node's own finalize hook, so that node's row is still
    ``running`` by construction — every run, always. Left as ``status: running`` the finalize turn
    read it as a fact worth reporting and wrote "the publish step is still recorded as running" into
    the pull-request body: internal state, in text people read. The status becomes ``pending`` (it
    is not running as far as this record can see — it had not started its work yet) and says so, so
    a reader who meets it knows it is expected rather than inferring an incident.
    """
    if facts.node_kind != "publish" or facts.status != "running" or facts.finished_at:
        return
    step["status"] = "pending"
    step["note"] = (
        "expected: this packet is built by the publish step itself, before it runs — its outcome "
        "is not part of the record and must not be described as unfinished work"
    )


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
