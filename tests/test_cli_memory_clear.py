"""`worc memory clear`: reversible per-tier content clear + irreversible --purge; confirm gates."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tests.conftest import build_git_config
from wastech_orchestrator import cli
from wastech_orchestrator.config.schema import OrchestratorConfig
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

_AUDIT = AuditContext(timestamp="2026-07-08T00:00:00Z")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture
def layout(clone: Path) -> MemoryLayout:
    return MemoryLayout.for_repo(clone)


def _config(clone: Path) -> OrchestratorConfig:
    return build_git_config(clone, memory_enabled=True)


def _populate(layout: MemoryLayout) -> MemoryService:
    service = MemoryService(layout)
    service.append(
        EpisodeRecord(
            id="ep1",
            task_id="t1",
            created_at="2026-07-07T00:00:00Z",
            trust_level=TrustLevel.ARTIFACT_BACKED,
        ),
        audit=_AUDIT,
    )
    service.append(
        LongTermRecord(
            memory_id="m1",
            kind=LongTermKind.SEMANTIC,
            subject="s",
            statement="x",
            trust_level=TrustLevel.HUMAN_CURATED,
        ),
        audit=_AUDIT,
    )
    service.append(
        EntityRecord(
            entity_id="e1",
            entity_type="module",
            canonical_name="src/a.py",
            trust_level=TrustLevel.REPO_OBSERVED,
            paths=("src/a.py",),
        ),
        audit=_AUDIT,
    )
    service.replace_quarantine(
        [{"memory_id": "q1", "statement": "x"}], action=AuditAction.QUARANTINE, audit=_AUDIT
    )
    return service


def _clear(config: OrchestratorConfig, layout: MemoryLayout, **over: object) -> int:
    base: dict[str, object] = {"kind": None, "purge": False, "dry_run": False, "yes": True}
    base.update(over)
    return cli._cmd_memory_clear(config, layout, **base)  # type: ignore[arg-type]


def test_clear_all_empties_tiers_and_keeps_audit(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _populate(layout)
    assert _clear(_config(clone), layout) == 0
    assert service.read_episodes() == []
    assert service.read_entities() == []
    assert service.read_quarantine() == []
    assert all(service.read_long_term(kind) == [] for kind in LongTermKind)
    # Store + audit chain preserved; a reversible clear- snapshot was taken.
    assert layout.root.exists()
    assert service.audit.verify_chain()
    assert any(name.startswith("clear-") for name in cli._snapshot_labels(layout))
    out = capsys.readouterr().out
    assert "cleared 4 record(s)" in out and "snapshot:" in out


def test_clear_kind_long_leaves_other_tiers(clone: Path, layout: MemoryLayout) -> None:
    service = _populate(layout)
    assert _clear(_config(clone), layout, kind="long") == 0
    assert all(service.read_long_term(kind) == [] for kind in LongTermKind)
    assert len(service.read_episodes()) == 1
    assert len(service.read_entities()) == 1
    assert len(service.read_quarantine()) == 1


def test_clear_kind_quarantine_leaves_other_tiers(clone: Path, layout: MemoryLayout) -> None:
    service = _populate(layout)
    assert _clear(_config(clone), layout, kind="quarantine") == 0
    assert service.read_quarantine() == []
    assert len(service.read_episodes()) == 1
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1
    assert len(service.read_entities()) == 1


def test_clear_dry_run_writes_nothing(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _populate(layout)
    assert _clear(_config(clone), layout, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "would clear 4 record(s)" in out
    assert len(service.read_episodes()) == 1  # unchanged
    assert cli._snapshot_labels(layout) == []  # no snapshot taken on a dry-run


def test_clear_aborts_when_not_confirmed(
    clone: Path,
    layout: MemoryLayout,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _populate(layout)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert _clear(_config(clone), layout, yes=False) == 0
    assert "aborted" in capsys.readouterr().out
    assert len(service.read_episodes()) == 1  # nothing removed


def test_clear_already_empty_is_a_noop(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    layout.ensure_tree()  # store exists but holds no records
    assert _clear(_config(clone), layout) == 0
    assert "already empty" in capsys.readouterr().out


def test_clear_no_store(
    clone: Path, layout: MemoryLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _clear(_config(clone), layout) == 0
    assert "no store yet" in capsys.readouterr().out


def test_clear_refused_while_task_active(
    clone: Path, layout: MemoryLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _populate(layout)
    monkeypatch.setattr(cli, "has_active_task", lambda _config: True)
    assert _clear(_config(clone), layout) == 1
    assert len(service.read_episodes()) == 1  # nothing changed


def test_clear_is_reversible_via_restore(clone: Path, layout: MemoryLayout) -> None:
    service = _populate(layout)
    config = _config(clone)
    assert _clear(config, layout) == 0
    assert service.read_episodes() == []
    # The clear- snapshot is the most recent → restore with the default (latest) snapshot.
    assert cli._cmd_memory_restore(config, layout, snapshot=None, dry_run=False) == 0
    assert len(service.read_episodes()) == 1
    assert len(service.read_entities()) == 1
    assert len(service.read_quarantine()) == 1
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1


def test_purge_removes_whole_store(clone: Path, layout: MemoryLayout) -> None:
    _populate(layout)
    assert layout.root.exists()
    assert _clear(_config(clone), layout, purge=True) == 0
    assert not layout.root.exists()


def test_purge_requires_literal_yes(
    clone: Path, layout: MemoryLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    _populate(layout)
    config = _config(clone)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")  # not the literal YES
    assert _clear(config, layout, purge=True, yes=False) == 0
    assert layout.root.exists()  # declined → store intact
    monkeypatch.setattr("builtins.input", lambda _prompt="": "YES")
    assert _clear(config, layout, purge=True, yes=False) == 0
    assert not layout.root.exists()


def test_purge_and_kind_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["memory", "clear", "--purge", "--kind", "long"])
    assert exc.value.code == 2


def test_disabled_memory_clear_is_a_noop(
    clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = build_git_config(clone, memory_enabled=False)
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    args = argparse.Namespace(
        memory_action="clear", kind=None, purge=False, dry_run=False, yes=True, log_level=None
    )
    assert cli.cmd_memory(args) == 0
    assert "disabled" in capsys.readouterr().out
