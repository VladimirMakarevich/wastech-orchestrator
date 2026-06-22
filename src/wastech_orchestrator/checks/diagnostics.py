"""Operator-facing check diagnostics (automatic check discovery, ops).

Provider-agnostic reporting helpers shared by the CLI: ``check_preflight`` resolves
(deterministically, no provider run) and reports whether the repository's checks are launchable;
``summarize_profile`` renders the cached profile read-only for ``status``. Keeping these out of
``cli.py`` keeps the CLI free of resolution logic.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.checks.model import ProbeStatus
from wastech_orchestrator.checks.profile import ResolvedCheckProfile
from wastech_orchestrator.checks.resolver import CheckResolver
from wastech_orchestrator.checks.store import ResolvedCheckProfileStore
from wastech_orchestrator.config.schema import OrchestratorConfig

_UNLAUNCHABLE = {ProbeStatus.NOT_LAUNCHABLE.value}


def check_preflight(
    config: OrchestratorConfig, artifacts_root: str | Path
) -> tuple[bool, list[str]]:
    """Resolve-or-load the check profile and report ``(ready, lines)`` (deterministic; no agent)."""
    resolver = CheckResolver(
        config, repo_root=config.repo.local_path, artifacts_root=artifacts_root
    )
    profile = resolver.resolve(allow_agent=False)
    return profile.ready, _report_lines(profile)


def load_profile(artifacts_root: str | Path) -> ResolvedCheckProfile | None:
    """Load the cached resolved profile (read-only; never resolves or probes)."""
    return ResolvedCheckProfileStore(Path(artifacts_root) / "checks").load()


def summarize_profile(profile: ResolvedCheckProfile) -> list[str]:
    """A compact read-only summary of the cached profile for ``status``."""
    lines = [
        f"checks_profile: source={profile.source.value}, resolved={len(profile.checks)}, "
        f"ready={profile.ready}, fingerprint={profile.fingerprint[:12]}"
    ]
    for check in profile.checks:
        lines.append(f"  {check.name}: {' '.join(check.argv)}")
    return lines


def _report_lines(profile: ResolvedCheckProfile) -> list[str]:
    verdict = "OK" if profile.ready else "FAIL"
    lines = [f"checks: {verdict} ({len(profile.checks)} resolved, source={profile.source.value})"]
    for check in profile.checks:
        lines.append(f"  - {check.name}: LAUNCHABLE  argv={list(check.argv)}")
    for candidate in profile.candidates:
        if candidate.selected:
            continue
        if candidate.rejection:
            argv = " ".join(candidate.argv)
            lines.append(f"  rejected: {candidate.name} ({argv}) — {candidate.rejection}")
        elif candidate.probe_status in _UNLAUNCHABLE:
            lines.append(
                f"  {candidate.probe_status}: {candidate.name} ({' '.join(candidate.argv)})"
            )
    for note in profile.notes:
        lines.append(f"  ! {note}")
    return lines
