"""PacketBuilder — deterministic, model-free per-node retrieval packets.

The read-path counterpart to the write funnel. Given a node and its task context, it selects and
ranks the stored records that belong in that node's packet (the **stage-1 deterministic filter**
only — an optional semantic rerank is deferred), renders them into a small
capped brief, and writes it to the per-task packet file. Defining invariants:

* **Model-free & pure.** No LLM call anywhere; selection reads only the store + the injected
  ``PacketContext``, so the same inputs yield the same ordered packet.
* **Precision over recall.** Hard per-node caps; over the line backstop it drops whole
  lowest-ranked records, never partial ones, so a packet always stays coherent.
* **Path, not root.** The caller is handed the per-node packet file path, never the memory root
  a node with no relevant memory gets **no file** — never a fabricated one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory._io import atomic_write_text
from wastech_orchestrator.memory.records import LongTermKind
from wastech_orchestrator.memory.service import MemoryService
from wastech_orchestrator.memory.trust import DURABLE_TRUST_LEVELS, TrustLevel

# Ranking weight per trust level (precision-first / trust-weighted). Higher = preferred. Keyed by
# the serialized value, since records carry ``trust_level`` as a string on disk.
_TRUST_RANK: dict[str, int] = {
    TrustLevel.HUMAN_CURATED.value: 5,
    TrustLevel.REVIEW_VERIFIED.value: 4,
    TrustLevel.REPO_OBSERVED.value: 3,
    TrustLevel.ARTIFACT_BACKED.value: 2,
    TrustLevel.AGENT_INFERRED.value: 1,
    TrustLevel.EXTERNAL_UNTRUSTED.value: 0,
}

# Serialized trust/kind values used to surface still-"held" durable lessons from quarantine: a
# durable repo-observed/reviewer lesson that has not yet recurred waits in quarantine, which the
# packet otherwise never reads — so it is write-only noise. Only DURABLE-trust, lesson-kind held
# records are surfaced (never agent-inferred / external-untrusted, which stay quarantined).
_DURABLE_TRUST_VALUES: frozenset[str] = frozenset(t.value for t in DURABLE_TRUST_LEVELS)
_LONG_TERM_KIND_VALUES: frozenset[str] = frozenset(k.value for k in LongTermKind)

# Nodes whose role naturally prefers reviewer-kind lessons ("review → more reviewer").
_REVIEWER_PREF_NODES: frozenset[str] = frozenset({"review", "fixing"})
# Nodes that lean on entity cards — they get a small entity-cap bump.
_ENTITY_HEAVY_NODES: frozenset[str] = frozenset({"implementation"})
_ENTITY_CAP_BUMP = 2

# Evidence whose ``ref`` rots and so is never rendered as a "see …" pointer:
# a task id / commit SHA is gone after a squash-merge, a ``.worc/logs/<task>`` dir is cleaned up.
_EPHEMERAL_EVIDENCE_TYPES: frozenset[str] = frozenset({"task", "commit", "diff"})
_COMMIT_SHA = re.compile(r"[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class PacketContext:
    """The deterministic inputs to one packet build — no clock, no randomness.

    ``node_id`` is the flow node about to run; ``touched_paths`` / ``touched_symbols`` are POSIX
    repo-relative and drive path-scoped retrieval (empty when nothing is touched yet, e.g. at
    planning before any edit). ``task_type`` is the flow dispatch key, carried as a retrieval signal
    (not a ranking input: the episodic tier it would rank is not injected).
    """

    node_id: str
    task_type: str | None = None
    touched_paths: tuple[str, ...] = ()
    touched_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectedPacket:
    """The records chosen for one packet (already filtered, ranked, and count-capped).

    Episodes are deliberately absent: the episodic tier is write-only and is never
    rendered into an agent's context.
    """

    long_term: tuple[dict[str, Any], ...] = ()
    entities: tuple[dict[str, Any], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.long_term or self.entities)


class PacketBuilder:
    """Build a per-node memory packet from the canonical store (deterministic, model-free).

    Reads through :class:`MemoryService`'s read API only; it never mutates the store and never calls
    a model. ``config`` supplies the per-node caps.
    """

    def __init__(self, service: MemoryService, config: MemoryConfig) -> None:
        self._service = service
        self._config = config

    # --- public API -----------------------------------------------------------

    def build(self, context: PacketContext) -> SelectedPacket:
        """Select + rank + count-cap the records for ``context`` (the stage-1 deterministic filter).

        Empty store / no matches → an empty :class:`SelectedPacket`. The line backstop is
        applied later, at render time, so this stays a pure selection step.
        """
        lt_cap, entity_cap = self._caps(context.node_id)
        long_term = self._select_long_term(context)[:lt_cap]
        entities = self._select_entities(context)[:entity_cap]
        return SelectedPacket(long_term=tuple(long_term), entities=tuple(entities))

    def render(self, packet: SelectedPacket) -> str:
        """Render ``packet`` to the brief markdown within the line backstop (whole-record drops)."""
        return _format(self._fit(packet))

    def write_packet(
        self,
        *,
        node_id: str,
        task_type: str | None,
        touched_paths: Sequence[str],
        dest: Path,
    ) -> Path | None:
        """Build + render the packet for one node and write it atomically to ``dest``.

        Returns ``dest`` when a packet was written, or ``None`` when there is no relevant memory —
        in which case **no file is created**, so ``{memory_path}`` renders empty. This is
        the seam the node-prompt builder calls; it constructs the :class:`PacketContext` itself so
        the caller need not depend on the memory record shapes.
        """
        context = PacketContext(
            node_id=node_id, task_type=task_type, touched_paths=tuple(touched_paths)
        )
        packet = self._fit(self.build(context))
        if packet.is_empty:
            return None
        atomic_write_text(dest, _format(packet))
        return dest

    # --- selection (stage-1 deterministic filter) ------------------------------

    def _select_long_term(self, ctx: PacketContext) -> list[dict[str, Any]]:
        rows = [row for row in self._all_long_term() if _is_active(row) and _node_ok(row, ctx)]
        # Also surface durable lessons still "held" in quarantine (awaiting recurrence) — the
        # packet is otherwise their only, never-taken read path. Durable trust only; the ranking and
        # per-node caps below bound them exactly like promoted long-term records.
        rows += [row for row in self._durable_quarantine() if _node_ok(row, ctx)]
        # Stable sorts, least-significant first → final order (most→least significant): trust,
        # path overlap, reviewer-preference (for the review/fixing nodes), recency, id.
        rows.sort(key=lambda r: str(_id_of(r)))
        rows.sort(key=lambda r: str(r.get("last_verified_at") or ""), reverse=True)
        rows.sort(key=lambda r: 0 if _reviewer_pref(r, ctx.node_id) else 1)
        rows.sort(key=lambda r: -_path_overlap(_scope_paths(r), ctx.touched_paths))
        rows.sort(key=lambda r: -_trust_rank(r))
        return rows

    def _select_entities(self, ctx: PacketContext) -> list[dict[str, Any]]:
        rows = [row for row in self._service.read_entities() if _is_active(row)]
        # Most→least significant: path overlap, trust, entity id (stable tiebreak). The cap alone
        # bounds entity-link flooding — one path scope cannot crowd out everything else.
        rows.sort(key=lambda r: str(r.get("entity_id") or ""))
        rows.sort(key=lambda r: -_trust_rank(r))
        rows.sort(key=lambda r: -_path_overlap(_as_str_list(r.get("paths")), ctx.touched_paths))
        return rows

    def _all_long_term(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in LongTermKind:
            rows.extend(self._service.read_long_term(kind))
        return rows

    def _durable_quarantine(self) -> list[dict[str, Any]]:
        """Durable, lesson-kind records still held in quarantine.

        A durable long-term lesson that has not yet recurred waits in ``quarantine/pending.jsonl``;
        the promotion gate keeps it there, but it is real repo knowledge, so the packet surfaces the
        DURABLE-trust, lesson-kind held records. Agent-inferred / external-untrusted quarantine
        stays invisible by design.
        """
        return [
            row
            for row in self._service.read_quarantine()
            if str(row.get("trust_level") or "") in _DURABLE_TRUST_VALUES
            and str(row.get("kind") or "") in _LONG_TERM_KIND_VALUES
        ]

    # --- caps + line backstop -------------------------------------------------

    def _caps(self, node_id: str) -> tuple[int, int]:
        entity = self._config.packet_max_entity
        if node_id in _ENTITY_HEAVY_NODES:
            entity += _ENTITY_CAP_BUMP
        return self._config.packet_max_long_term, entity

    def _fit(self, packet: SelectedPacket) -> SelectedPacket:
        """Drop whole lowest-ranked records until the brief is within the line backstop.

        Never truncates a record (that would strip its provenance) — it drops the lowest-value tier
        first (entity → lesson) and, within a tier, the lowest-ranked (last) record.
        """
        working = packet
        while not working.is_empty and len(_render_lines(working)) > self._config.packet_max_lines:
            working = _drop_lowest(working)
        return working


# --- module-level pure helpers ------------------------------------------------


def _is_active(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "active") == "active"


def _node_ok(row: dict[str, Any], ctx: PacketContext) -> bool:
    """A lesson applies when its scope names no nodes (repo-wide) or names this node."""
    nodes = _as_str_list(_scope(row).get("nodes"))
    return not nodes or ctx.node_id in nodes


def _reviewer_pref(row: dict[str, Any], node_id: str) -> bool:
    return node_id in _REVIEWER_PREF_NODES and str(row.get("kind") or "") == LongTermKind.REVIEWER


def _trust_rank(row: dict[str, Any]) -> int:
    return _TRUST_RANK.get(str(row.get("trust_level") or ""), 0)


def _scope(row: dict[str, Any]) -> dict[str, Any]:
    scope = row.get("scope")
    return scope if isinstance(scope, dict) else {}


def _scope_paths(row: dict[str, Any]) -> list[str]:
    return _as_str_list(_scope(row).get("paths"))


def _path_overlap(record_paths: Sequence[str], touched: Sequence[str]) -> int:
    """How many touched paths the record references — same path or one nested under the other.

    Bounded by ``len(touched)``; a directory scope (``src/core``) matches files beneath it and a
    file scope matches a touched directory above it. POSIX form throughout (records store POSIX).
    """
    if not record_paths or not touched:
        return 0
    count = 0
    for touched_path in touched:
        if any(_paths_related(touched_path, rp) for rp in record_paths):
            count += 1
    return count


def _paths_related(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _id_of(row: dict[str, Any]) -> Any:
    return row.get("memory_id") or row.get("entity_id") or row.get("id")


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _drop_lowest(packet: SelectedPacket) -> SelectedPacket:
    """Drop the single lowest-value record: an entity, else a long-term lesson."""
    if packet.entities:
        return replace(packet, entities=packet.entities[:-1])
    if packet.long_term:
        return replace(packet, long_term=packet.long_term[:-1])
    return packet


def _render_lines(packet: SelectedPacket) -> list[str]:
    """The brief as a list of lines — one bullet per record (progressive disclosure: a record links
    to its evidence rather than inlining it). One line per record keeps the bullet count bounded by
    the record caps, so the line backstop is the only size knob that needs enforcing."""
    lines: list[str] = ["# Repository memory (advisory — verify against the code)"]
    if packet.long_term:
        lines.append("")
        lines.append("## Lessons")
        lines.extend(_lesson_bullet(row) for row in packet.long_term)
    if packet.entities:
        lines.append("")
        lines.append("## Entities")
        lines.extend(_entity_bullet(row) for row in packet.entities)
    return lines


def _format(packet: SelectedPacket) -> str:
    return "\n".join(_render_lines(packet)) + "\n"


def _lesson_bullet(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "lesson")
    statement = _one_line(row.get("statement") or row.get("subject") or "")
    trust = str(row.get("trust_level") or "")
    bullet = f"- [{kind}] {statement} (trust: {trust})"
    remedy = _one_line(row.get("remedy") or "")
    if remedy and remedy != statement:
        bullet += f" — remedy: {remedy}"
    evidence = _first_durable_evidence_ref(row.get("evidence"))
    if evidence:
        bullet += f" — see {evidence}"
    return bullet


def _entity_bullet(row: dict[str, Any]) -> str:
    name = str(row.get("canonical_name") or row.get("entity_id") or "")
    entity_type = str(row.get("entity_type") or "entity")
    summary = _one_line(row.get("summary") or "")
    bullet = f"- [{entity_type}] {name}"
    if summary:
        bullet += f": {summary}"
    paths = _as_str_list(row.get("paths"))
    if paths:
        bullet += f" ({', '.join(paths)})"
    return bullet


def _first_durable_evidence_ref(evidence: Any) -> str | None:
    """The first evidence ref safe to render — a resolvable pointer (repo file, doc, named check).

    Rotting pointers are never shown as a "see …" link: a task
    id, a commit SHA (squash-merge deletes it), or a ``.worc/logs/<task>`` dir is a dead reference.
    Such refs stay in the store as internal provenance and still feed the deterministic trust
    classifier — the filter is render-time only.
    """
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if isinstance(item, dict) and _is_durable_evidence_ref(item):
            return _one_line(item.get("ref"))
    return None


def _is_durable_evidence_ref(item: dict[str, Any]) -> bool:
    if str(item.get("type") or "").strip().lower() in _EPHEMERAL_EVIDENCE_TYPES:
        return False
    ref = _one_line(item.get("ref"))
    if not ref:
        return False
    return ".worc/logs/" not in ref and _COMMIT_SHA.match(ref) is None


def _one_line(text: Any) -> str:
    """Collapse a value to a single trimmed line so one record is always exactly one bullet line."""
    return " ".join(str(text).split())
