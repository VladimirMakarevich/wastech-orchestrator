"""CleanupJob — the bounded, model-free maintenance pass (design §5 cleanup, §7 bounded autonomy).

Keeps memory from rotting between tasks without ever touching an active task or the repo. It runs in
the ``watch_loop`` idle gap (the single-slot invariant guarantees no active task there) and behind
the operator ``worc memory compact``. Hard invariants:

* **Demote / expire / quarantine / merge only.** It **never** creates a new long-term lesson and
  **never** edits code/docs/skills (AC-C3): there is no promote code path here, so the
  ``cleanup_promotions_per_pass`` config knob is a documentation-only invariant (it stays 0 by
  construction; the runtime never reads it).
* **Snapshot first, then bounded.** A snapshot of the touched tiers precedes the batch (AC-SF4), and
  the idle pass honors the Q1 budget (``max_scanned`` / ``max_edits`` / ``max_wall_clock``); the
  foreground ``compact`` pass (``full=True``) lifts the caps (FR6) but keeps every safety rule.
* **Quarantine, never silent delete** (Q2). A stale target (path/symbol gone) is first remapped by
  basename; failing that it is moved to quarantine, never dropped.
* **No network, model-free, deterministic.** Time is injected; nothing here calls a model.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory.audit import AuditAction, AuditContext
from wastech_orchestrator.memory.derived import DerivedIndex
from wastech_orchestrator.memory.lifecycle import normalize_subject
from wastech_orchestrator.memory.records import LongTermKind
from wastech_orchestrator.memory.service import MemoryService, derive_long_term_id

_QUARANTINED = "quarantined"


@dataclass(frozen=True)
class CleanupReport:
    """A summary of one ``run_once`` pass (for logging/tests). ``promoted`` is always 0 (AC-C3)."""

    ran: bool
    scanned: int = 0
    expired: int = 0  # episodes pruned past TTL
    remapped: int = 0  # entities/lessons whose moved file was remapped by basename
    quarantined: int = 0  # stale entities + lessons moved to quarantine (never deleted)
    merged: int = 0  # duplicate long-term lessons collapsed
    promoted: int = 0  # always 0 — cleanup never creates a long-term lesson
    snapshot: str | None = None


class CleanupJob:
    """One bounded maintenance pass over the canonical store (design §5/§7)."""

    def __init__(
        self,
        service: MemoryService,
        index: DerivedIndex,
        config: MemoryConfig,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._index = index
        self._config = config
        self._monotonic = monotonic

    def run_once(
        self, *, audit: AuditContext, full: bool = False, dry_run: bool = False
    ) -> CleanupReport:
        """Run one pass: snapshot → expire episodes → reconcile entities → reconcile lessons →
        merge duplicates.

        ``full`` (the foreground ``compact`` pass) lifts the scan/edit/wall-clock caps but keeps
        every safety rule. ``dry_run`` computes the same counts but writes nothing (no snapshot, no
        tier rewrite) — the plan ``worc memory compact --dry-run`` prints. Returns a
        :class:`CleanupReport`; ``ran=False`` means there was nothing on disk to act on.
        """
        tier_files = self._service.tier_files()
        if not tier_files:
            return CleanupReport(ran=False)

        label = f"cleanup-{audit.timestamp}"
        snapshot_dir = None if dry_run else self._service.snapshot(tier_files, label=label)
        budget = _Budget(self._config, self._monotonic, full=full)

        expired = self._expire_episodes(audit, budget, dry_run=dry_run)
        remapped, quarantined = self._reconcile_entities(audit, budget, dry_run=dry_run)
        lesson_remapped, lesson_quarantined = self._reconcile_lessons(
            audit, budget, dry_run=dry_run
        )
        remapped += lesson_remapped
        quarantined += lesson_quarantined
        merged = self._merge_long_term_duplicates(audit, budget, dry_run=dry_run)
        return CleanupReport(
            ran=True,
            scanned=budget.scanned,
            expired=expired,
            remapped=remapped,
            quarantined=quarantined,
            merged=merged,
            snapshot=None if snapshot_dir is None else snapshot_dir.as_posix(),
        )

    # --- episodic TTL expiry (design §4: short-term has a TTL; long-term never) -----------

    def _expire_episodes(self, audit: AuditContext, budget: _Budget, *, dry_run: bool) -> int:
        rows = self._service.read_episodes()
        if not rows:
            return 0
        now = _parse(audit.timestamp)
        cutoff = None if now is None else now - timedelta(days=self._config.short_term_ttl_days)
        # Oldest-first, so a tight edit budget expires the stalest episodes before the rest.
        expired = sorted(
            (row for row in rows if _episode_expired(row, now=now, cutoff=cutoff)),
            key=lambda r: str(r.get("created_at") or ""),
        )
        budget.scan(len(rows))
        take = budget.allow_edits(len(expired))
        if take <= 0:
            return 0
        if not dry_run:
            drop_ids = {str(_episode_id(row)) for row in expired[:take]}
            kept = [row for row in rows if str(_episode_id(row)) not in drop_ids]
            self._service.replace_episodes(kept, action=AuditAction.PRUNE, audit=audit)
        budget.spend_edits(take)
        return take

    # --- entity staleness: remap a moved file, else quarantine (Q2; AC-C4) ----------------

    def _reconcile_entities(
        self, audit: AuditContext, budget: _Budget, *, dry_run: bool
    ) -> tuple[int, int]:
        rows = self._service.read_entities()
        if not rows:
            return 0, 0
        kept: list[dict[str, Any]] = []
        newly_quarantined: list[dict[str, Any]] = []
        remapped = quarantined = 0
        for row in rows:
            if not budget.can_scan() or not budget.can_edit() or not _record_active(row):
                kept.append(row)
                continue
            budget.scan(1)
            verdict = self._classify_entity(row)
            if verdict is None:  # fresh — paths still present
                kept.append(row)
                continue
            kind, payload = verdict
            if kind == "remap":
                kept.append(payload)
                remapped += 1
            else:  # "quarantine"
                newly_quarantined.append(payload)
                quarantined += 1
            budget.spend_edits(1)
        if not remapped and not quarantined:
            return 0, 0
        if not dry_run:
            # A remap can rewrite a moved card's key onto a path another card already holds (e.g.
            # the new-path card the supervisor proposed before this pass ran) — collapse the dupe.
            self._service.replace_entities(
                _collapse_entities(kept), action=AuditAction.MERGE, audit=audit
            )
            if newly_quarantined:
                pending = [*self._service.read_quarantine(), *newly_quarantined]
                self._service.replace_quarantine(
                    pending, action=AuditAction.QUARANTINE, audit=audit
                )
        return remapped, quarantined

    def _classify_entity(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """Classify one entity card: ``None`` (fresh), a remapped copy, or a quarantined copy.

        An entity is stale when any of its paths no longer exists. A missing path with exactly one
        same-basename tracked candidate is remapped (the file moved) — the remap rewrites both the
        ``paths`` and the ``canonical_name`` key (F44 keys a card on ``paths[0]``), so a later
        re-proposal at the new path merges into it instead of spawning a duplicate (memory V2 ADR,
        move 3). Any unresolved missing path sends the whole card to quarantine — never a silent
        delete (Q2).
        """
        paths = _str_list(row.get("paths"))
        if not paths:
            return None  # a path-less card can't be assessed for staleness — leave it
        remapped_paths: list[str] = []
        moved = False
        for path in paths:
            if self._index.path_exists(path):
                remapped_paths.append(path)
                continue
            candidates = self._index.find_by_basename(path)
            if len(candidates) == 1:
                remapped_paths.append(candidates[0])
                moved = True
            else:
                return ("quarantine", {**row, "status": _QUARANTINED})
        if moved:
            return ("remap", {**row, "paths": remapped_paths, "canonical_name": remapped_paths[0]})
        return None

    # --- lesson staleness: remap a moved scope path, else quarantine (F2; never deleted) ---------

    def _reconcile_lessons(
        self, audit: AuditContext, budget: _Budget, *, dry_run: bool
    ) -> tuple[int, int]:
        """Remap or quarantine an **active** long-term lesson whose ``scope.paths`` names a vanished
        target.

        Mirrors :meth:`_reconcile_entities`: a missing scope path with exactly one same-basename
        candidate is remapped in place (memory V2 ADR, move 3 — a refactor no longer quarantines a
        durable lesson); any unresolved missing path moves the whole lesson to quarantine, never a
        silent delete (Q2) and never a judgment-based drop. A path-less lesson has no existence
        signal and is left fully intact. The design §5 "contradicted twice" drop stays a later item
        (no contradiction ledger yet). Honors the same scan/edit budget as the other passes.
        """
        remapped_total = quarantined_total = 0
        for kind in LongTermKind:
            if not budget.can_scan() or not budget.can_edit():
                break
            rows = self._service.read_long_term(kind)
            if not rows:
                continue
            kept: list[dict[str, Any]] = []
            newly_quarantined: list[dict[str, Any]] = []
            remapped_here = quarantined_here = 0
            for row in rows:
                if not budget.can_scan() or not budget.can_edit() or not _record_active(row):
                    kept.append(row)
                    continue
                budget.scan(1)
                verdict = self._classify_lesson(kind, row)
                if verdict is None:  # fresh — all scope paths present (or path-less)
                    kept.append(row)
                    continue
                outcome, payload = verdict
                if outcome == "remap":
                    kept.append(payload)
                    remapped_here += 1
                else:
                    newly_quarantined.append(payload)
                    quarantined_here += 1
                budget.spend_edits(1)
            if not remapped_here and not quarantined_here:
                continue
            remapped_total += remapped_here
            quarantined_total += quarantined_here
            if not dry_run:
                # A remap re-derives the moved lesson's id (F30 keys it on scope.paths), which can
                # collide with a row already at the new id — collapse by id before writing.
                action = AuditAction.MERGE if remapped_here else AuditAction.QUARANTINE
                self._service.replace_long_term(
                    kind, _collapse_by_memory_id(kept), action=action, audit=audit
                )
                if newly_quarantined:
                    pending = [*self._service.read_quarantine(), *newly_quarantined]
                    self._service.replace_quarantine(
                        pending, action=AuditAction.QUARANTINE, audit=audit
                    )
        return remapped_total, quarantined_total

    def _classify_lesson(
        self, kind: LongTermKind, row: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]] | None:
        """Classify one active lesson: ``None`` (fresh), a remapped copy, or a quarantined copy.

        A lesson is stale when a ``scope.paths`` entry no longer exists. A missing path with exactly
        one same-basename tracked candidate is remapped (the file moved) — the remap rewrites the
        scope paths AND re-derives the ``memory_id`` (F30 keys it on scope paths), so a later
        re-proposal at the new path merges into it instead of spawning a duplicate (memory V2 ADR,
        move 3). Any unresolved missing path quarantines the whole lesson — never a silent delete
        (Q2). A path-less lesson has no existence signal and is always fresh.
        """
        scope = row.get("scope")
        if not isinstance(scope, Mapping):
            return None  # no (or malformed) scope → no existence signal, always fresh
        paths = _str_list(scope.get("paths"))
        if not paths:
            return None
        remapped_paths: list[str] = []
        moved = False
        for path in paths:
            if self._index.path_exists(path):
                remapped_paths.append(path)
                continue
            candidates = self._index.find_by_basename(path)
            if len(candidates) == 1:
                remapped_paths.append(candidates[0])
                moved = True
            else:
                return ("quarantine", {**row, "status": _QUARANTINED})
        if not moved:
            return None
        subject = str(row.get("subject") or "")
        new_id = derive_long_term_id(kind, subject, remapped_paths)
        return ("remap", {**row, "scope": {**scope, "paths": remapped_paths}, "memory_id": new_id})

    # --- duplicate long-term merge (design §5: keep oldest id, union evidence) -------------

    def _merge_long_term_duplicates(
        self, audit: AuditContext, budget: _Budget, *, dry_run: bool
    ) -> int:
        merged_total = 0
        for kind in LongTermKind:
            if not budget.can_scan() or not budget.can_edit():
                break
            rows = self._service.read_long_term(kind)
            budget.scan(len(rows))
            collapsed, merges = _collapse_duplicates(rows)
            if merges <= 0:
                continue
            take = budget.allow_edits(merges)
            if take < merges:
                continue  # collapse a whole file's duplicates within one edit budget, or none
            if not dry_run:
                self._service.replace_long_term(
                    kind, collapsed, action=AuditAction.MERGE, audit=audit
                )
            budget.spend_edits(merges)
            merged_total += merges
        return merged_total


class _Budget:
    """Bounded-autonomy accounting for one pass (§7). ``full`` lifts the caps for ``compact``."""

    def __init__(self, config: MemoryConfig, monotonic: Callable[[], float], *, full: bool) -> None:
        self._full = full
        self._max_scanned = config.cleanup_max_scanned
        self._max_edits = config.cleanup_max_edits
        self._max_wall_clock = config.cleanup_max_wall_clock_s
        self._monotonic = monotonic
        self._start = monotonic()
        self.scanned = 0
        self.edits = 0

    def _within_clock(self) -> bool:
        return self._full or (self._monotonic() - self._start) < self._max_wall_clock

    def can_scan(self) -> bool:
        return self._within_clock() and (self._full or self.scanned < self._max_scanned)

    def can_edit(self) -> bool:
        return self._within_clock() and (self._full or self.edits < self._max_edits)

    def scan(self, n: int) -> None:
        self.scanned += n

    def allow_edits(self, wanted: int) -> int:
        """How many of ``wanted`` edits the remaining budget permits (all of them when ``full``)."""
        if self._full:
            return wanted
        return max(0, min(wanted, self._max_edits - self.edits))

    def spend_edits(self, n: int) -> None:
        self.edits += n


# --- module-level pure helpers ------------------------------------------------


def _episode_expired(
    row: Mapping[str, Any], *, now: datetime | None, cutoff: datetime | None
) -> bool:
    """Whether an episode is past its TTL: an explicit ``expires_at`` already reached (vs ``now``),
    else ``created_at`` older than ``cutoff`` (= now − ttl). An unparseable/absent stamp is never
    expired (fail-closed — keep it)."""
    explicit = _parse(row.get("expires_at"))
    if explicit is not None:
        return now is not None and explicit <= now
    if cutoff is None:
        return False
    created = _parse(row.get("created_at"))
    return created is not None and created < cutoff


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _episode_id(row: Mapping[str, Any]) -> Any:
    return row.get("id")


def _record_active(row: Mapping[str, Any]) -> bool:
    return str(row.get("status") or "active") == "active"


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _collapse_duplicates(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse long-term rows sharing a normalized subject: keep the first, union evidence + seen
    tasks onto it, drop the rest. Returns ``(collapsed_rows, merge_count)``; the input order of the
    survivors is preserved (deterministic)."""
    by_subject: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    merges = 0
    for row in rows:
        subject = row.get("subject")
        key = normalize_subject(subject) if isinstance(subject, str) else f"\0{id(row)}"
        if key not in by_subject:
            by_subject[key] = dict(row)
            order.append(key)
            continue
        _absorb(by_subject[key], row)
        merges += 1
    return [by_subject[key] for key in order], merges


