"""Tests for the terminal-exchange sealing protocol (WRI-007).

Exercises :mod:`wastech_orchestrator.core.flow.exchange_seal` directly against a real temp exchange
+ private home: seal → verified private snapshot + removed active dir; restore-for-continue; the
resume decision seam (reuse / restore / empty); contaminated-tree quarantine; and the cross-platform
failure paths (checksum mismatch, corrupt manifest, symlink escape, injected Windows lock + retry
exhaustion) driven deterministically so they run on any host (the real Windows attrs are the WRI-006
gate's job).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.exchange_seal import (
    MANIFEST_NAME,
    ExchangeCleanupBlocked,
    ExchangeSealError,
    ensure_current_exchange,
    exchange_seal_root,
    quarantine_contaminated,
    restore_for_continue,
    seal_exchange,
)
from wastech_orchestrator.providers.artifacts import exchange_task_dir
from wastech_orchestrator.providers.exchange import ExchangeError, build_exchange_manifest

TASK_ID = "add-http-retry"


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(exchange_root, private_home)`` under a temp repo."""
    return tmp_path / ".worc-io", tmp_path / ".worc"


def _populate(exchange_root: Path, task_id: str = TASK_ID) -> Path:
    """Write a representative active exchange for ``task_id`` and return its dir."""
    task_dir = exchange_task_dir(exchange_root, task_id)
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text("the plan\n", encoding="utf-8")
    (task_dir / "current.diff").write_text("diff --git a b\n", encoding="utf-8")
    run = task_dir / "stages" / "review" / "run-000009"
    run.mkdir(parents=True)
    (run / "findings.json").write_text('{"findings": []}\n', encoding="utf-8")
    return task_dir


def _noop_sleep(_seconds: float) -> None:
    pass


# --- seal ----------------------------------------------------------------------------------------


