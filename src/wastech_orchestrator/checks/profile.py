"""The resolved check profile artifact (backlog: automatic check discovery).

A machine-readable record of the checks the orchestrator will run, plus the evidence, probe results,
and the discovery-input fingerprint that lets the profile be cached and invalidated. It is
structurally secret-free: it carries argv lists, evidence strings, and paths only — never
environment values or file contents.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from wastech_orchestrator.checks.model import (
    CheckCandidate,
    CheckSource,
    ResolvedCheck,
)

# The profile's own format version, independent of the config CONFIG_SCHEMA_VERSION.
# v2 (2026-06-14): adds commands_signature + the approval fields. A v1 profile lacks them and
# loads with approved=False, which simply triggers an approval on the next *change* to the set.
PROFILE_SCHEMA_VERSION = 2


def commands_signature(checks: Sequence[ResolvedCheck]) -> str:
    """A stable, secret-free hash identifying the *set* of check commands (name + argv).

    Order-independent (sorted) so re-ordering the same checks is not a "change". This is the value a
    changed-command-set approval gate compares against — argv only, never env or file contents.
    """
    parts = sorted(check.name + "\x00" + "\x00".join(check.argv) for check in checks)
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProfileCandidateRecord:
    """One candidate considered during resolution (selected, rejected, or not launchable)."""

    name: str
    argv: tuple[str, ...]
    source: str
    evidence: tuple[str, ...]
    probe_status: str | None
    selected: bool
    rejection: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "source": self.source,
            "evidence": list(self.evidence),
            "probe_status": self.probe_status,
            "selected": self.selected,
            "rejection": self.rejection,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProfileCandidateRecord:
        return cls(
            name=str(data.get("name", "")),
            argv=tuple(str(a) for a in data.get("argv", [])),
            source=str(data.get("source", "")),
            evidence=tuple(str(e) for e in data.get("evidence", [])),
            probe_status=data.get("probe_status"),
            selected=bool(data.get("selected", False)),
            rejection=data.get("rejection"),
        )

    @classmethod
    def from_candidate(
        cls, candidate: CheckCandidate, *, selected: bool, rejection: str | None = None
    ) -> ProfileCandidateRecord:
        return cls(
            name=candidate.name,
            argv=candidate.argv,
            source=candidate.source.value,
            evidence=candidate.evidence,
            probe_status=candidate.probe_status.value if candidate.probe_status else None,
            selected=selected,
            rejection=rejection,
        )


@dataclass(frozen=True)
class ResolvedCheckProfile:
    """The resolved checks plus the audit trail and the fingerprint used for cache invalidation."""

    schema_version: int
    ready: bool
    source: CheckSource
    checks: tuple[ResolvedCheck, ...]
    candidates: tuple[ProfileCandidateRecord, ...]
    platform: str
    fingerprint: str
    created_at: str
    last_validated_at: str
    notes: tuple[str, ...] = ()
    # The sensitive-change approval gate: a stable hash of the selected command set, whether
    # the operator approved *this* set, and a secret-free link to the approving HITL interaction.
    commands_signature: str = ""
    approved: bool = False
    approved_at: str = ""
    approved_interaction_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ready": self.ready,
            "source": self.source.value,
            "checks": [{"name": c.name, "argv": list(c.argv)} for c in self.checks],
            "candidates": [c.to_json() for c in self.candidates],
            "platform": self.platform,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "last_validated_at": self.last_validated_at,
            "notes": list(self.notes),
            "commands_signature": self.commands_signature,
            "approved": self.approved,
            "approved_at": self.approved_at,
            "approved_interaction_id": self.approved_interaction_id,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ResolvedCheckProfile | None:
        try:
            source = CheckSource(str(data["source"]))
            checks = tuple(
                ResolvedCheck(name=str(c["name"]), argv=tuple(str(a) for a in c["argv"]))
                for c in data["checks"]
            )
            candidates = tuple(
                ProfileCandidateRecord.from_json(c) for c in data.get("candidates", [])
            )
            return cls(
                schema_version=int(data["schema_version"]),
                ready=bool(data["ready"]),
                source=source,
                checks=checks,
                candidates=candidates,
                platform=str(data.get("platform", "")),
                fingerprint=str(data.get("fingerprint", "")),
                created_at=str(data.get("created_at", "")),
                last_validated_at=str(data.get("last_validated_at", "")),
                notes=tuple(str(n) for n in data.get("notes", [])),
                commands_signature=str(data.get("commands_signature", "")),
                approved=bool(data.get("approved", False)),
                approved_at=str(data.get("approved_at", "")),
                approved_interaction_id=str(data.get("approved_interaction_id", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None  # an unreadable profile is treated as absent (rediscover)
