# Phase 03 — Read path

Status: **planned** — [plan](../index.md) · [design §2,§6](../../design.md) · [acceptance: AC-R1..R4](../../acceptance-criteria.md)

**Goal:** each stage receives a small, deterministic, capped memory packet by path. Depends on phase 01 (store) for the read API; phase 02 for content to read.

**Exit criteria:** AC-R1..R4 and AC-R2 caps pass; reproducible packets.

## Tasks

| # | Task | Touches |
| --- | --- | --- |
| 1 | [`PacketBuilder`](01-packet-builder.md) | new `memory/packet.py` — deterministic stage-1 filter + ranking |
| 2 | [Packet rendering & caps](02-packet-rendering-and-caps.md) | brief shaping, per-node caps, atomic packet write |
| 3 | [`memory_path` prompt variable](03-memory-path-prompt-variable.md) | `core/prompts.py` `ALLOWED_PROMPT_VARS`, node-driven build/inject |
| 4 | [Role-prompt references](04-role-prompt-references.md) | packaged `planning`/`implementation`/`review`/`fixing` role prompts |
| 5 | [Empty-state behavior](05-empty-state-behavior.md) | minimal/empty packet guarantee (AC-R4) |

## Notes

- Two-stage retrieval, but stage 2 (semantic rerank) is **not** built here — it is V3, gated by replay results. V1 ships the deterministic filter only. Reproducibility (same inputs → same packet) is a test target (AC-R3).
- **Seam reality:** `ALLOWED_PROMPT_VARS` is a `frozenset`; `None → ""` is already handled, and the conditional `{?memory_path}...{/memory_path}` block form already exists in `core/prompts.py` — task 03.4 wraps the reference in that block so empty memory disappears cleanly (AC-R4).
- The per-task packet file lives under the gitignored `.worc/` home, so it needs no new ignore rule.