def test_seal_creates_verified_snapshot_and_removes_active(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    task_dir = _populate(exchange_root)

    result = seal_exchange(exchange_root, private_home, TASK_ID, metadata={"final_status": "done"})

    assert result is not None
    # The active in-repo exchange is gone; the exchange root holds no task dir for the next task.
    assert not task_dir.exists()
    # A verified snapshot survives privately, with a manifest and every curated file.
    assert result.seal_dir == exchange_seal_root(private_home, TASK_ID) / "seal-000001"
    assert (result.seal_dir / MANIFEST_NAME).is_file()
    assert (result.seal_dir / "plan.md").read_text(encoding="utf-8") == "the plan\n"
    assert (result.seal_dir / "stages" / "review" / "run-000009" / "findings.json").is_file()
    manifest = json.loads((result.seal_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["task_id"] == TASK_ID
    assert manifest["seal_no"] == 1
    assert manifest["metadata"]["final_status"] == "done"
    assert result.entry_count == 3


def test_seal_no_active_exchange_is_noop(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    assert seal_exchange(exchange_root, private_home, TASK_ID) is None


def test_seal_versions_increment(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)
    first = seal_exchange(exchange_root, private_home, TASK_ID)
    _populate(exchange_root)  # a second terminal (e.g. FAILED → continue → FAILED)
    second = seal_exchange(exchange_root, private_home, TASK_ID)
    assert first is not None and second is not None
    assert first.seal_dir.name == "seal-000001"
    assert second.seal_dir.name == "seal-000002"


def test_seal_fails_closed_on_symlink_in_exchange(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    task_dir = _populate(exchange_root)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        (task_dir / "link").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this host")
    with pytest.raises(ExchangeError):
        seal_exchange(exchange_root, private_home, TASK_ID)


# --- Windows lock / retry (injected remover) -----------------------------------------------------


def test_seal_retries_then_succeeds_on_transient_lock(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)
    calls = {"n": 0}

    def flaky_remover(path: Path) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("sharing violation")
        # Third attempt succeeds — remove for real.
        import shutil

        shutil.rmtree(path)

    result = seal_exchange(
        exchange_root, private_home, TASK_ID, remover=flaky_remover, sleeper=_noop_sleep
    )
    assert result is not None
    assert calls["n"] == 3
    assert not exchange_task_dir(exchange_root, TASK_ID).exists()


def test_seal_blocked_when_lock_never_clears(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)

    def stuck_remover(_path: Path) -> None:
        raise PermissionError("locked by another process")

    with pytest.raises(ExchangeCleanupBlocked):
        seal_exchange(
            exchange_root, private_home, TASK_ID, remover=stuck_remover, sleeper=_noop_sleep
        )
    # The snapshot was still sealed before removal was attempted (restore stays possible).
    assert (exchange_seal_root(private_home, TASK_ID) / "seal-000001" / MANIFEST_NAME).is_file()


# --- restore -------------------------------------------------------------------------------------


def test_restore_materializes_and_verifies_latest(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)
    seal_exchange(exchange_root, private_home, TASK_ID)

    result = restore_for_continue(exchange_root, private_home, TASK_ID)

    task_dir = exchange_task_dir(exchange_root, TASK_ID)
    assert result.restored is True
    assert result.task_dir == task_dir
    assert (task_dir / "plan.md").read_text(encoding="utf-8") == "the plan\n"
    assert (task_dir / "stages" / "review" / "run-000009" / "findings.json").is_file()
    # The snapshot's own manifest is metadata, not exchange content — never restored into the tree.
    assert not (task_dir / MANIFEST_NAME).exists()


def test_restore_refuses_missing_snapshot(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    with pytest.raises(ExchangeSealError, match="no sealed exchange snapshot"):
        restore_for_continue(exchange_root, private_home, TASK_ID)


def test_restore_refuses_over_existing_active(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)
    seal_exchange(exchange_root, private_home, TASK_ID)
    _populate(exchange_root)  # a stale active dir reappears
    with pytest.raises(ExchangeSealError, match="state conflict"):
        restore_for_continue(exchange_root, private_home, TASK_ID)


def test_restore_detects_checksum_mismatch(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)
    seal = seal_exchange(exchange_root, private_home, TASK_ID)
    assert seal is not None
    # Tamper a sealed file's bytes without touching the recorded manifest digest.
    (seal.seal_dir / "plan.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ExchangeSealError, match="drifted"):
        restore_for_continue(exchange_root, private_home, TASK_ID)


def test_restore_detects_corrupt_manifest(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)
    seal = seal_exchange(exchange_root, private_home, TASK_ID)
    assert seal is not None
    (seal.seal_dir / MANIFEST_NAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(ExchangeSealError, match="cannot read exchange seal manifest"):
        restore_for_continue(exchange_root, private_home, TASK_ID)


# --- ensure_current_exchange (the resume decision seam) ------------------------------------------


def test_ensure_reuses_active_exchange(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    task_dir = _populate(exchange_root)
    marker = task_dir / "plan.md"
    result = ensure_current_exchange(
        exchange_root, private_home, TASK_ID, contaminated=False, active_unsafe=False
    )
    assert result.restored is False
    # A parked/crashed continue must not overwrite the live exchange from an older snapshot.
    assert marker.read_text(encoding="utf-8") == "the plan\n"


def test_ensure_restores_when_no_active(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    _populate(exchange_root)
    seal_exchange(exchange_root, private_home, TASK_ID)
    result = ensure_current_exchange(
        exchange_root, private_home, TASK_ID, contaminated=False, active_unsafe=False
    )
    assert result.restored is True
    assert exchange_task_dir(exchange_root, TASK_ID).is_dir()


def test_ensure_empty_when_no_active_and_no_seal(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    result = ensure_current_exchange(
        exchange_root, private_home, TASK_ID, contaminated=False, active_unsafe=False
    )
    assert (
        result.restored is False
    )  # legacy/never-published continue proceeds with an empty exchange


def test_ensure_refuses_contaminated(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    with pytest.raises(ExchangeSealError, match="contaminated"):
        ensure_current_exchange(
            exchange_root, private_home, TASK_ID, contaminated=True, active_unsafe=False
        )


def test_ensure_refuses_unsafe(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    with pytest.raises(ExchangeSealError, match="unsafe"):
        ensure_current_exchange(
            exchange_root, private_home, TASK_ID, contaminated=False, active_unsafe=True
        )


# --- quarantine ----------------------------------------------------------------------------------


def test_quarantine_moves_tree_and_records_evidence(tmp_path: Path) -> None:
    exchange_root, private_home = _roots(tmp_path)
    task_dir = _populate(exchange_root)
    expected = build_exchange_manifest(task_dir, TASK_ID)

    evidence = quarantine_contaminated(
        exchange_root,
        private_home,
        TASK_ID,
        expected=expected,
        observed_changes=("content changed 'plan.md'",),
    )

    assert not task_dir.exists()  # removed from the active root
    assert (evidence / "tree" / "plan.md").is_file()  # relocated as evidence
    doc = json.loads((evidence / "evidence.json").read_text(encoding="utf-8"))
    assert doc["observed_changes"] == ["content changed 'plan.md'"]
    assert doc["expected_manifest"]["manifest_digest"]
