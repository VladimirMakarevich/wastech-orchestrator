"""Resolved-profile persistence round-trip (automatic check discovery)."""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.checks.model import CheckSource, ResolvedCheck
from wastech_orchestrator.checks.profile import PROFILE_SCHEMA_VERSION, ResolvedCheckProfile
from wastech_orchestrator.checks.store import ResolvedCheckProfileStore


def _profile() -> ResolvedCheckProfile:
    return ResolvedCheckProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        ready=True,
        source=CheckSource.DETECTED,
        checks=(ResolvedCheck(name="tests", argv=(".venv/bin/pytest",)),),
        candidates=(),
        platform="linux",
        fingerprint="abc123",
        created_at="2026-06-13T00:00:00+00:00",
        last_validated_at="2026-06-13T00:00:00+00:00",
    )


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = ResolvedCheckProfileStore(tmp_path / "checks")
    store.save(_profile())
    loaded = store.load()
    assert loaded is not None
    assert loaded.ready is True
    assert loaded.checks[0].argv == (".venv/bin/pytest",)
    assert loaded.fingerprint == "abc123"


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert ResolvedCheckProfileStore(tmp_path / "checks").load() is None


def test_load_corrupt_returns_none(tmp_path: Path) -> None:
    store = ResolvedCheckProfileStore(tmp_path / "checks")
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not json{", encoding="utf-8")
    assert store.load() is None
