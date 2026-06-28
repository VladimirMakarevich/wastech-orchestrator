# 03.2 — Packet rendering & caps

[phase](index.md) · [design §6](../../design.md) · [acceptance: AC-R2/R3](../../acceptance-criteria.md)

**Goal:** render the selected records into a small per-node brief within the hard caps and write it atomically to the per-task packet file.

## Scope

In: shape the brief (planning / implementation / review / fixing weighting per Q5); enforce per-node caps; write `logs/<task-id>/memory/<stage>.md` atomically. Out: record selection (03.1), variable injection (03.3).

## Approach

- **Caps** (Q5 / NFR4, `MemoryConfig`, tunable): baseline ≤ 3 long-term / ≤ 5 entity / ≤ 3 episodic, with a ~120-line / ~15-bullet backstop. `implementation` weighted toward more entity cards, `review` toward more reviewer lessons.
- **Over cap → drop whole lowest-ranked records** (never partial, NFR4) so packets stay coherent.
- **Progressive disclosure**: link to deeper evidence rather than inlining it (design §6).
- Write under the per-task artifact dir (the packet file lives under the gitignored `.worc/` home, so it never needs its own ignore rule); **atomic write** (01.3 primitive), **UTF-8 + explicit `\n`** (NFR8).

## Files

- `src/wastech_orchestrator/.../memory/packet.py` (renderer + packet-path helper).

## Tests

- Caps enforced; over-cap drops whole records, never partial (AC-R2/NFR4).
- Packet is written atomically (no partial file on interrupt).
- Same inputs → byte-identical packet (AC-R3).

## Done when

Per-node briefs render within the caps and write atomically; AC-R2 holds.
