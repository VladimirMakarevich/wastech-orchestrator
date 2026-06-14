"""Check resolution — the deterministic pipeline that produces a launchable profile (§4, §5, §11).

Orchestrates RepositoryInspector -> CheckCandidateDetector -> (optional AgentCheckDiscovery) ->
CheckCandidateValidator -> CheckProbeRunner -> ResolvedCheckProfileStore, honoring
``checks.discovery.mode`` and the cache/fingerprint rules. Provider-agnostic: it proposes argv
lists; the orchestrator-owned Check Runner remains the sole quality-gate authority.

Mode semantics:

* ``configured`` (default): use ``checks.commands`` as-is; ``ready=True`` even when empty (the
  backward-compatible path — the runtime launch/quality split still protects the fix budget).
* ``deterministic``/``auto``: discover the launchable profile, preferring configured commands when
  they probe launchable; ``auto`` additionally runs the agent fallback when confidence is low. An
  empty result is ``ready=False`` (stop before any branch — §11).
* ``disabled``: an explicit zero-check profile with a prominent warning note.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from wastech_orchestrator.checks.detect import CheckCandidateDetector
from wastech_orchestrator.checks.fingerprint import compute_fingerprint
from wastech_orchestrator.checks.inspect import RepositoryEvidence, RepositoryInspector
from wastech_orchestrator.checks.model import (
    CheckCandidate,
    CheckSource,
    Confidence,
    ProbeStatus,
    ResolvedCheck,
    normalize_commands,
)
from wastech_orchestrator.checks.probe import CheckProbeRunner
from wastech_orchestrator.checks.profile import (
    PROFILE_SCHEMA_VERSION,
    ProfileCandidateRecord,
    ResolvedCheckProfile,
    commands_signature,
)
from wastech_orchestrator.checks.store import ResolvedCheckProfileStore
from wastech_orchestrator.checks.validate import CheckCandidateValidator
from wastech_orchestrator.config.schema import (
    CheckDiscoveryMode,
    CheckRefreshPolicy,
    OrchestratorConfig,
)
from wastech_orchestrator.providers.process import ProcessResult, run_process

RunProcess = Callable[..., ProcessResult]

# The combined project-owned wrapper check; when launchable it supersedes per-language checks (§17).
_WRAPPER_NAME = "checks"


class ReResolveReason(StrEnum):
    """Why a mid-task re-resolve was triggered — only ever *infrastructure proof*, never a quality
    failure (§1.2). Recorded in the profile notes for audit."""

    LAUNCH_FAILED = "launch_failed"
    FINGERPRINT_CHANGED = "fingerprint_changed"
    LOW_CONFIDENCE = "low_confidence"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CheckResolver:
    """Resolve (or load) the repository's :class:`ResolvedCheckProfile`."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        repo_root: str | Path,
        artifacts_root: str | Path,
        run_process: RunProcess = run_process,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], str] = _now_iso,
        discovery: object | None = None,  # AgentCheckDiscovery (Phase 3); inert when None
    ) -> None:
        self._config = config
        self._repo_root = Path(repo_root)
        self._store = ResolvedCheckProfileStore(Path(artifacts_root) / "checks")
        self._clock = clock
        self._discovery = discovery
        self._inspector = RepositoryInspector(
            repo_root, denied_read_paths=config.security.denied_read_paths
        )
        self._detector = CheckCandidateDetector()
        self._validator = CheckCandidateValidator(denied_commands=config.security.denied_commands)
        self._prober = CheckProbeRunner(
            repo_root=repo_root,
            allowed_environment=config.security.allowed_environment,
            run_process=run_process,
            which=which,
        )

    @property
    def store(self) -> ResolvedCheckProfileStore:
        return self._store

    def resolve(self, *, allow_agent: bool = False, refresh: bool = False) -> ResolvedCheckProfile:
        """Return a resolved profile, reusing the cached one when the fingerprint is unchanged."""
        discovery_cfg = self._config.checks.discovery
        fingerprint = compute_fingerprint(self._repo_root)

        if not refresh and discovery_cfg.refresh is not CheckRefreshPolicy.ALWAYS:
            cached = self._store.load()
            if cached is not None and (
                discovery_cfg.refresh is CheckRefreshPolicy.NEVER
                or cached.fingerprint == fingerprint
            ):
                return cached

        profile = self._resolve_fresh(discovery_cfg.mode, allow_agent, fingerprint)
        self._store.save(profile)
        return profile

    def reresolve(self, *, allow_agent: bool, reason: ReResolveReason) -> ResolvedCheckProfile:
        """Force a fresh resolve (ignoring the cache) because of *infrastructure proof* (§1.2).

        Used only when there is real evidence the command is wrong — a launch failure, a changed
        config/CI fingerprint, or low-confidence detection — never because a check *reported*
        failures (that would let the gate quietly rewrite its own command until it passes). The
        reason is stamped into the profile notes for audit.
        """
        fingerprint = compute_fingerprint(self._repo_root)
        profile = self._resolve_fresh(self._config.checks.discovery.mode, allow_agent, fingerprint)
        profile = replace(profile, notes=(*profile.notes, f"re-resolved: {reason.value}"))
        self._store.save(profile)
        return profile

    # --- resolution --------------------------------------------------------------------------

    def _resolve_fresh(
        self, mode: CheckDiscoveryMode, allow_agent: bool, fingerprint: str
    ) -> ResolvedCheckProfile:
        configured = normalize_commands(self._config.checks.commands)

        if mode is CheckDiscoveryMode.DISABLED:
            return self._profile(
                ready=True,
                source=CheckSource.DISABLED,
                checks=(),
                records=(),
                fingerprint=fingerprint,
                notes=("discovery mode 'disabled': the quality gate is OFF — no checks will run",),
            )

        if mode is CheckDiscoveryMode.CONFIGURED:
            return self._configured_profile(configured, fingerprint)

        # deterministic / auto: discover, preferring configured commands when launchable.
        return self._discovered_profile(configured, mode, allow_agent, fingerprint)

    def _configured_profile(
        self, configured: list[ResolvedCheck], fingerprint: str
    ) -> ResolvedCheckProfile:
        """``configured`` mode: trust the operator's commands (probe only for the audit trail)."""
        checks: list[ResolvedCheck] = []
        records: list[ProfileCandidateRecord] = []
        for rc in configured:
            candidate = CheckCandidate(
                name=rc.name,
                argv=rc.argv,
                source=CheckSource.CONFIGURED,
                evidence=("checks.commands",),
                confidence=Confidence.HIGH,
            )
            result = self._validator.validate(candidate)
            if result.candidate is None:
                records.append(
                    ProfileCandidateRecord.from_candidate(
                        candidate, selected=False, rejection=result.rejection
                    )
                )
                continue
            probed = self._prober.probe(result.candidate)
            checks.append(rc)
            records.append(ProfileCandidateRecord.from_candidate(probed, selected=True))
        return self._profile(
            ready=True,  # configured trusts the operator; the runtime split catches launch failures
            source=CheckSource.CONFIGURED,
            checks=tuple(checks),
            records=tuple(records),
            fingerprint=fingerprint,
        )

    def _discovered_profile(
        self,
        configured: list[ResolvedCheck],
        mode: CheckDiscoveryMode,
        allow_agent: bool,
        fingerprint: str,
    ) -> ResolvedCheckProfile:
        evidence = self._inspector.collect()
        candidates = self._configured_candidates(configured)
        candidates += self._detector.detect(evidence)

        validated = self._validate_and_probe(candidates)
        if (
            mode is CheckDiscoveryMode.AUTO
            and allow_agent
            and self._config.checks.discovery.agent_fallback
            and self._discovery is not None
            and not _has_launchable(validated, "tests")
        ):
            validated += self._run_agent_fallback(evidence)

        selected, records = _select(validated)
        source = _profile_source(selected)
        return self._profile(
            ready=bool(selected),
            source=source,
            checks=tuple(ResolvedCheck(name=c.name, argv=c.argv) for c in selected),
            records=tuple(records),
            fingerprint=fingerprint,
        )

    def _configured_candidates(self, configured: list[ResolvedCheck]) -> list[CheckCandidate]:
        return [
            CheckCandidate(
                name=rc.name,
                argv=rc.argv,
                source=CheckSource.CONFIGURED,
                evidence=("checks.commands",),
                confidence=Confidence.HIGH,
            )
            for rc in configured
        ]

    def _validate_and_probe(self, candidates: list[CheckCandidate]) -> list[CheckCandidate]:
        out: list[CheckCandidate] = []
        for candidate in candidates:
            result = self._validator.validate(candidate)
            if result.candidate is None:
                out.append(
                    CheckCandidate(
                        name=candidate.name,
                        argv=candidate.argv,
                        source=candidate.source,
                        evidence=(*candidate.evidence, f"rejected: {result.rejection}"),
                        confidence=candidate.confidence,
                        probe_status=ProbeStatus.UNSUPPORTED,
                    )
                )
                continue
            out.append(self._prober.probe(result.candidate))
        return out

    def _run_agent_fallback(self, evidence: RepositoryEvidence) -> list[CheckCandidate]:
        # Phase 3 wires AgentCheckDiscovery here; its proposals flow through the same
        # validator + prober as deterministic candidates (the agent is advisory).
        discover = getattr(self._discovery, "discover", None)
        if discover is None:
            return []
        proposals = discover(self._repo_root, evidence)
        return self._validate_and_probe(list(proposals))

    def _profile(
        self,
        *,
        ready: bool,
        source: CheckSource,
        checks: tuple[ResolvedCheck, ...],
        records: tuple[ProfileCandidateRecord, ...],
        fingerprint: str,
        notes: tuple[str, ...] = (),
    ) -> ResolvedCheckProfile:
        now = self._clock()
        return ResolvedCheckProfile(
            schema_version=PROFILE_SCHEMA_VERSION,
            ready=ready,
            source=source,
            checks=checks,
            candidates=records,
            platform=sys.platform,
            fingerprint=fingerprint,
            created_at=now,
            last_validated_at=now,
            notes=notes,
            commands_signature=commands_signature(checks),
        )


