# 02.4 — MemoryService.apply_delta

[phase](index.md) · [design §2,§5,§7](../../design.md) · [acceptance: AC-W2/W4, AC-SF2/SF5](../../acceptance-criteria.md)

**Goal:** the single deterministic funnel both write seams feed: redact → validate → assign trust → merge/dedup → promote or quarantine → audit.

## Scope

In: the `apply_delta` pipeline implementing the design §5 lifecycle rules and §7 trust model, consuming typed records (02.1) from both the success (02.2) and failure (02.3) seams. Out: tier-file persistence specifics (02.5), audit-row mechanics (reuse 01.4), retrieval (phase 03).

## Approach

Pipeline order (design §2), each step deterministic and model-free:

- **Redact first** — `redact_text` / `redact_mapping` (`src/wastech_orchestrator/providers/redaction.py`) before any disk touch (C1/NFR5). No record bypasses redaction.
- **Validate** — resolve referenced paths/symbols via `DerivedIndex` (minimal existence check; may stub until phase 04.4 lands, then tighten); **reject missing evidence**; label external-only candidates (AC-W2/W4).
- **Assign trust** — deterministically, from the §7 table (`repo-observed` / `human-curated` / `review-verified` / `artifact-backed` / `agent-inferred` / `external-untrusted`). `trust_hint` is advisory input, never the final value (no self-certification, AC-SF5).
- **Merge / dedup** — design §5: same normalized subject + overlapping entities + compatible evidence → keep oldest id, union evidence/entities, take newest wording, log the merge.
- **Promote or quarantine** — promote to long-term only when Q3 thresholds pass: trust ∈ {repo-observed, human-curated, review-verified, validated artifact-backed} **and** (recurrence ≥ 2 tasks within 60d **or** trust ∈ {human-curated, review-verified} **or** explained a recurring failure **or** annotates a stable hotspot) **and** no current contradiction. `external-untrusted` / `agent-inferred` **never** auto-promote → quarantine (AC-SF2/AC-W4). Conflicts with active memory that are only weakly grounded → quarantine, **never** silent delete.
- **Audit** — every path writes an audit row via the 01.4 primitive (AC-SF3).

Thresholds (`2` / `60d`) are `MemoryConfig` defaults, tunable (Q3).

## Files

- `src/wastech_orchestrator/.../memory/service.py` (`apply_delta`).

## Tests

- Missing/invalid evidence → quarantine, never promote (AC-W2).
- `external-untrusted` / `agent-inferred` → quarantine, never durable long-term (AC-SF2/AC-W4).
- Promotion thresholds enforced exactly (Q3); a record one short of recurrence stays short-term.
- Merge keeps oldest id and unions evidence/entities.
- Every branch writes exactly one audit row (AC-SF3); trust assigned on every record (AC-SF5).

## Done when

`apply_delta` implements §5/§7 deterministically and model-free; AC-W2/W4 and AC-SF2/SF5 hold.
