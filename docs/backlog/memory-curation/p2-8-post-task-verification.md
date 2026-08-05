# P2.8 — verify what a task just wrote, while its evidence still exists

Priority: **P2** Status: **proposed** Date: 2026-07-26 Source: [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §4.7 — the campaign's main substitution for the original idea

## Problem

The original `auto-dream` proposal was periodic: every 20th orchestrator run or 50th task, sweep the whole store. But the store is not decaying from volume — it is 71 records after 21 tasks, and its damage was inflicted **at the moment of writing**. And the later a claim is examined, the less there is to examine it with:

- the run's artifacts under `.worc/logs/<task-id>/` are cleaned up (see the `logs clean` / retention items);
- evidence anchored to `task` / `commit` / `diff` is already treated as rotting by the read path (`_EPHEMERAL_EVIDENCE_TYPES`, `memory/packet.py`), and the finalize prompt explicitly asks the model not to anchor on a SHA or task id;
- the task's diff and touched paths are no longer cheaply recoverable.

So a periodic sweep is structurally the weakest possible moment to verify a claim. Verifying 1–5 fresh records right after the task that wrote them is cheaper, more accurate, and hits the cause instead of the symptom.

## Change

After a task that wrote a memory delta finishes, in the **idle gap** (never inside the task — `has_active_task` still guards it, `cli.py:1673`), run a narrow curator pass scoped to the records that task created or changed.

**Input:** those 1–5 records, their evidence refs, the task's diff and touched paths, its artifacts, and the failure/publish outputs if any.

**One verifiable question per record:** does the cited evidence support this statement, and does the statement contradict an existing active record?

**Output** — the same proposal contract as [P2.6](p2-6-curator-propose-only.md), with a narrow verdict set: `supported` / `unsupported` / `contradicts:<id>` / `not-atomic`, plus the corresponding proposal. `supported` may carry a deterministic proof (resolved path + content hash) that satisfies the [P0.2](p0-2-evidence-validation-and-trust.md) validator, letting an `artifact-backed` record legitimately reach durable trust — which is the one place where this item _adds_ knowledge rather than just guarding it.

**Which records to consider:** the ids reported by `apply_delta`'s `ApplyResult` (promoted / merged / quarantined counts already exist — extend it to carry ids), or the ids in the audit events of that task. No new bookkeeping tier.

**Failure seam.** A terminal failure writes only an episode today (`WriteSource.FAILURE`, `memory/service.py:81`), and three real failures are absent from failure memory. This item does **not** fix that seam — that is a separate write-path task (see the campaign README's Open item 1) — but it is the natural consumer of it: once a terminal failure is a typed record, this pass is what verifies its signature and remedy while the `publish-error.txt` still exists.

## Acceptance

- After a delta-writing task, the pass examines exactly the records that task touched — not the whole store.
- A record whose evidence does not support its statement is flagged and loses durable eligibility (through P0.2's ceiling, not by a direct write).
- A record contradicting an active one produces a conflict proposal naming both ids.
- A task that wrote no delta triggers no pass at all.
- The pass never runs while a task is active, and never blocks the next task pickup.
- Cost per pass is bounded and measured: single-digit thousands of input tokens for a typical 1–5 record delta.

## Test

A fixture task writes three records — one supported, one with a non-resolving ref, one contradicting an active record — and the pass produces exactly three verdicts with the right ops. A zero-delta task fixture asserts no pass. An active-task fixture asserts refusal. A "stale artifacts" fixture (task logs already cleaned) asserts graceful degradation: fewer verdicts, no crash, and an explicit note that the evidence base was incomplete.

## Scope / risk

The highest-value item of the curation half, and it must not turn into a gate: verification is advisory, runs after the task is closed and published, and can never reopen, re-route or fail a task (the supervisor's advisory-by-construction rule, `core/supervisor.py:1-21`). Watch the cadence: one pass per delta-writing task is frequent, so keep the input narrow and the reasoning tier modest, or it becomes the supervisor-cadence problem again in a new place (see `../token-optimization/`). Also resist scope drift into "and re-verify neighbours" — a broad sweep is [P2.6](p2-6-curator-propose-only.md)'s job, triggered by a health signal.

## Depends on

[P2.6](p2-6-curator-propose-only.md) for the curator module, route identity and proposal contract; [P2.7](p2-7-review-and-apply.md) for the apply path. Benefits from — and partially overlaps — the "terminal failure as a typed event" item, which is why the README asks for that decision before this one starts.
