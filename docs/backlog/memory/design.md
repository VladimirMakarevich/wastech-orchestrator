# Design

Status: **draft — to refine** Date: 2026-06-28 — [task hub](index.md)

The buildable detailed design for V1. This is the distillation that we iterate on; the full rationale, evidence, comparison matrices, and sources are in [research/memory-architecture-blueprint.md](research/memory-architecture-blueprint.md) (section references below point there).

## 1. Component model (narrow supervisor + deterministic services)

The single most important choice: the supervisor stays a narrow, trusted narrator; everything risky is deterministic, model-free, separately testable. (Blueprint §4.1, [supervisor-role-split.md](research/supervisor-role-split.md).)

| Component | Owns | LLM calls |
| --- | --- | --- |
| **Supervisor** | Observe steps; write summary; **optionally emit one structured `candidate_memory_delta` at finalize** | reuses finalize turn |
| **MemoryService** | Canonical store; redaction; validation; trust; merge/dedup; promotion; quarantine; audit; rollback | none |
| **PacketBuilder** | Stage-aware retrieval; precision-first top-k; ranking; token-capped brief; writes packet files | none |
| **CleanupJob** | TTL expiry; path/symbol existence checks; dup-merge candidates; stale marking; bounded budget | none |
| **DerivedIndex** | Repo map; symbol index; changed-path/entity lookup; later FTS/embeddings — all **rebuildable** | none |

The supervisor never assembles packets, writes the canonical store directly, holds promotion authority, resolves conflicts, owns cleanup, or enforces policy.

## 2. Data flow

**Write (once, at finalization):** task artifacts → `Supervisor.finalize()` emits `summary.md` + optional structured `candidate_memory_delta` → `MemoryService.apply_delta()`: redact → validate (resolve paths/symbols, reject missing evidence, label external-only) → assign trust → merge/dedup → promote (if rules pass) else quarantine → append audit row (+ hashes) → `.worc/memory/`.

**Read (per stage):** `PacketBuilder.build(stage, task_context)`: deterministic filter (stage, touched paths/symbols, task type, recency, trust, entity links) → precision-first top-k → [optional rerank, later] → token-capped brief → write `logs/<task-id>/memory/<stage>.md` → node prompt gets `memory_path` → agent reads the packet itself.

**Cleanup (between tasks):** in the `watch_loop` idle gap (single-slot invariant guarantees no active task) → `CleanupJob.run_once()`: snapshot → TTL expiry → stale-mark missing paths/symbols → propose duplicate merges → quarantine uncertain → audit. No network; never edits code/docs/skills; never creates long-term lessons.

## 3. Storage layout

Canonical store: **task-independent**, **gitignored** local state (not committed, never in a PR). Per-task packets are **ephemeral** under the task artifact dir. (Blueprint §4.3.)

```text
.worc/memory/                       # canonical, task-independent, gitignored
  README.md  manifest.json
  long_term/   index.md  conventions.md  semantic.jsonl  procedural.jsonl  reviewer.jsonl  failures.jsonl
  short_term/  recent.jsonl  runs/<task-id>/episode.json
  entities/    index.md  entities.jsonl  aliases.json
  audit/       log.jsonl  snapshots/<ts>/
  quarantine/  pending.jsonl  rejected.jsonl
  derived/     repo_map.json  symbol_index.sqlite(later)  retrieval.sqlite(later)  embeddings/(later)

logs/<task-id>/memory/<stage>.md    # ephemeral per-task packets (gitignored, rotated)
```

`audit/`, `quarantine/`, `snapshots/` exist from day one. `derived/` is a rebuildable cache, not memory truth (it may later relocate, e.g. `.worc/cache/`).

Hard constraints: do **not** put memory in `state.db`; do **not** route the canonical store through `task_artifact_dir` (that is per-task).

## 4. Tiers

| Tier | Stores | Aging | Primary readers |
| --- | --- | --- | --- |
| **Short-term episodic** | distilled per-run outcomes, touched paths/symbols, check/review results, candidate promotions | TTL 14–45d, aggressive prune **[refine]** | planning, implementation, fixing |
| **Long-term** | `semantic` (facts/commands/fragile areas), `procedural` (verified workflows), `reviewer` (recurring expectations), `failure` (signatures + remedy) | no TTL, periodic revalidation | planning, review, fixing |
| **Entity** | cards for file/module/context/dependency/owner: name, aliases, paths/symbols, summary, relationships, risk notes, commands, links, last-validated commit | validate on touch + sweep | implementation, review, fixing |

Store only what **repeats**, **stays true**, or **saves rediscovery**. Never store secrets, raw transcripts/sessions, full diffs/logs, doc prose already written elsewhere, low-confidence/unverified facts, external-only facts as durable truth, case-specific conclusions, or auto-executing procedural instructions. Full store/don't-store list: blueprint §5.2. Record schemas: blueprint §5.3.

## 5. Lifecycle decision rules

(Blueprint §6.5 — keep these strict, not permissive.)

