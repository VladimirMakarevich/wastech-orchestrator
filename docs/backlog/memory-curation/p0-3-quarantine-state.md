# P0.3 — stale quarantine is served back to agents as ordinary memory

Priority: **P0** Status: **proposed** Date: 2026-07-26 Source: [memory audit](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-24-wastimeapp-memory-audit.md) P0-3 · [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §4.2

## Problem

One file, `quarantine/pending.jsonl`, holds at least two incompatible states: "durable and waiting to recur" (a promotion waiting room) and "stale / unresolvable / low trust" (a safety tombstone). The reason a record is there exists only in the audit rationale, never on the record. The packet builder then surfaces **any** durable-trust lesson kind from that file, without a status, a quarantine reason, or a verification time — so a record that cleanup deliberately isolated comes back to the agent looking like active knowledge.

Cleanup therefore creates a false sense of isolation, and the store's freshness signal cannot be trusted.

## Evidence

[`memory/packet.py:177`](../../../src/wastech_orchestrator/memory/packet.py):

```python
def _durable_quarantine(self) -> list[dict[str, Any]]:
    return [
        row
        for row in self._service.read_quarantine()
        if str(row.get("trust_level") or "") in _DURABLE_TRUST_VALUES
        and str(row.get("kind") or "") in _LONG_TERM_KIND_VALUES
    ]
```

The filter is trust + kind only. Nothing reads _why_ the row is in quarantine, and `_format` renders it as a normal bullet.

Eight quarantined lessons currently pass this filter in the WastimeApp store; **seven** of them have scopes the same cleanup semantics already judged invalid: `blog/**`, the renamed `08_live_calculator…`, directory/glob scopes `mobile/wastime-journey-book/**` and `*.md`. Ids: `ltm_280e9e58e76a`, `ltm_4e21ffa95730`, `ltm_e9589a90583a`, `ltm_01dd4034f9d5`, `ltm_adcaf397a514`, `ltm_b7aaf397e5e7`, `ltm_229d498ea8c4`.

Audit scenario 6 confirms the round trip: a context naming the old `08_live_calculator…` path returns the quarantined `ltm_01dd4034f9d5` as an ordinary lesson. The current test suite pins the behavior as desired — [`test_durable_held_quarantine_lesson_is_surfaced`](../../../tests/memory/test_packet.py) (`tests/memory/test_packet.py:122`) — without distinguishing the reason.

The write path adds the same ambiguity from its side: `_quarantine_reason` (`memory/service.py`) composes a human-readable cause into the **audit rationale**, not onto the record.

## Change

1. Add typed state to the record: `quarantine_reason` (`awaiting_recurrence` | `stale_path` | `conflict` | `unsafe` | `invalid_evidence`), `quarantined_at`, `retrieval_eligible`, `source_state`.
2. Only `awaiting_recurrence` may be advisory-eligible, and only under an explicit config policy; `stale_path` / `conflict` / `unsafe` / `invalid_evidence` are always excluded from retrieval.
3. Until the migration lands, **fail closed**: do not read cleanup-quarantined records into a packet at all.
4. When a pending record is legitimately included, render its status and verification time in the packet so the agent can see it is not settled knowledge.
5. Safer still, and preferred if the change is being made anyway: split the store physically — `pending_promotion.jsonl` separate from `rejected/stale.jsonl`, with different read policies, so "rejected" is unreachable through the public packet API by construction.

## Acceptance

- None of the seven listed stale ids appears in any packet, under any node or context.
- A valid `awaiting_recurrence` record is included only when config allows it, and is visually marked.
- Cleanup's state transition is covered end to end: active → stale quarantine → not retrievable.
- `stale-memory-in-packet`: 7 known → 0.

## Test

Extend `test_durable_held_quarantine_lesson_is_surfaced` into a matrix over `quarantine_reason`: only `awaiting_recurrence` can surface, and only with the policy on. A regression fixture pinned to the seven real ids asserts absence from every scenario in the audit's retrieval set. A cleanup transition test asserts a record moved for a vanished path is not retrieval-eligible afterwards.

## Scope / risk

Smallest of the three P0 items and the only one whose failure direction is safe: excluding a record from a packet cannot corrupt anything, it can only lose advisory context. That makes it the right first step. The one real risk is over-exclusion starving packets of legitimately pending durable lessons — which is why the `awaiting_recurrence` policy exists rather than a blanket ban, and why P1.5's report should show how many records the policy is holding back.

## Depends on

Nothing. Ship first: it is the only item that removes proven-stale claims from live prompts immediately.
