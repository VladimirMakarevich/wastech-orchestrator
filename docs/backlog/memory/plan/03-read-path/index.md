# Phase 03 — Read path

Status: **outline** — [plan](../index.md) · [design §2,§6](../../design.md) · [acceptance: AC-R1..R4](../../acceptance-criteria.md)

**Goal:** each stage receives a small, deterministic, capped memory packet by path. Depends on phase 01 (store) for the read API; phase 02 for content to read.

**Exit criteria:** AC-R1..R4 and AC-R2 caps pass; reproducible packets.

## Tasks (split into files at scope-lock)

- **`PacketBuilder`** — deterministic stage-aware retrieval: filter by stage + touched paths/symbols + task type + recency + trust + entity links; precision-first top-k; ranking. No LLM.
- **Packet rendering & caps** — shape the per-stage brief (planning / implementation / review / fixing) within the hard caps (design §6); write `logs/<task-id>/memory/<stage>.md` atomically.
- **`memory_path` prompt variable (node-driven)** — add to `ALLOWED_PROMPT_VARS` in `core/prompts.py`; build + populate a per-node packet for **any** node whose role prompt references `{memory_path}` (no hardcoded node set); it points at the per-node packet, never the memory root.
- **Role-prompt references (default set, editable)** — reference `{memory_path}` in the packaged `planning` / `implementation` / `review` / `fixing` role prompts (seeded into `.worc/flows/roles/` at install); any other node can opt in by adding the variable, with no Core change.
- **Empty-state behavior** — no relevant memory → minimal/empty packet, never fabricated (AC-R4).

## Notes

Two-stage retrieval, but stage 2 (semantic rerank) is **not** built here — it is V3, gated by replay results. V1 ships the deterministic filter only. Reproducibility (same inputs → same packet) is a test target (AC-R3).
