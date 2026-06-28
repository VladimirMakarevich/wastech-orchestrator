# Memory subsystem — task hub

Status: **proposed (in design)** Date: 2026-06-28 Owner: Vladimir Makarevich

This is the root for the **persistent, repo-scoped memory subsystem** task — the place to start and the index of everything. The goal: stop losing expensive, repo-specific lessons between independent runs, without turning into an opaque, rotting, or attackable context dump.

One-line shape: a **files-first, supervisor-distilled, deterministically-managed, evidence-backed** memory layer under `.worc/memory/`, with three tiers, written once at finalization, read as small per-stage packets, and curated by bounded background jobs. Full rationale: [research/memory-architecture-blueprint.md](research/memory-architecture-blueprint.md).

## Documents

| File | Purpose |
| --- | --- |
| [problem.md](problem.md) | The original problem and why it matters now. |
| [requirements.md](requirements.md) | Functional / non-functional requirements + hard constraints (firm vs to-refine). |
| [design.md](design.md) | The buildable detailed design (components, data flow, layout, tiers, lifecycle, safety). |
| [acceptance-criteria.md](acceptance-criteria.md) | Testable criteria per area. |
| [out-of-scope.md](out-of-scope.md) | What V1 excludes and what is rejected outright, with rationale. |
| [definition-of-done.md](definition-of-done.md) | The V1 merge gate. |
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

Documents carry only what is **decided and buildable**; everything still open lives in [questions.md](questions.md) with a status, and moves to "Decided" with its decision (not deleted). This task supersedes the exploratory [../orchestrator-memory.md](../orchestrator-memory.md) and is indexed from the [backlog README](../README.md).

## Current status & next steps

- ✅ Research consolidated into the [blueprint](research/memory-architecture-blueprint.md); task structure scaffolded (this hub + spec docs + plan skeleton).
- ✅ [requirements.md](requirements.md) walked through and **locked for V1** (FR1–8, NFR1–9, C1–5); decided Q4 / Q6 / Q8, narrowed Q1 / Q5. Only tunable default integers remain.
- 🔜 Finalize [design.md](design.md) against the locked requirements; resolve the remaining [questions.md](questions.md) (Q2, Q3, Q7, Q9, Q10); then break [plan/](plan/index.md) phase 01 into committed task files and (optionally) raise an ADR for V1.
