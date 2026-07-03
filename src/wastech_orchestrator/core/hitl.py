"""Typed node HITL signals and durable interaction artifacts.

Only a node that declares a ``human_input`` / ``planning`` output contract may ask the human. The
provider proposes a typed signal, but the Core validates it strictly, sends it through the
transport-agnostic ``Notifier`` contract, and passes the redacted answer back to a fresh run through
``AgentRunRequest.human_input_path``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

from wastech_orchestrator.notify import AskFailure, AskHandle, AskKind, AskResult
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.redaction import redact_text

#: Which built-in typed-output contract a flow node's structured result must satisfy. Derived per
#: node (never from a stage name): ``human_input`` = a question/approval round-trip; ``planning``
#: adds the decomposition proposal; ``none`` = a plain author node. (Skill selection is no longer a
#: planning concern — it is resolved by the Core from operator pins + the supervisor's proposal.)
OutputContract = Literal["none", "human_input", "planning"]

_RISKS = frozenset({"clarification", "deletion", "dependency", "protected", "other"})
_MAX_PATHS = 100
_MAX_TEXT = 16_000

_HUMAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["question", "approval"]},
        "question": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT},
        "context": {"type": "string", "maxLength": _MAX_TEXT},
        "risk": {
            "type": "string",
            "enum": sorted(_RISKS),
        },
        "paths": {
            "type": "array",
            "maxItems": _MAX_PATHS,
            "items": {"type": "string", "minLength": 1, "maxLength": 512},
        },
    },
    "required": ["kind", "question", "context", "risk", "paths"],
}

_SUBTASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "order": {"type": "integer", "minimum": 1},
        "title": {"type": "string", "minLength": 1},
        "slug": {"type": "string", "minLength": 1},
        "acceptance_criteria": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
    },
    "required": ["order", "title", "slug", "acceptance_criteria", "depends_on"],
}


class StageOutputError(ValueError):
    """The provider returned a malformed typed stage result."""


@dataclass(frozen=True)
class HumanInputSignal:
    """One validated human question or approval request proposed by an agent stage."""

    kind: AskKind
    question: str
    context: str
    risk: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class TypedStageOutput:
    """Validated content plus an optional HITL signal."""

    content: str
    human_input: HumanInputSignal | None
    structured: Mapping[str, Any]


def typed_output_schema(contract: OutputContract) -> dict[str, Any] | None:
    """Return the strict provider schema for a HITL-capable node's typed output.

    ``human_input`` (a refinement-style node): ``content`` plus an optional human question/approval.
    ``planning``: the same, plus the decomposition proposal. ``none`` (a plain author node) has no
    built-in typed schema (the node may still set its own schema).
    """
    if contract == "human_input":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "content": {"type": "string"},
                "human_input": _HUMAN_INPUT_SCHEMA,
            },
            "required": ["content", "human_input"],
        }
    if contract == "planning":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "content": {"type": "string"},
                "human_input": _HUMAN_INPUT_SCHEMA,
                "decompose": {"type": "boolean"},
                "subtasks": {
                    "type": "array",
                    "items": _SUBTASK_SCHEMA,
                },
            },
            "required": ["content", "human_input", "decompose", "subtasks"],
        }
    return None


def parse_typed_output(
    contract: OutputContract, structured: Mapping[str, Any] | None
) -> TypedStageOutput:
    """Validate a human_input/planning node result, independent of provider schema enforcement."""
    if contract == "none":
        raise StageOutputError("node does not support typed HITL output")
    if not isinstance(structured, Mapping):
        raise StageOutputError("node must return structured output")

    expected = (
        {"content", "human_input"}
        if contract == "human_input"
        else {"content", "human_input", "decompose", "subtasks"}
    )
    if set(structured) != expected:
        raise StageOutputError(f"output keys must be exactly {sorted(expected)}")
    content = structured.get("content")
    if not isinstance(content, str):
        raise StageOutputError("content must be a string")

    if contract == "planning":
        if not isinstance(structured.get("decompose"), bool):
            raise StageOutputError("planning.decompose must be a boolean")
        subtasks = structured.get("subtasks")
        if not isinstance(subtasks, list):
            raise StageOutputError("planning.subtasks must be a list")
        _validate_subtasks(subtasks)

    raw_signal = structured.get("human_input")
    signal = None if raw_signal is None else _parse_signal(raw_signal)
    return TypedStageOutput(content=content, human_input=signal, structured=structured)


def _parse_signal(raw: Any) -> HumanInputSignal:
    if not isinstance(raw, Mapping):
        raise StageOutputError("human_input must be an object or null")
    required = {"kind", "question", "context", "risk", "paths"}
    if set(raw) != required:
        raise StageOutputError(f"human_input keys must be exactly {sorted(required)}")
    kind = raw.get("kind")
    question = raw.get("question")
    context = raw.get("context")
    risk = raw.get("risk")
    paths = raw.get("paths")
    if kind not in ("question", "approval"):
        raise StageOutputError("human_input.kind must be question or approval")
    if not isinstance(question, str) or not question.strip() or len(question) > _MAX_TEXT:
        raise StageOutputError("human_input.question must be a non-empty bounded string")
    if not isinstance(context, str) or len(context) > _MAX_TEXT:
        raise StageOutputError("human_input.context must be a bounded string")
    if risk not in _RISKS:
        raise StageOutputError(f"human_input.risk must be one of {sorted(_RISKS)}")
    if not isinstance(paths, list):
        raise StageOutputError("human_input.paths must be a list")
    if len(paths) > _MAX_PATHS:
        raise StageOutputError(f"human_input.paths may contain at most {_MAX_PATHS} paths")
    normalized = tuple(sorted({_normalize_path(path) for path in paths}))
    return HumanInputSignal(
        kind=kind,
        question=question.strip(),
        context=context.strip(),
        risk=risk,
        paths=normalized,
    )


def _validate_subtasks(raw_subtasks: list[Any]) -> None:
    required = {"order", "title", "slug", "acceptance_criteria", "depends_on"}
    for index, raw in enumerate(raw_subtasks):
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise StageOutputError(
                f"planning.subtasks[{index}] keys must be exactly {sorted(required)}"
            )
        order = raw.get("order")
        title = raw.get("title")
        slug = raw.get("slug")
        acceptance = raw.get("acceptance_criteria")
        depends_on = raw.get("depends_on")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise StageOutputError(f"planning.subtasks[{index}].order must be a positive integer")
        if not isinstance(title, str) or not title.strip():
            raise StageOutputError(f"planning.subtasks[{index}].title must be non-empty")
        if not isinstance(slug, str) or not slug.strip():
            raise StageOutputError(f"planning.subtasks[{index}].slug must be non-empty")
        if (
            not isinstance(acceptance, list)
            or not acceptance
            or any(not isinstance(item, str) or not item.strip() for item in acceptance)
        ):
            raise StageOutputError(
                f"planning.subtasks[{index}].acceptance_criteria must be non-empty strings"
            )
        if not isinstance(depends_on, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in depends_on
        ):
            raise StageOutputError(
                f"planning.subtasks[{index}].depends_on must contain positive integers"
            )


def _normalize_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
        raise StageOutputError("human_input path must be a non-empty bounded string")
    value = raw.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise StageOutputError(f"human_input path is not repository-relative: {raw!r}")
    return path.as_posix()


def interaction_path(
    artifacts_root: str | Path,
    task_id: str,
    node_id: str,
    *,
    subtask: int | None = None,
) -> Path:
    """Durable artifact for a node's embedded HITL round-trip, keyed by the flow node id."""
    suffix = f"-subtask-{subtask}" if subtask is not None else ""
    return task_artifact_dir(artifacts_root, task_id) / "hitl" / f"{node_id}{suffix}.json"


