# 05.6 — Docs sync

[phase](index.md) · [definition-of-done](../../definition-of-done.md) · [acceptance: all](../../acceptance-criteria.md)

**Goal:** bring the docs and follow-ups tracker in sync with the shipped subsystem and flip the task to implemented.

## Scope

In: `/sync-docs` across the functional map / configuration / CLI reference; flip the [task hub](../../index.md) status to implemented; record deferred work; pass the Stop docs-sync gate. Out: code.

## Approach

- Run `/sync-docs` and update: the `memory` config block (configuration docs), the `worc memory` verbs (CLI reference), and the write/read/cleanup flows (functional map / system-flows).
- Flip the [task hub](../../index.md) status from "build pending" to implemented.
- Record deferred work in [../../../follow_ups.md](../../../follow_ups.md): the V2/V3/V4 triggers (gated by AC-O4), the possible V1.x `worc memory add/edit`, and the tuning of the provisional Q1/Q5 integers against the eval baseline.

## Files

- `docs/functional/*`, configuration docs, CLI reference, [../../index.md](../../index.md), [../../../follow_ups.md](../../../follow_ups.md).

## Tests

- The Stop docs-sync gate passes; `/run-checks` is green.

## Done when

The [definition-of-done](../../definition-of-done.md) is satisfied, docs match the code, and the task hub is flipped to implemented.
