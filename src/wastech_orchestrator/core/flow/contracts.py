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


class RunKind(StrEnum):
    """Why a run exists, without inventing fake pipeline stages (the foundation run-kind field)."""

    STAGE = "stage"  # produces the deliverable (the author/editor: implementation, fixing, ...)
    EVALUATOR = "evaluator"  # read-only; judges a produced artifact, returns a bounded verdict


class EvaluatorRole(StrEnum):
    """The fine-grained discriminator carried by every ``EVALUATOR`` run.

    These are the in-flow evaluator roles that ship; operator-authored flows may use other role
    strings (they get the default evaluator behavior). Supervision is **not** an evaluator role:
    summary + per-step advisory oversight is a constant orchestrator layer above the flow, not a
    graph node (2026-06-19 revision; see ``docs/backlog/flows/flow-contract.md``).
    """

    REVIEW = "review"
    CRITIC = "critic"
    VERIFIER = "verifier"
    TEST_QUALITY = "test_quality"


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