def guardrail_interaction_path(
    artifacts_root: str | Path,
    task_id: str,
    node_id: str,
    *,
    subtask: int | None,
    cycle: int,
) -> Path:
    subtask_suffix = f"-subtask-{subtask}" if subtask is not None else ""
    return (
        task_artifact_dir(artifacts_root, task_id)
        / "hitl"
        / f"guardrail-{node_id}{subtask_suffix}-cycle-{cycle}.json"
    )


def node_interaction_path(
    artifacts_root: str | Path,
    task_id: str,
    node_id: str,
    *,
    subtask: int | None = None,
) -> Path:
    """Durable artifact for a standalone ``hitl`` gate node, keyed by node id (no ``Stage``).

    Prefixed with ``node-`` so a node id never collides with a stage-keyed interaction file.
    """
    suffix = f"-subtask-{subtask}" if subtask is not None else ""
    return task_artifact_dir(artifacts_root, task_id) / "hitl" / f"node-{node_id}{suffix}.json"


def turn_gate_interaction_path(
    artifacts_root: str | Path,
    task_id: str,
    node_id: str,
    *,
    subtask: int | None = None,
) -> Path:
    """Durable artifact for a node's max-turns continue/stop gate (idea 29), keyed by node id.

    One file per node (no cycle index): with no resume cap there is nothing to count, so each
    continue/stop round-trip reuses and overwrites this artifact (``mark_consumed`` after each
    answer). Prefixed ``turn-gate-`` so it never collides with the embedded-HITL or guardrail files.
    """
    suffix = f"-subtask-{subtask}" if subtask is not None else ""
    return task_artifact_dir(artifacts_root, task_id) / "hitl" / f"turn-gate-{node_id}{suffix}.json"


