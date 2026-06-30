"""`worc memory` CLI (04.1): show / validate (read-only); compact / restore (mutating)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import build_git_config
from wastech_orchestrator import cli
from wastech_orchestrator.memory import (
    AuditAction,
    AuditContext,
    EntityRecord,
    EpisodeRecord,
    LongTermKind,
    LongTermRecord,
    MemoryLayout,
    MemoryService,
    TrustLevel,
)

_AUDIT = AuditContext(timestamp="2026-06-30T00:00:00Z")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture
def layout(clone: Path) -> MemoryLayout:
    return MemoryLayout.for_repo(clone)


def _config(clone: Path) -> object:
    return build_git_config(clone, memory_enabled=True)


def _populate(layout: MemoryLayout) -> MemoryService:
    service = MemoryService(layout)
    service.append(
        EpisodeRecord(
            id="ep1", task_id="t1", created_at="2026-06-29T00:00:00Z",
            trust_level=TrustLevel.ARTIFACT_BACKED,
        ),
        audit=_AUDIT,
    )
    service.append(
        LongTermRecord(
            memory_id="m1", kind=LongTermKind.SEMANTIC, subject="s", statement="x",
            trust_level=TrustLevel.HUMAN_CURATED,
        ),
        audit=_AUDIT,
    )
    service.append(
        EntityRecord(
            entity_id="e1", entity_type="module", canonical_name="src/gone.py",
            trust_level=TrustLevel.REPO_OBSERVED, paths=("src/gone.py",),
        ),
        audit=_AUDIT,
    )
    return service


def test_show_summarizes_store(
    layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    _populate(layout)
    assert cli._cmd_memory_show(layout) == 0
    out = capsys.readouterr().out
    assert "episodes (short-term): 1" in out
    assert "long-term: 1" in out
    assert "entities: 1" in out
    assert "chain intact" in out


def test_show_no_store_is_clean(layout: MemoryLayout, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._cmd_memory_show(layout) == 0
    assert "no store yet" in capsys.readouterr().out


def test_validate_reports_stale_entity_read_only(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _populate(layout)
    config = _config(clone)
    assert cli._cmd_memory_validate(config, layout) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "1 stale" in out and "e1" in out
    # read-only: the entity is still active, nothing quarantined.
    assert len(service.read_entities()) == 1
    assert service.read_quarantine() == []


def test_compact_dry_run_writes_nothing(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _populate(layout)
    assert cli._cmd_memory_compact(_config(clone), layout, dry_run=True) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "dry-run" in out and "quarantine 1" in out
    assert len(service.read_entities()) == 1  # unchanged
    assert service.read_quarantine() == []


def test_compact_executes_and_quarantines(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _populate(layout)
    assert cli._cmd_memory_compact(_config(clone), layout, dry_run=False) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "done" in out and "snapshot:" in out
    assert service.read_entities() == []  # stale entity moved out
    assert len(service.read_quarantine()) == 1


def test_compact_refused_while_task_active(
    clone: Path, layout: MemoryLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    _populate(layout)
    monkeypatch.setattr(cli, "has_active_task", lambda _config: True)
    assert cli._cmd_memory_compact(_config(clone), layout, dry_run=False) == 1  # type: ignore[arg-type]


def test_restore_dry_run_then_rollback(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _populate(layout)
    snapshot = service.snapshot(service.tier_files(), label="snap-1")
    assert snapshot.is_dir()
    # Mutate after the snapshot: drop the long-term lesson.
    service.replace_long_term(LongTermKind.SEMANTIC, [], action=AuditAction.PRUNE, audit=_AUDIT)
    assert service.read_long_term(LongTermKind.SEMANTIC) == []

    config = _config(clone)
    assert cli._cmd_memory_restore(config, layout, snapshot=None, dry_run=True) == 0  # type: ignore[arg-type]
    assert "dry-run" in capsys.readouterr().out
    assert service.read_long_term(LongTermKind.SEMANTIC) == []  # dry-run changed nothing

    assert cli._cmd_memory_restore(config, layout, snapshot=None, dry_run=False) == 0  # type: ignore[arg-type]
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1  # rolled back (AC-SF4)


def test_restore_no_snapshots(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    _populate(layout)
    assert cli._cmd_memory_restore(_config(clone), layout, snapshot=None, dry_run=False) == 1  # type: ignore[arg-type]
    assert "no snapshots" in capsys.readouterr().out


def test_idle_hook_runs_then_rate_limits(
    clone: Path, layout: MemoryLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stale entity (path neither tracked nor on disk) is quarantined on the first pass; a second
    # pass within cleanup_min_interval_s is rate-limited; a pass after the interval runs again.
    config = build_git_config(clone, memory_enabled=True)
    clock = {"t": 1000.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    def _add_stale(entity_id: str) -> None:
        MemoryService(layout).append(
            EntityRecord(
                entity_id=entity_id, entity_type="module", canonical_name="src/gone.py",
                trust_level=TrustLevel.REPO_OBSERVED, paths=("src/gone.py",),
            ),
            audit=_AUDIT,
        )

    _add_stale("e1")
    hook = cli._build_cleanup_hook(config)
    assert hook is not None
    hook()  # first pass: runs
    assert len(MemoryService(layout).read_quarantine()) == 1

    _add_stale("e2")
    clock["t"] = 1100.0  # +100s < min_interval (300s) → rate-limited, no run
    hook()
    assert len(MemoryService(layout).read_entities()) == 1  # e2 still active

    clock["t"] = 1500.0  # +500s ≥ min_interval → runs again
    hook()
    assert MemoryService(layout).read_entities() == []
    assert len(MemoryService(layout).read_quarantine()) == 2


def test_disabled_memory_is_a_noop(
    clone: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_git_config(clone, memory_enabled=False)
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    import argparse

    args = argparse.Namespace(memory_action="show", log_level=None)
    assert cli.cmd_memory(args) == 0
    assert "disabled" in capsys.readouterr().out
