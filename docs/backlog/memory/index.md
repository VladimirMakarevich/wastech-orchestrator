# Memory subsystem — task hub

Status: **V1 implemented — merged to `main`** (all five phases + audit remediation; PR #14 / commit `e7aa47a`) Date: 2026-07-01 Owner: Vladimir Makarevich

This is the root for the **persistent, repo-scoped memory subsystem** task — the place to start and the index of everything. The goal: stop losing expensive, repo-specific lessons between independent runs, without turning into an opaque, rotting, or attackable context dump.

One-line shape: a **files-first, supervisor-distilled, deterministically-managed, evidence-backed** memory layer under `.worc/memory/`, with three tiers, written once at finalization, read as small per-stage packets, and curated by bounded background jobs. Full rationale: [research/memory-architecture-blueprint.md](research/memory-architecture-blueprint.md).

**Two selectable memory sources.** This subsystem is the orchestrator's **own** audited store (the "orchestrator-memory" source). The other source is the coding agent's **own native memory** — for Claude, its built-in auto-memory under `~/.claude/projects/<repo>/memory/`, which the orchestrator confines by default (F37) but an operator may opt into via `agents.providers.claude.allow_native_memory` ([../archive/done/agent-native-memory-opt-in.md](../archive/done/agent-native-memory-opt-in.md)). The two are independent knobs: disable this store (`memory.enabled: false`) and use native memory, run both, or neither. Unlike this store, native memory is unaudited and outside the redaction net — the accepted cost of that opt-in.

## Documents

| File | Purpose |
| --- | --- |
| [problem.md](problem.md) | The original problem and why it matters now. |
| [requirements.md](requirements.md) | Functional / non-functional requirements + hard constraints (firm vs to-refine). |
| [design.md](design.md) | The buildable detailed design (components, data flow, layout, tiers, lifecycle, safety). |
| [adr.md](adr.md) | **The accepted V1 architecture decision (ADR-0001)** — the capstone decision record. |
| [acceptance-criteria.md](acceptance-criteria.md) | Testable criteria per area. |
| [out-of-scope.md](out-of-scope.md) | What V1 excludes and what is rejected outright, with rationale. |
| [definition-of-done.md](definition-of-done.md) | The V1 merge gate. |
| [implementation-audit.md](implementation-audit.md) | **Post-implementation review** — conformance summary + the open findings (F1–F6) the merge gate does not catch. |
| [questions.md](questions.md) | Open decisions — the working list we lock incrementally. |
| [happy-path.md](happy-path.md) | Plain-language Before/After scenarios + diagrams of what the operator gets. |
| [research/](research/index.md) | The two deep-research reports, the role-split note, and the consolidated blueprint. |
| [plan/](plan/index.md) | The phased implementation plan (one folder per phase, tasks inside). |

## How we work on this task

Spec-driven, in order, iterating as we go:

1. **Understand** — [problem.md](problem.md) (stable).
2. **Agree the what** — [requirements.md](requirements.md) + [out-of-scope.md](out-of-scope.md). Anything contentious → [questions.md](questions.md).
3. **Agree the how** — [design.md](design.md), grounded in the [blueprint](research/memory-architecture-blueprint.md).
4. **Agree "done"** — [acceptance-criteria.md](acceptance-criteria.md) + [definition-of-done.md](definition-of-done.md).
5. **Sequence the work** — [plan/](plan/index.md), derived from the design once it is locked.
6. **Build & verify** — phase by phase, each gated by its acceptance criteria and `/run-checks`.

Documents carry only what is **decided and buildable**; everything still open lives in [questions.md](questions.md) with a status, and moves to "Decided" with its decision (not deleted). This task supersedes the exploratory [../archive/done/orchestrator-memory.md](../archive/done/orchestrator-memory.md) and is indexed from the [backlog README](../README.md).

## Current status & next steps

- ✅ Research consolidated into the [blueprint](research/memory-architecture-blueprint.md); task structure scaffolded (this hub + spec docs + plan skeleton).
- ✅ [requirements.md](requirements.md) **locked for V1** (FR1–8, NFR1–9, C1–5) and **all [questions.md](questions.md) resolved (Q1–Q10)**; concrete contracts/defaults captured in [design.md](design.md) §10.
- ✅ [ADR-0001](adr.md) **accepted** — the V1 architecture is ratified.
- ✅ [plan/](plan/index.md) **fully detailed** — all five phases broken into committed task files (Goal / Scope / Approach / Files / Tests / Done-when each), grounded in the verified code seams (design §9). Three actualization deltas folded in: `.worc/` is already gitignored wholesale (no new ignore rule — 01.1); `supervisor.finalize()` is today a free-text turn that 02.2 converts to structured output on the same turn; the `evaluations` table already exists as the home for the Q6 marker (02.6).
- ✅ **Phase 01 (Foundations) implemented** on `feat/memory-subsystem` — config v24 + the `memory` block, the canonical store layout, the deterministic `MemoryService` skeleton (records, trust levels, redacted atomic writes, read API), and the append-only hash-chained audit log + snapshots/restore. Suite green; the knobs are parsed but not yet consumed.
- ✅ **Phase 02 (Write path) implemented** — the candidate-delta contract + tolerant parser, the deterministic `MemoryService.apply_delta` funnel (validate → trust → merge/dedup → promote-or-quarantine → audit), tier persistence + quarantine, the best-effort `evaluations` marker, and both write seams wired (the supervisor emits the delta on its existing finalize turn — zero extra LLM calls; terminal failures write a deterministic short-term record). Suite green.
- ✅ **Phase 03 (Read path) implemented** — the deterministic, model-free `PacketBuilder` (stage-1 filter + precision-first ranking + per-node caps + whole-record line backstop + atomic packet write, empty → no file), `{memory_path}` allowlisted and **node-driven** (built only for a node whose role prompt references it, injected as the per-node packet path under `logs/<task-id>/memory/<node>.md`, never the store root), and the packaged `planning` / `implementation` / `review` / `fixing` role prompts referencing it inside a `{?memory_path}…{/memory_path}` block. AC-R1..R4 hold; suite green.
- ✅ **Phase 04 (Curation) implemented** — the minimal `DerivedIndex` (path/symbol existence + rename-remap, rebuildable cache), the bounded model-free `CleanupJob` (snapshot-first; TTL-expire / remap-or-quarantine-stale / merge-duplicates within the Q1 budget; never promotes, never edits code), the `worc memory show/validate/compact/restore` CLI (read-only vs mutating, `--dry-run`, active-task gate), and the rate-limited idle-gap cleanup hook in `watch_loop`. AC-C1..C4 + AC-SF4 groundwork hold; suite green.
- ✅ **Phase 05 (Safety & evaluation) implemented** — the four adversarial drills (redaction → 0 leaks across tiers/audit/quarantine/packets; poisoning → low-trust quarantined, never durable, never out-ranks trusted, never silently overwrites; staleness → removed/renamed targets remapped-or-quarantined, never deleted; rollback → `restore` returns byte-identical pre-state + an audit row), plus the deterministic, model-free offline-replay harness (`tests/eval/harness.py`: the metric stack + the AC-O1..O4 verdicts + the measured-lift roadmap gate) and a recorded — currently **synthetic** (greenfield: no real task corpus yet) — [eval baseline](research/eval-baseline.md). Suite green.
- ✅ **V1 is implemented and merged to `main`** (PR #14 / commit `e7aa47a`; the `feat/memory-subsystem` branch is now redundant). All five phases landed; the [definition-of-done](definition-of-done.md) holds. Next: record a **real** eval baseline once production runs accrue and tune the provisional Q1/Q3/Q5 integers against it; the V2 (SQLite/FTS) / V3 (embeddings) / V4 (graph) phases stay gated behind a measured lift (AC-O4). Deferred V1-scope-cut tightening is tracked in [../follow_ups.md](../follow_ups.md).
- 📋 **Post-implementation audit recorded** ([implementation-audit.md](implementation-audit.md), 2026-07-01), then **remediated** the same day. **F1** wired `DerivedIndex` write-time entity-path validation into `apply_delta` (NFR2); **F2** added lesson existence reconciliation to `CleanupJob` (contradiction-detection stays an explicit V2 item); **F3** seeded runtime redaction with the known config/env secret literals (C1); **F4** made `restore` prune snapshot-absent tier files; **F6** annotated the minor nits. **F5** is partial — AC-S3 and an AC-S4 disabled-vs-baseline run were backfilled; the Windows path round-trip (AC-X) and AC-SF5 ride the standing Windows-CI follow-up. Suite green, `ruff`/`mypy` clean.
- ✅ **V2 durable-concepts refinement implemented** (2026-07-11, [ADR](../archive/done/memory-concepts-over-episodic-ledger.md)) — sharpens _what_ memory injects, not the storage engine (SQLite/embeddings/graph stay gated on AC-O4). The episodic tier is now a **write-only shell** (never rendered into a packet; the written episode drops the log-dir/terminal-status pointers, keeps only `touched_paths`); the rendered packet carries **durable lessons + entity cards only** and filters rotting evidence refs (commit SHAs / task ids / log dirs). The durable tier is **un-starved** — `repo-observed` joins `human-curated`/`review-verified` in promoting on first sight (only `artifact-backed` still waits for recurrence) — and made **refactor-robust**: `CleanupJob` now remaps a moved entity card **and** a moved lesson in place (rewriting the path-anchored key so a re-proposal merges), instead of quarantining. The path anchor (F30/F44) is deliberately **kept** — per-run wording drift is far more frequent than file moves, so path-keying is the more reliable identity (see the ADR's open-question 5).