| Decision | Rule (summary) |
| --- | --- |
| Promote to long-term | trust ∈ {repo-observed, human-curated, review-verified, validated artifact-backed}; has evidence; recurred ≥2 tasks OR marked stable OR explained a recurring failure OR annotates a stable hotspot; one short repo-specific sentence; no current contradiction. **[refine]** thresholds |
| Promote to entity | knowledge naturally attaches to a file/module/context/dependency/owner and improves path-scoped retrieval |
| Keep short-term only | task-specific, recent, possibly-superseded, or useful mainly for resume/debug |
| Drop as stale | episodic past TTL & never promoted; entity target gone & no remap; lesson contradicted twice; failure pattern obsolete; superseded by newer canonical entry |
| Merge duplicates | same normalized subject + overlapping entities + compatible evidence → keep oldest id, union evidence/entities, newest wording, log merge |
| Quarantine conflict | new evidence contradicts active memory but is only agent-inferred/weakly grounded → quarantine; never silently delete |

## 6. Retrieval (precision-first, two-stage)

1. **Deterministic filter** by stage, touched paths/symbols, bounded context, task type, recency, trust, entity relations.
2. **Optional semantic rerank** — later (V3), only over the filtered set, never primary.

Per-packet hard caps (tunable, deliberately small): ≤ ~120 lines, ≤ ~15 bullets, ≤ 3 long-term lessons, ≤ 5 entity records, ≤ 3 related episodic notes. Default = pass the path to the generated stage brief; never the raw memory root. Progressive disclosure via links to deeper evidence. (Blueprint §6.2–§6.3.)

## 7. Safety & trust

Redaction + secret scan before any disk write (reuse `redact_text` / `redact_mapping`). Deny-by-default: persist only allowlisted source classes (local task artifacts, review/check outputs, repo files/docs, operator/HITL inputs, deterministic analysis).

| Trust level | Meaning | Durable long-term? |
| --- | --- | --- |
| `repo-observed` | verifiable from current code/config | yes |
| `human-curated` | operator wrote/approved | yes (required for procedural) |
| `review-verified` | confirmed by a review/fixing outcome | yes |
| `artifact-backed` | derived from task artifacts/checks | yes, only if validator confirms |
| `agent-inferred` | LLM synthesis, unconfirmed | no — quarantine / short-term |
| `external-untrusted` | from web/MCP/user/API content | never auto — quarantine, human |

Audit: every mutation logs id, timestamp, actor, source artifact ids, affected ids, action, pre/post hashes, rationale; append-only + hash-chained; batch cleanup snapshots first; a `restore` makes bad writes cheap to undo. Bounded autonomy: max scan/edit per pass (promotions-per-pass default 0), wall-clock budget, fail-closed, no active-task writes. Enforcement is deterministic (policies/hooks), never memory prose. (Blueprint §7.)

## 8. Config

`MemoryConfig` dataclass in `config/schema.py`, wired into `OrchestratorConfig` with a safe default, parsed in `config/loader.py`, documented in `packaged/config.example.yaml`. A global enable/disable flag plus tier caps / TTLs / cleanup budget. Bump `CONFIG_SCHEMA_VERSION` (currently **23**). Keep new **fatal** checks to a minimum — fatal only when there is no safe runtime fallback.

## 9. Seams in the current codebase

(Verified 2026-06-28. Full list: blueprint §8.)

- Redaction: `redact_text` / `redact_mapping` in [../../../src/wastech_orchestrator/providers/redaction.py](../../../src/wastech_orchestrator/providers/redaction.py).
- Atomic writes: `_atomic_json` in [../../../src/wastech_orchestrator/core/hitl.py](../../../src/wastech_orchestrator/core/hitl.py).
- Prompt path-variable: add `memory_path` to `ALLOWED_PROMPT_VARS` in [../../../src/wastech_orchestrator/core/prompts.py](../../../src/wastech_orchestrator/core/prompts.py); populate in node prompt-variable builders; reference `{memory_path}` in packaged role prompts.
- Supervisor: extend `finalize()` in [../../../src/wastech_orchestrator/core/supervisor.py](../../../src/wastech_orchestrator/core/supervisor.py) to return `candidate_memory_delta`; reuse the durable `__supervisor__` lineage.
- Idle hook: `watch_loop` in [../../../src/wastech_orchestrator/cli.py](../../../src/wastech_orchestrator/cli.py).
- Config: [../../../src/wastech_orchestrator/config/schema.py](../../../src/wastech_orchestrator/config/schema.py), [../../../src/wastech_orchestrator/config/loader.py](../../../src/wastech_orchestrator/config/loader.py).
- New modules (deterministic, unit-testable, no fake-CLI needed): `MemoryService`, `PacketBuilder`, `CleanupJob`, `DerivedIndex`.

## Open design points

Tracked in [questions.md](questions.md): autodream cadence/budget, codebase-reconciliation source of truth, promotion thresholds, CLI verbs & scheduling, `memory_path` naming & caps, audit home (dedicated log vs `evaluations` row vs both), where resume/debug-grade episodic detail lives.