def interaction_id(task_id: str, node_id: str, subtask: int | None = None) -> str:
    """Return a compact deterministic id that fits Telegram callback-data limits.

    ``node_id`` is the interaction key — the flow node id (embedded agent HITL / dangerous-diff
    guard / check-discovery) or a standalone ``hitl`` gate node id.
    """
    raw = f"{task_id}:{node_id}:{subtask if subtask is not None else '-'}"
    return "h" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def discovery_interaction_path(artifacts_root: str | Path, task_id: str) -> Path:
    """The durable artifact for a check-command-set approval, under the task's ``hitl/``."""
    return task_artifact_dir(artifacts_root, task_id) / "hitl" / "check-discovery.json"


def discovery_interaction_id(task_id: str, signature: str) -> str:
    """A compact deterministic id for a discovery approval, scoped to the command-set signature so a
    *changed* set yields a fresh interaction (fits Telegram callback-data limits)."""
    raw = f"{task_id}:check-discovery:{signature}"
    return "d" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def iter_task_interactions(artifacts_root: str | Path, task_id: str) -> Iterator[dict[str, Any]]:
    """Yield every durable interaction artifact recorded for a task, in stable filename order.

    Covers embedded-HITL round-trips, dangerous-diff guardrails, and gate-node interactions (every
    ``hitl/*.json``). The dangerous-diff guard uses this to honor a prior in-task approval of an
    identical dangerous diff so a later workspace-write node does not re-prompt for it.
    """
    hitl_dir = task_artifact_dir(artifacts_root, task_id) / "hitl"
    if not hitl_dir.is_dir():
        return
    for path in sorted(hitl_dir.glob("*.json")):
        persisted = load_interaction(path)
        if persisted is not None:
            yield persisted


