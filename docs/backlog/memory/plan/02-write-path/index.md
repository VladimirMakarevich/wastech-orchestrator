# Phase 02 — Write path

Status: **outline** — [plan](../index.md) · [design §2,§5](../../design.md) · [acceptance: AC-W1..W4](../../acceptance-criteria.md)

**Goal:** memory gets written once per task at finalization, deterministically and safely, with zero new LLM calls. Depends on phase 01.

**Exit criteria:** AC-W1..W4 and AC-SF2/SF5 (trust enforcement, no low-trust auto-promotion) pass.

## Tasks (split into files at scope-lock)

- **Candidate-delta contract** — define the structured `candidate_memory_delta` schema (lessons / failures / entities, each with trust hint + evidence pointers). See [questions.md](../../questions.md) Q9.
- **Supervisor emit** — extend `finalize()` to return the delta from the existing summary turn; stays best-effort (malformed delta → skip, never block publish); reuse the `__supervisor__` durable lineage.
- **`MemoryService.apply_delta`** — redact → validate (resolve paths/symbols, reject missing evidence, label external-only) → assign trust → merge/dedup → promote (rules in design §5) or quarantine → audit.
- **Tier persistence** — append episodic; update entity cards; promote to long-term only when rules pass.
- **Two write seams** — success (publish) → supervisor candidate delta via the existing `_engine_finalize` turn → full write (long-term eligible). Terminal failure / manual (`_fail` / `_go_terminal` in `process_one`, **no** supervisor turn) → **deterministic** short-term/failure record (no LLM), never long-term. External-context tasks → quarantine-unless-validated.
- **Audit marker** — record the write in `audit/log.jsonl` and (per Q6) an `evaluations` marker row.

## Notes

All of `apply_delta` is deterministic and unit-tested without a model. The only LLM touch is the supervisor delta, which reuses the existing finalize turn (assert zero extra calls — AC-W1).
