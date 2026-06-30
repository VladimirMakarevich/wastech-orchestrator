"""Audit log + snapshots (01.4): AC-SF3 (audited, hash-chained, append-only), AC-SF4 (restore)."""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.memory import (
    AuditAction,
    AuditActor,
    AuditContext,
    LongTermKind,
    LongTermRecord,
    MemoryLayout,
    MemoryService,
    TrustLevel,
    content_hash,
)

_AUDIT = AuditContext(timestamp="2026-06-30T00:00:00Z", actor=AuditActor.FINALIZER, rationale="why")


def _semantic(statement: str, memory_id: str = "ltm1") -> LongTermRecord:
    return LongTermRecord(
        memory_id=memory_id,
        kind=LongTermKind.SEMANTIC,
        subject="s",
        statement=statement,
        trust_level=TrustLevel.HUMAN_CURATED,
    )


def test_each_append_writes_one_audit_row_with_hashes(tmp_path: Path) -> None:
    # AC-SF3: every mutation writes exactly one audit row with pre/post hashes + rationale.
    service = MemoryService(MemoryLayout.for_repo(tmp_path))
    service.append(_semantic("first"), audit=_AUDIT)
    rows = service.audit.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "append"
    assert row["affected_ids"] == ["ltm1"]
    assert row["pre_hash"] is None  # the file did not exist before the first append
    assert isinstance(row["post_hash"], str) and row["post_hash"]
    assert row["rationale"] == "why"
    # A second mutation appends exactly one more row.
    service.append(_semantic("second", memory_id="ltm2"), audit=_AUDIT)
    assert len(service.audit.rows()) == 2


def test_audit_chain_verifies_and_detects_tampering(tmp_path: Path) -> None:
    service = MemoryService(MemoryLayout.for_repo(tmp_path))
    service.append(_semantic("a"), audit=_AUDIT)
    service.append(_semantic("b", memory_id="ltm2"), audit=_AUDIT)
    assert service.audit.verify_chain() is True
    # Tamper with a row in place → the recomputed hash no longer matches.
    log = service.audit.path
    tampered = log.read_text(encoding="utf-8").replace('"rationale": "why"', '"rationale": "X"', 1)
    log.write_text(tampered, encoding="utf-8", newline="\n")
    assert service.audit.verify_chain() is False


def test_audit_log_is_append_only(tmp_path: Path) -> None:
    service = MemoryService(MemoryLayout.for_repo(tmp_path))
    service.append(_semantic("a"), audit=_AUDIT)
    first = service.audit.path.read_text(encoding="utf-8")
    service.append(_semantic("b", memory_id="ltm2"), audit=_AUDIT)
    after = service.audit.path.read_text(encoding="utf-8")
    # Earlier content is a prefix of later content: only appended to, never rewritten in place.
    assert after.startswith(first)


def test_snapshot_mutate_restore_is_byte_identical(tmp_path: Path) -> None:
    # AC-SF4: snapshot → mutate → restore returns byte-identical pre-state.
    service = MemoryService(MemoryLayout.for_repo(tmp_path))
    service.append(_semantic("original"), audit=_AUDIT)
    semantic_file = tmp_path / ".worc" / "memory" / "long_term" / "semantic.jsonl"
    before = semantic_file.read_bytes()
    snapshot = service.snapshot(service.tier_files(), label="2026-06-30T00:00:00Z")
    service.replace_all([_semantic("changed")], action=AuditAction.MERGE, audit=_AUDIT)
    assert semantic_file.read_bytes() != before  # mutated
    service.restore(snapshot, audit=_AUDIT)
    assert semantic_file.read_bytes() == before  # restored exactly


def test_restore_logs_a_rollback_row(tmp_path: Path) -> None:
    service = MemoryService(MemoryLayout.for_repo(tmp_path))
    service.append(_semantic("x"), audit=_AUDIT)
    snapshot = service.snapshot(service.tier_files(), label="snap1")
    service.restore(snapshot, audit=_AUDIT)
    assert [row["action"] for row in service.audit.rows()][-1] == "rollback"


def test_snapshot_label_is_filesystem_safe(tmp_path: Path) -> None:
    # A colon-bearing timestamp must not break on Windows; the label is sanitized to a safe name.
    service = MemoryService(MemoryLayout.for_repo(tmp_path))
    service.append(_semantic("x"), audit=_AUDIT)
    snapshot = service.snapshot(service.tier_files(), label="2026-06-30T12:00:00Z")
    assert ":" not in snapshot.name
    assert snapshot.is_dir()


def test_content_hash_is_deterministic() -> None:
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")
