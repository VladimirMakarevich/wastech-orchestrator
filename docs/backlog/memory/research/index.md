# Research & raw materials — memory subsystem

This folder holds the inputs that the task is built on: two independent deep-research efforts, one design note, and the consolidated synthesis that distills them. These are **source material** — frozen references, not the living spec. The living, iterable documents are the task files one level up (start at [../index.md](../index.md)).

## Consolidated synthesis (read this first)

- [memory-architecture-blueprint.md](memory-architecture-blueprint.md) — the consolidated, evidence-backed architecture blueprint that merges both reports + the role-split note. It is the single richest reference for **why** the design is shaped the way it is (landscape, comparison matrices, rejected alternatives, sources). The task's [../design.md](../design.md) is the buildable distillation of it; this blueprint is the rationale behind that.

## Recorded results

- [eval-baseline.md](eval-baseline.md) — the offline replay harness's recorded baseline (the AC-O1..O4 gate). Currently **synthetic** (greenfield: no real task corpus yet); the approach + thresholds are locked, and the integers are replaced by a real baseline once production runs accrue.

## Raw research

- [worc-report/](worc-report/worc-deeep-research-memory-report.md) — internal deep research (13 parts): executive summary, landscape map, storage/retrieval/update comparison matrices, recommended architecture, tiers, lifecycle, safety, concrete proposals, evolution path, evaluation plan, final recommendation, sources, bottom line.
- [3rd-party-report/](3rd-party-report/00-3rd-party-deep-research-memory-report.md) — external deep research (6 parts): executive summary, landscape map, comparison of architectures, recommended architecture, lifecycle/write/read/safety, evaluation/evolution/final recommendation.
- [supervisor-role-split.md](supervisor-role-split.md) — design note: why the supervisor should stay narrow and the heavy lifting should move to deterministic services (`MemoryService` / `PacketBuilder` / `CleanupJob` / `DerivedIndex`), judged on efficiency, simplicity, reliability, and token cost.

## How the research was used

The two reports were produced independently and converged on the same answer — that convergence is the backbone of the design. The blueprint captures the convergent signals, the empirical grounding (AGENTbench, ContextBench, memory-poisoning work, STATE-Bench, KGCompass/Prometheus, AWM, MemCoder), and the source list. The task documents (problem / requirements / design / acceptance-criteria / plan) carry forward only what is decided and buildable; everything left to decide is tracked in [../questions.md](../questions.md).
