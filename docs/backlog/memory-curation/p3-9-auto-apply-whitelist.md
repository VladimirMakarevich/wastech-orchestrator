# P3.9 — promote specific proposal classes to auto-apply, once precision is measured

Priority: **P3** Status: **proposed (blocked on a measurement)** Date: 2026-07-26 Source: [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §4.5, §4.10, §4.11 stage 3

## Problem

The human in the loop ([P2.7](p2-7-review-and-apply.md)) exists for one reason: `proposal_precision` is unknown. It is not a permanent design principle — it is the price of not having the number. Once two or three real passes have produced a measured precision per operation class, the classes that clear the bar should stop asking, because an operator who must approve a mechanical demotion every week will eventually approve without reading, which is worse than automation.

This item is deliberately last and deliberately blocked: without the measurement it has no criterion, and shipping it early would re-create exactly the risk the campaign was built to avoid.

## Change

1. **Measure first.** Over at least three passes (mixed: two broad [P2.6](p2-6-curator-propose-only.md) sweeps and one [P2.8](p2-8-post-task-verification.md) cadence window), record per operation class: proposals made, accepted, rejected, and — where observable — proposals accepted and later reverted.
2. **Define the bar per class, not globally.** Reversibility matters as much as precision: a wrong `mark_stale` costs an advisory bullet, a wrong `collapse_duplicates` costs a record's wording forever. Suggested shape (numbers to be set from the data, not now): a class may auto-apply only if it is reversible **and** its precision is high across the measured passes **and** it has zero observed false-quarantine.
3. **Never auto-apply the irreversible classes.** `rewrite_statement`, `split_claim`, `collapse_duplicates`, `delete` stay operator-gated permanently — the campaign's authority table is not a stepping stone for them.
4. **Config, not code.** A `memory.curation.auto_apply: [<class>, …]` allowlist, defaulting to empty, so an operator can widen or revoke it without a release. An unknown class name in the list is ignored with a warning (fail-closed), never treated as a wildcard.
5. **Keep the paper trail identical.** An auto-applied proposal produces the same single logical audit event, snapshot, and rollback path as an approved one, with the actor distinguishing it. The rejection ledger still applies: an auto-appliable class does not bypass a previously rejected hash.
6. **Kill switch.** A single config flip (or `--no-auto-apply`) returns the whole layer to propose-only, and the report says which classes are currently auto-applying so the operator is never surprised by a change they did not see.

## Acceptance

- The measurement exists and is written down per class, with the sample size, before any class is enabled.
- With an empty allowlist (the default) behavior is byte-identical to P2.7.
- An enabled class auto-applies with the same audit/snapshot/rollback guarantees as a manual approval.
- An irreversible class cannot be enabled — the config validator rejects it, rather than silently ignoring it.
- Flipping the kill switch restores propose-only behavior with no store change.
- Every auto-applied change is attributable to the curator actor and reversible by `worc memory restore`.

## Test

A config fixture enabling a reversible class asserts auto-application with a correct event; the same fixture with the class absent asserts a proposal instead. A validator fixture asserts an irreversible class in the allowlist is a config error. A previously-rejected hash in an enabled class asserts suppression. A kill-switch fixture asserts propose-only behavior and zero store mutation.

## Scope / risk

Small in code, large in consequence: this is the item that hands a model a (narrow) write channel. Two guards make it acceptable — the classes are reversible by construction, and the audit/snapshot path is the same one an operator approval uses, so nothing becomes unreviewable, only unblocked. The real risk is a precision number computed from too few passes; treat a small sample as no sample. Also watch the drift argument: a class that measures well on a 71-record store may behave differently at 700, so the measurement is not a one-off — re-check it when the store's order of magnitude changes.

## Depends on

[P2.7](p2-7-review-and-apply.md) and [P2.8](p2-8-post-task-verification.md), plus a **measured** `proposal_precision` from real passes. Blocked until that number exists.