def load_interaction(path: Path) -> dict[str, Any] | None:
    """Load a durable interaction artifact; malformed content fails closed."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise StageOutputError(f"invalid HITL artifact: {path}")
    return data


def write_waiting_interaction(
    path: Path,
    *,
    task_id: str,
    node_id: str,
    subtask: int | None,
    signal: HumanInputSignal,
    handle: AskHandle,
) -> None:
    payload = {
        "schema_version": 1,
        "interaction_id": handle.interaction_id,
        "task_id": task_id,
        "node_id": node_id,
        "subtask": subtask,
        "status": "waiting" if handle.delivered else "transport_error",
        "request": {
            "kind": signal.kind,
            "question": redact_text(signal.question),
            "context": redact_text(signal.context),
            "risk": signal.risk,
            "paths": list(signal.paths),
        },
        "handle": asdict(handle),
        "telegram_message_id": handle.message_id,
        "deadline": handle.expires_at,
        "answer": None,
        "approved": None,
        "failure": None if handle.delivered else "transport_error",
    }
    _atomic_json(path, payload)


def write_answer(path: Path, result: AskResult) -> None:
    payload = load_interaction(path)
    if payload is None:
        raise StageOutputError(f"missing HITL artifact: {path}")
    payload["status"] = "answered" if result.failure is None else result.failure
    payload["answer"] = redact_text(result.text) if result.text is not None else None
    payload["approved"] = result.approved
    payload["failure"] = result.failure
    _atomic_json(path, payload)


def mark_consumed(path: Path) -> None:
    payload = load_interaction(path)
    if payload is None:
        raise StageOutputError(f"missing HITL artifact: {path}")
    payload["status"] = "consumed"
    _atomic_json(path, payload)


def mark_interaction_status(path: Path, status: str) -> None:
    payload = load_interaction(path)
    if payload is None:
        raise StageOutputError(f"missing HITL artifact: {path}")
    payload["status"] = status
    _atomic_json(path, payload)


# Statuses an interaction can carry while still un-answered: the delivered-but-unanswered "waiting"
# plus every AskFailure (timeout / transport_error / invalid_response, persisted by write_answer).
# Cleanup (reset / consume) acts on exactly these — derived from AskFailure so a new failure mode is
# covered automatically and never silently skipped.
_RESETTABLE_STATUSES: frozenset[str] = frozenset({"waiting", *get_args(AskFailure)})


def reset_pending_interactions(artifacts_root: str | Path, task_id: str) -> list[str]:
    """Remove un-answered (``waiting`` / any ``AskFailure``) HITL artifacts for a continue.

    The infra failure being continued from is often a dropped notifier (the prompt never delivered,
    timed out, or returned an invalid response). Deleting only those artifacts makes the re-entered
    stage ask fresh instead of blocking on, or replaying, a stale prompt; ``answered``/``consumed``
    artifacts from earlier completed stages are left intact (the resume engine skips those stages).
    Returns the paths that were reset, for logging.
    """
    hitl_dir = task_artifact_dir(artifacts_root, task_id) / "hitl"
    if not hitl_dir.is_dir():
        return []
    reset: list[str] = []
    for path in sorted(hitl_dir.glob("*.json")):
        payload = load_interaction(path)
        if payload is not None and payload.get("status") in _RESETTABLE_STATUSES:
            path.unlink()
            reset.append(str(path))
    return reset


def consume_pending_interactions(artifacts_root: str | Path, task_id: str) -> list[str]:
    """Close un-answered (``waiting`` or any ``AskFailure``) HITL artifacts for a `finalize`.

    Marks them ``consumed`` (not deletes — the audit artifact is preserved) so a later resume can't
    act on a stale prompt for a task the operator already finalized. ``answered``/``consumed``
    artifacts are left untouched. Returns the paths that were closed.
    """
    hitl_dir = task_artifact_dir(artifacts_root, task_id) / "hitl"
    if not hitl_dir.is_dir():
        return []
    closed: list[str] = []
    for path in sorted(hitl_dir.glob("*.json")):
        payload = load_interaction(path)
        if payload is not None and payload.get("status") in _RESETTABLE_STATUSES:
            mark_interaction_status(path, "consumed")
            closed.append(str(path))
    return closed


def handle_from_artifact(payload: Mapping[str, Any]) -> AskHandle:
    raw = payload.get("handle")
    if not isinstance(raw, Mapping):
        raise StageOutputError("HITL artifact has no handle")
    try:
        interaction = raw["interaction_id"]
        kind = raw["kind"]
        expires_raw = raw["expires_at"]
        message_raw = raw.get("message_id")
        offset_raw = raw.get("update_offset")
        delivered = raw.get("delivered", True)
        if not isinstance(interaction, str) or not interaction or len(interaction) > 64:
            raise ValueError
        if kind not in ("question", "approval"):
            raise ValueError
        if isinstance(expires_raw, bool):
            raise ValueError
        expires_at = float(expires_raw)
        if not math.isfinite(expires_at):
            raise ValueError
        if not isinstance(delivered, bool):
            raise ValueError
        if message_raw is not None and (
            not isinstance(message_raw, int) or isinstance(message_raw, bool) or message_raw <= 0
        ):
            raise ValueError
        if offset_raw is not None and (
            not isinstance(offset_raw, int) or isinstance(offset_raw, bool) or offset_raw < 0
        ):
            raise ValueError
        if delivered and message_raw is None:
            raise ValueError
        return AskHandle(
            interaction_id=interaction,
            kind=kind,
            expires_at=expires_at,
            message_id=message_raw,
            update_offset=offset_raw,
            delivered=delivered,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StageOutputError("HITL artifact handle is malformed") from exc


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)
