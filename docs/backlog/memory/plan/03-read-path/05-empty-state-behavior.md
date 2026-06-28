# 03.5 — Empty-state behavior

[phase](index.md) · [design §6](../../design.md) · [acceptance: AC-R4](../../acceptance-criteria.md)

**Goal:** a node with no relevant memory gets a minimal/empty packet — never a fabricated one.

## Scope

In: define and test the empty/minimal-packet behavior threaded through `PacketBuilder` (03.1), the renderer (03.2), and the variable (03.3). Out: no new module — this is a guarantee, verified here.

## Approach

- No relevant records → either write **no** packet (so `{memory_path}` renders empty and the conditional block drops) or a minimal "no relevant memory" stub; **never** invent content (AC-R4).
- Consistent with the Q10 disabled-state (empty string) and FR4 (minimal/empty, never fabricated). Behavior is deterministic.

## Files

- `src/wastech_orchestrator/.../memory/packet.py` (empty path); a dedicated test module.

## Tests

- Empty store / no matches → empty-or-minimal packet with no fabricated lines (AC-R4).
- The empty result is deterministic and the prompt is unaffected.

## Done when

AC-R4 holds; the empty state is explicit, minimal, and tested.