def _priority(candidate: CheckCandidate) -> int:
    base = {Confidence.LOW: 1, Confidence.MEDIUM: 2, Confidence.HIGH: 3}[candidate.confidence]
    return 100 + base if candidate.source is CheckSource.CONFIGURED else base


def _has_launchable(candidates: list[CheckCandidate], name: str) -> bool:
    return any(c.name == name and c.probe_status is ProbeStatus.LAUNCHABLE for c in candidates)


def _select(
    candidates: list[CheckCandidate],
) -> tuple[list[CheckCandidate], list[ProfileCandidateRecord]]:
    """Pick the top-priority launchable candidate per logical name; a launchable wrapper wins.

    Pinning (§1.2): when a logical name has a CONFIGURED candidate, only a configured candidate may
    fill that slot — a deliberate operator pin is never silently replaced by a detected fallback. If
    the configured pin does not probe launchable, the name is left unchosen (reported not-ready in
    the records) rather than masked by detection.
    """
    by_name: dict[str, list[tuple[int, int, CheckCandidate]]] = {}
    for index, candidate in enumerate(candidates):
        by_name.setdefault(candidate.name, []).append((_priority(candidate), index, candidate))
    pinned = {c.name for c in candidates if c.source is CheckSource.CONFIGURED}

    chosen: dict[str, CheckCandidate] = {}
    for name, items in by_name.items():
        ordered = sorted(items, key=lambda t: (-t[0], t[1]))
        launchable = [
            c
            for _, _, c in ordered
            if c.probe_status is ProbeStatus.LAUNCHABLE
            and (name not in pinned or c.source is CheckSource.CONFIGURED)
        ]
        if launchable:
            chosen[name] = launchable[0]

    # A launchable project-owned wrapper supersedes the per-language checks (§17).
    if _WRAPPER_NAME in chosen:
        selected = [chosen[_WRAPPER_NAME]]
    else:
        selected = [chosen[name] for name in sorted(chosen) if name != _WRAPPER_NAME]

    selected_ids = {id(c) for c in selected}
    records = [
        ProfileCandidateRecord.from_candidate(c, selected=id(c) in selected_ids) for c in candidates
    ]
    return selected, records


def _profile_source(selected: list[CheckCandidate]) -> CheckSource:
    if any(c.source is CheckSource.AGENT for c in selected):
        return CheckSource.AGENT
    if any(c.source is CheckSource.CONFIGURED for c in selected):
        return CheckSource.CONFIGURED
    return CheckSource.DETECTED