def _absorb(keeper: dict[str, Any], other: Mapping[str, Any]) -> None:
    """Fold ``other`` into ``keeper`` (same subject): union evidence + seen tasks, keep counts."""
    evidence = [it for it in (keeper.get("evidence") or []) if isinstance(it, Mapping)]
    have = {(item.get("type"), item.get("ref")) for item in evidence}
    for item in other.get("evidence") or []:
        if isinstance(item, Mapping) and (item.get("type"), item.get("ref")) not in have:
            evidence.append(dict(item))
            have.add((item.get("type"), item.get("ref")))
    keeper["evidence"] = evidence
    seen = list(
        dict.fromkeys(
            [*_str_list(keeper.get("seen_task_ids")), *_str_list(other.get("seen_task_ids"))]
        )
    )
    keeper["seen_task_ids"] = seen
    keeper["usage_count"] = max(int(keeper.get("usage_count") or 0), len(seen))


def _collapse_by_memory_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse long-term rows sharing a ``memory_id`` — created when a lesson remap re-derives a
    moved lesson's id onto one another row already holds (memory V2 ADR, move 3). Keeps the first,
    unions evidence + seen tasks onto it (:func:`_absorb`), preserves order (deterministic). The
    subject-keyed pass handles same-subject dupes; this handles the remap collision regardless of
    subject drift. Rows without a ``memory_id`` (never expected for long-term) stay distinct."""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        key = str(row.get("memory_id") or f"\0{index}")
        if key in by_id:
            _absorb(by_id[key], row)
            continue
        by_id[key] = dict(row)
        order.append(key)
    return [by_id[key] for key in order]


def _collapse_entities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse entity cards sharing a ``canonical_name`` — created when an entity remap rewrites a
    moved card's key onto a path another card already holds (memory V2 ADR, move 3). Latest content
    wins (mirrors the ``_ingest_entity`` upsert) while ``last_seen_task_ids`` is unioned; first
    appearance fixes the order (deterministic). Cards without a ``canonical_name`` stay distinct."""
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        key = str(row.get("canonical_name") or f"\0{index}")
        seen = _str_list(row.get("last_seen_task_ids"))
        if key in by_key:
            seen = list(dict.fromkeys([*_str_list(by_key[key].get("last_seen_task_ids")), *seen]))
        else:
            order.append(key)
        merged = dict(row)  # latest content wins
        merged["last_seen_task_ids"] = seen
        by_key[key] = merged
    return [by_key[key] for key in order]
