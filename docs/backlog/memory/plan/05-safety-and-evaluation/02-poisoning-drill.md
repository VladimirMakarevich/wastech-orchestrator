# 05.2 — Poisoning drill

[phase](index.md) · [design §6,§7](../../design.md) · [acceptance: AC-SF2/SF5](../../acceptance-criteria.md)

**Goal:** prove that `external-untrusted` / `agent-inferred` candidates never auto-promote and never outrank trusted, repo-backed memory.

## Scope

In: an adversarial test feeding low-trust candidates and asserting quarantine + no durable long-term + no ranking dominance. Out: the trust logic (02.4), retrieval (03.1).

## Approach

- Craft deltas with `external-untrusted` / `agent-inferred` trust → assert quarantine, never durable long-term (AC-SF2/SF5).
- Assert `PacketBuilder` never ranks a low-trust record above a trusted repo-backed one (design §6/§7).
- Include a **contradiction** case: a weakly-grounded candidate that contradicts active memory → quarantine, never a silent overwrite (NFR2).

## Files

- `tests/.../test_memory_poisoning_drill.py`.

## Tests

- Low-trust candidates never auto-promote (AC-SF2); trust is enforced at promotion (AC-SF5).
- Low-trust never outranks trusted in a packet.
- A contradicting low-trust candidate is quarantined, not applied.

## Done when

AC-SF2 and AC-SF5 hold under the drill.
