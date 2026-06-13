"""The resolved check profile artifact (backlog: automatic check discovery, §10).

A machine-readable record of the checks the orchestrator will run, plus the evidence, probe results,
and the discovery-input fingerprint that lets the profile be cached and invalidated. It is
structurally secret-free: it carries argv lists, evidence strings, and paths only — never
environment values or file contents (§12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wastech_orchestrator.checks.model import (
    CheckCandidate,
    CheckSource,
    ResolvedCheck,
)

# The profile's own format version, independent of the config CONFIG_SCHEMA_VERSION.
PROFILE_SCHEMA_VERSION = 1


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
            )
        except (KeyError, TypeError, ValueError):
            return None  # an unreadable profile is treated as absent (rediscover)
