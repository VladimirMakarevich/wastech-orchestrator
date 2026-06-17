"""Provider-neutral flow execution vocabulary (flow-engine P0.1).

The shared contract types consumed by the flow engine, its schema/validator, and the state store,
defined once so follow-on slices do not invent parallel enums or mappings. This module is pure: no
IO, no git, no SQLite, no provider CLI knowledge, and (in P0.1) no consumers yet.

It carries the foundation execution vocabulary that the absorbed backlog programs share: the
run-role audit field (``run_kind`` + ``role``), session scope, the deterministic ``QualityAction``
-> lifecycle mapping, output/publishing policy identifiers, the execution-unit identity, and a
deterministic secret-free fingerprint primitive. The flow-graph schema vocabulary (node kinds,
checkers, edges) belongs to the schema slice (P0.2); the ceiling to the validator slice (P0.3).

See ``docs/backlog/flows/{index.md,flow-contract.md}`` and ``co-design/notes.md`` for the locked
forms. Enum values deliberately avoid YAML 1.1 boolean/null tokens (``on``/``off``/``yes``/``no``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from wastech_orchestrator.core.state_machine import Status


class RunKind(StrEnum):
    """Why a run exists, without inventing fake pipeline stages (the foundation run-kind field)."""

    STAGE = "stage"  # produces the deliverable (the author/editor: implementation, fixing, ...)
    EVALUATOR = "evaluator"  # read-only; judges a produced artifact, returns a bounded verdict


class EvaluatorRole(StrEnum):
    """The fine-grained discriminator carried by every ``EVALUATOR`` run.

    These are the roles that ship; operator-authored flows may use other role strings (they get the
    default evaluator behavior). Only ``final_handoff`` triggers special core handling.
    """

    SUPERVISOR = "supervisor"
    REVIEW = "review"
    CRITIC = "critic"
    VERIFIER = "verifier"
    TEST_QUALITY = "test_quality"


class EvaluationKind(StrEnum):
    """An evaluator's sub-mode: a blocking stage verdict vs the final read-only handoff pass."""

    STAGE_OUTPUT = "stage_output"
    FINAL_HANDOFF = "final_handoff"


class SessionScope(StrEnum):
    """Provider-neutral session intent.

    ``EDITING_LINEAGE`` is for the stage authors (implementation / fixing). An evaluator never uses
    the author's editing lineage: it is ``FRESH_DISPOSABLE`` (fresh each pass) or, for a multi-round
    evaluator such as a research critic, ``RESUME_OWN_LINEAGE`` (its own resumable session).
    """

    FRESH_DISPOSABLE = "fresh_disposable"
    EDITING_LINEAGE = "editing_lineage"
    RESUME_OWN_LINEAGE = "resume_own_lineage"


class PermissionProfile(StrEnum):
    """The orchestrator permission profile a node resolves to (clamped to the ceiling in P0.3)."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class OutputPolicy(StrEnum):
    """Scalar output-policy identifier (resolves to the foundation ``ResolvedOutputPolicy``)."""

    CODE_CHANGE = "code_change"
    REPOSITORY_DOCUMENT = "repository_document"
    PRIVATE_CONTROL_WORKSPACE_REPORT = "private_control_workspace_report"


class PublishingPolicy(StrEnum):
    """Terminal publishing policy of a whole flow (the single profile-level source of truth)."""

    PULL_REQUEST = "pull_request"
    DOCUMENTATION_PULL_REQUEST = "documentation_pull_request"
    LOCAL_ARTIFACT = "local_artifact"
    PRIVATE_CONTROL_WORKSPACE_REPORT = "private_control_workspace_report"
    NONE = "none"


class NetworkPolicy(StrEnum):
    """Network access ceiling of a flow (binary levels; absence = no network).

    YAML 1.1 boolean/null tokens (``off``/``no``) are deliberately avoided — a flow grants no
    network by omitting ``network_policy`` entirely (the loader resolves a missing key to ``None``).
    """

    ADVISORIES = "advisories"  # fetch vulnerability advisories / package metadata (security_audit)
    RESEARCH = "research"  # broader external research fetches (deep_research)


class QualityAction(StrEnum):
    """The deterministic action the Core applies after a quality result (the foundation vocabulary).

    Only the Core ever applies an action; agents, profiles, and providers return validated verdicts
    but never transition state directly.
    """

    CONTINUE = "continue"
    ENTER_FIXING = "enter_fixing"
    REPEAT_STAGE = "repeat_stage"
    STOP_MANUAL = "stop_manual"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class LifecycleEffect:
    """How a :class:`QualityAction` maps onto the canonical state machine.

    ``kind`` is one of:

    - ``"advance"`` — to the stage's normal next status (relative; resolved against the graph);
    - ``"reenter_same"`` — re-enter the current status from persisted feedback (no self-loop edge);
    - ``"goto"`` — a fixed ``target`` status.
    """

    kind: Literal["advance", "reenter_same", "goto"]
    target: Status | None = None


# foundation mapping (each action maps to canonical state-machine behavior; do not duplicate it):
#   continue     -> normal next status for the stage
#   enter_fixing -> the implementing -> fixing edge
#   repeat_stage -> re-enter the same status (no new self-loop edge)
#   stop_manual  -> manual_action_required
#   fail         -> failed
QUALITY_ACTION_EFFECT: dict[QualityAction, LifecycleEffect] = {
    QualityAction.CONTINUE: LifecycleEffect("advance"),
    QualityAction.ENTER_FIXING: LifecycleEffect("goto", Status.FIXING),
    QualityAction.REPEAT_STAGE: LifecycleEffect("reenter_same"),
    QualityAction.STOP_MANUAL: LifecycleEffect("goto", Status.MANUAL_ACTION_REQUIRED),
    QualityAction.FAIL: LifecycleEffect("goto", Status.FAILED),
}


def quality_action_effect(action: QualityAction) -> LifecycleEffect:
    """Return the canonical lifecycle effect for ``action`` (total over :class:`QualityAction`)."""
    return QUALITY_ACTION_EFFECT[action]


@dataclass(frozen=True, slots=True)
class ExecutionUnit:
    """The foundation-owned identity of the thing being executed: ``(task_id, subtask_order)``.

    ``subtask_order`` is ``None`` for the root task and the linear order for a decomposed subtask.
    Durable sessions, evaluations, and decomposition key their per-unit state on this identity.
    """

    task_id: str
    subtask_order: int | None = None

    @property
    def is_root(self) -> bool:
        """True iff this is the root task (not a decomposed subtask)."""
        return self.subtask_order is None


def fingerprint(payload: Mapping[str, object]) -> str:
    """Deterministic, key-order-independent SHA-256 over a canonical JSON serialization.

    The reusable primitive behind ``flow_fingerprint`` (the resolved graph snapshot) and
    ``execution_policy_fingerprint`` (a single resolved execution descriptor), both added in P0.2.
    The caller is responsible for passing a secret-free payload — no redaction happens here.
    """

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
