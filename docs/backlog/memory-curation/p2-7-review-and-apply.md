# P2.7 — review and apply the approved proposals (`worc memory review`) + the rejection ledger

Priority: **P2** Status: **proposed** Date: 2026-07-26 Source: [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §4.6.1, §4.8, §4.11 stage 2

## Problem

[P2.6](p2-6-curator-propose-only.md) produces proposals and deliberately cannot apply them. Without an apply path the curator is a document nobody acts on; with the wrong apply path it becomes the thing the campaign exists to prevent. Two mechanisms are needed: a review verb that turns an approved proposal into an audited mutation, and a rejection ledger — because a pass that re-proposes what the operator already declined will stop being read after the second time, and then the whole layer is dead weight regardless of its quality.

## Change

**`worc memory review`** — three interchangeable forms, so approval is never a chore that requires sitting at a terminal:

1. **Interactive:** one proposal at a time. `_confirm` / `_confirm_yes` (`cli.py:1742`, `1751`) already handle the `--non-interactive` / non-TTY contract; reuse them rather than adding another `input()` path (the single-stdin-reader rule).
2. **Batch by class:** `--accept-class stale-evidence --reject-class rewrite-statement`. A whole class is approved without reading each line.
3. **By file:** the operator edits `proposals.jsonl` (deletes the lines they do not want) and runs `worc memory review --from <file>`. Editing in a text editor _is_ the decision.

Approval may also arrive remotely: the transport-agnostic `Notifier` plus the HITL approval round-trip (`core/hitl.py`) and `auto_mode.confirm_next_task` already deliver operator confirmations over Telegram. Reuse that channel rather than inventing a second one.

**Application** goes through the existing audited seam, never a direct file write: snapshot → atomic tier rewrite → **one logical audit event** per transition (the P1.4 format: tier, file, added/updated/removed/moved ids, reason per id, record hashes, snapshot id), with a new `AuditActor` for the curator (`memory/audit.py:40` currently knows only `finalizer` / `cleanup` / `operator`). Rollback stays `worc memory restore`.

**Trust of an applied proposal.** Applying a curator proposal never raises trust by itself. `human-curated` is set **only** by explicit operator approval of that specific record; a proposal the operator accepted in batch by class counts as approval for that class. Nothing applied here enters `_AUTO_PROMOTE` implicitly.

**The rejection ledger.** Keyed on `proposal_hash` = content hash of (`op`, sorted `target_ids`, normalized proposed text). A rejected proposal is never proposed again; dedup is against **everything ever seen**, not against what was applied — otherwise every pass rebuilds the same rejected set and never converges. The ledger records the decision, the timestamp, and (optionally) a one-line operator reason, so "why is this not fixed" has an answer six months later.

**Refusals.** `review` refuses while a task is active, exactly like `compact` / `restore` (`cli.py:2424`, `2453`), and refuses to apply a proposal whose `target_ids` no longer match the store's current state — a stale proposal is re-derived by the next pass, never force-applied.

## Acceptance

- An approved proposal produces exactly one logical audit event with exact ids and per-id reasons, plus exactly one snapshot; `worc memory restore` returns the store to the prior state.
- A rejected proposal does not reappear in the next pass over an unchanged store.
- Applying nothing (all rejected) writes no snapshot and no event.
- A proposal targeting a record that changed since the pass is refused with a diagnosable message, not applied.
- `review` refuses while a task is active.
- No applied proposal results in a trust level above `artifact-backed` without explicit approval of that record.

## Test

Round-trip fixture: propose → approve one, reject one → assert one event, one snapshot, the rejected hash in the ledger; run the pass again and assert the rejected proposal is absent. Batch-class fixture asserts class filters select exactly the intended proposals. Stale-target fixture mutates the record between propose and review and asserts refusal. Restore fixture asserts byte-identical rollback. An `--from <file>` fixture with hand-deleted lines asserts only the remaining proposals apply.

## Scope / risk

This is the only place in the campaign where a model-derived change reaches the store, so its blast radius is the whole point of the design: one snapshot, one event, one verb, one rollback. Two risks. (a) **Batch approval is a footgun** — `--accept-class rewrite-statement` would approve the most dangerous class wholesale; either exclude the irreversible classes from batch mode or require an explicit second confirmation for them. (b) **Ledger poisoning** — a hash that is too coarse silently suppresses a legitimately different later proposal; normalize conservatively and make the ledger inspectable (and clearable) through the CLI.

## Depends on

[P2.6](p2-6-curator-propose-only.md) for the proposal contract, and [P1.4](p1-4-memory-audit-trail.md) for the event format that makes an applied edit explainable and reversible.
