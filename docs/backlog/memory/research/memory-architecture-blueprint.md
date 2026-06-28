# Memory subsystem — consolidated architecture blueprint

Status: **proposed (consolidated design)** Date: 2026-06-28 Owner: Vladimir Makarevich

This is the single authoritative design document for a persistent, repo-scoped memory subsystem in `wastech-orchestrator`. It consolidates and supersedes the exploratory [orchestrator-memory.md](../../orchestrator-memory.md) stake-in-the-ground by folding in the findings of two independent deep-research efforts and one design note, and it is the document the implementation plan and the ADR(s) should be built from.

It is a **blueprint**, not a build spec: it fixes the shape, the invariants, the lifecycle, the safety model, the roadmap, and the evaluation gates. The exact module names, function signatures, and config keys are settled at ADR/implementation time.

Sources synthesized here:

- Internal deep research — [worc-report/](./worc-report/worc-deeep-research-memory-report.md) (13 parts).
- External deep research — [3rd-party-report/](./3rd-party-report/00-3rd-party-deep-research-memory-report.md) (6 parts).
- Supervisor role-split note — [supervisor-role-split.md](./supervisor-role-split.md).
- Predecessor exploratory direction — [orchestrator-memory.md](../../orchestrator-memory.md).

This document deliberately optimizes for **what is best, highest-quality, most reliable, and most efficient**, not for the smallest diff against the current codebase. Where it refines or corrects the predecessor draft, that is called out explicitly in [§ 12. How this refines the predecessor draft](#12-how-this-refines-the-predecessor-draft).

---

## 1. The decision in one paragraph

Build a **files-first, supervisor-distilled, deterministically-managed, evidence-backed, repo-scoped memory layer** under `.worc/memory/`, split into three tiers (short-term episodic, long-term semantic/procedural/reviewer/failure, entity cards), kept **separate from derived repo indices** (repo map / symbol index / code search), written **once per task at finalization** as a structured byproduct of the supervisor's existing summary turn, read by agents only through **small, stage-specific, precision-first packet files passed by path**, and curated by **bounded, deterministic, audited background jobs** — never a freeform autonomous "dreamer". Memory is **advisory context, never a source of truth and never an enforcement channel**: the code, the task artifacts, and the checks remain the truth; enforcement stays in orchestrator policies and hooks. Vector search and a knowledge graph are explicit later stages, unlocked only by measured failure of the simpler design — not adopted on day one.

---

## 2. Why this is the right shape (the strong convergent signals)

Both research efforts, run independently, converged on the same answer. That convergence — plus the empirical evidence behind it — is the backbone of this design. The signals, strongest first:

1. **Files-first, not vector-first or graph-first.** Plain `md` / `json` / `jsonl` under a local directory wins on inspectability, auditability, git-friendliness, provider-neutrality, low ops burden, and a natural fit for this project's path-based prompt model. SQLite, embeddings, and graphs are migration targets, not starting points. (Both reports rank "plain files" and "files + structured index" highest for our scope.)

2. **Memory is not the same plane as a repo index.** A repo map / symbol index / code search answers _"what is in the code right now"_ and is a **rebuildable cache** of the current tree. Memory answers _"what did we learn across runs that is worth not rediscovering"_. Mixing them creates staleness and confusion. Keep them in separate planes with separate lifecycles. This is one of the clearest shared recommendations and the largest conceptual upgrade over the predecessor draft.

3. **Small and precise beats large and complete.** The empirical evidence is blunt: developer-written context files gave only an ~4% average task-success lift, LLM-generated context files were slightly _negative_ (about −3%), and either way context files raised exploration/testing/reasoning cost by 20%+ (AGENTbench / "Evaluating AGENTS.md"). Coding agents systematically **over-retrieve** and frontier models bias toward recall over precision (ContextBench). Therefore: precision-first retrieval, hard caps, progressive disclosure — not maximal accumulation.

4. **Write in the background, at task finalization — not in the hot path.** Heavy extraction/dedup/promotion belongs _after_ implementation/testing/review/fixing, when artifacts, checks, review findings, and the supervisor summary already exist. Hot-path writes add latency and entangle memory management with the agent's main job. Every memory framework surveyed makes this hot-path-vs-background distinction and recommends background for substantial updates.

5. **The supervisor distills; deterministic services do the rest.** The supervisor is the one layer that sees the whole task, so it is the right place to emit a _candidate memory delta_ alongside its summary — but it must **not** become a second control plane. Validation, trust assignment, promotion, dedup, conflict handling, packet building, and cleanup are deterministic, unit-testable, model-free logic. This keeps the supervisor cheap (one structured output, zero extra LLM calls) and makes the risky parts inspectable. (This is the central recommendation of the [role-split note](./supervisor-role-split.md).)

6. **Read by path, as a curated per-stage packet — never the whole store.** Agents receive a small stage-specific brief file (planning / implementation / review / fixing), not the memory root. This matches the project's existing "pass artifact paths, not inline blobs" model and the production "progressive disclosure" pattern (Claude Code `MEMORY.md` + on-demand topic files; Codex skills loaded by name/description until relevant).

7. **Memory is advisory, never source-of-truth, never enforcement.** Long-term memory stores reusable repository knowledge (conventions, fragile areas, commands, recurring review/failure patterns), not task conclusions as "truth". Reviewed artifacts and code stay authoritative. Enforcement lives in deterministic policies/hooks; memory only shapes prompts. Procedural memory that could change execution behavior must be human-promoted, or it becomes a stealth control plane.

8. **Safety is day-one, not bolted on.** Persistent memory is a documented attack surface (memory-poisoning research: poisoning is persistent, existing prompt-injection defenses are incomplete, and write/retrieve aggressiveness increases attack success). So: redaction + secret-scan before any write; deny-by-default allowlisted storage; trust levels and provenance enforced through the write→retrieve→act pipeline; append-only hash-chained audit log; pre-cleanup snapshots; quarantine; cheap rollback; and strictly bounded autonomy.

9. **Provider-neutral canonical format, curated views to providers.** Store canonical memory in our own neutral format and hand providers only curated packets. Provider-native memory (Claude auto-memory, Codex durable guidance) is machine-local / instruction-layer, not a portable system-of-record — never adopt it as the canonical layer.

10. **Evolve on measured need, not on ambition.** Each step up the storage ladder (SQLite → embeddings → graph) is unlocked by a specific, measured failure of the simpler design, gated by the evaluation plan. "No vector/graph infra without a measurable recall or quality lift" is itself a success criterion.

---

## 3. Non-negotiable design principles

These bound every downstream decision. They are the distilled invariants of this blueprint; they extend (never override) the repo's hard invariants in [.agents/rules/](../../../../.agents/rules/), [AGENTS.md](../../../../AGENTS.md), and [CLAUDE.md](../../../../CLAUDE.md).

- **P1 — Memory is advisory.** It shapes prompts; it never routes, gates, enforces, or acts. The Core still decides; the supervisor stays advisory.
- **P2 — Code + artifacts + checks are the source of truth.** Memory is distilled, provenanced knowledge _about_ them, never a replacement.
- **P3 — Memory ≠ derived index.** Cross-run learned knowledge and rebuildable current-state caches are separate planes with separate lifecycles.
- **P4 — Deterministic where it is risky.** Validation, trust, promotion, dedup, conflict, retrieval ranking, and cleanup are model-free, inspectable, unit-testable code. The LLM only _proposes_ a candidate delta.
- **P5 — Bounded everywhere.** Read size, write frequency, per-pass cleanup budget, retention windows, and autonomy are all capped and configurable.
- **P6 — Evidence or it does not persist durably.** No durable long-term entry without a local evidence pointer and a passing validation against the current repo.
- **P7 — Redact before disk, always.** Memory is artifacts-class storage; the no-secrets invariant applies in full, including because agents read memory back into prompts that can land in committed artifacts.
- **P8 — Auditable and reversible.** Every mutation is logged; every batch cleanup is snapshotted; bad writes are cheap to undo.
- **P9 — Provider-neutral.** Canonical store is our format; providers get curated views only.
- **P10 — Disable-able.** The whole subsystem can be turned off by config; when off, behavior is exactly today's.

---

## 4. Target architecture

### 4.1 Component model (narrow supervisor + deterministic services)

The single most important architectural choice is the **role split**. The supervisor stays a narrow, trusted narrator; everything risky is deterministic, model-free, separately testable.

| Component | Owns | Model calls | Why it is separate |
| --- | --- | --- | --- |
| **Supervisor** | Observe each step; write whole-task summary; **optionally emit one structured `candidate_memory_delta` at finalize** | Reuses its finalize turn (0 extra) | Keeps it a trusted narrator, not a hidden control plane; cheapest token profile. |
| **MemoryService** | Canonical store under `.worc/memory/`; redaction; validation; trust assignment; merge/dedup; promotion; quarantine; audit; rollback | None (deterministic) | The critical reliability/safety seam — must be unit-testable without a model. |
| **PacketBuilder** | Stage-aware retrieval; precision-first top-k selection; ranking; token-capped brief shaping; writes per-task packet files | None (deterministic) | Packets must be reproducible and measurable on precision/recall; no per-stage LLM. |
| **CleanupJob** | TTL expiry; path/symbol existence checks; safe command revalidation; duplicate-merge candidates; stale marking; bounded per-pass budget | None (deterministic) | Cleanup must be boring and safe; freeform LLM cleanup is too risky and too costly. |
| **DerivedIndex** | Repo map; symbol index; changed-path/entity lookup; later FTS; later embeddings — all **rebuildable** | None | Current-tree structure is a cache, not durable memory truth (P3). |

The supervisor never assembles packets, never writes the canonical store directly, never holds promotion authority, never resolves conflicts, never owns cleanup, never enforces policy. Those are deterministic services.

### 4.2 Data flow

**Write (once, at task finalization):**

```
task artifacts (plan, diff/stat, checks, review findings, summary, HITL outcomes)
        │
        ▼
Supervisor.finalize()  ──► summary.md  +  optional candidate_memory_delta (structured)
        │
        ▼
MemoryService.apply_delta():
   redact ─► validate (resolve paths/symbols, reject missing evidence, label external-only)
          ─► assign trust ─► merge/dedup ─► promote (only if rules pass) | quarantine
          ─► append audit row (+ pre/post hashes)
        │
        ▼
.worc/memory/   (canonical, task-independent, gitignored)
```

**Read (per stage, deterministic):**

```
PacketBuilder.build(stage, task_context):
   deterministic filter (stage, touched paths/symbols, task type, recency, trust, entity links)
   ─► precision-first top-k  ─► [optional semantic rerank, later]
   ─► token-capped brief
   ─► write logs/<task-id>/memory/<stage>.md
        │
        ▼
node prompt gets memory_path  ──►  agent reads the packet file itself
```

**Cleanup (between tasks, bounded):**

```
watch_loop idle gap (no active task; single-slot invariant guarantees quiescence)
   ─► CleanupJob.run_once(): snapshot ─► TTL expiry ─► stale-mark missing paths/symbols
      ─► propose duplicate merges ─► quarantine uncertain ─► audit
   (no network; never edits code/AGENTS.md/docs/skills; never creates new long-term lessons)
```

### 4.3 Recommended `.worc/memory/` layout

Canonical store, **task-independent** and **gitignored** (it is local orchestrator state, not committed, never part of a PR — keeping the leak surface small and avoiding PR noise). Names are indicative.

```text
.worc/memory/                       # canonical, task-independent, gitignored
  README.md                         # human orientation + the invariants in §3
  manifest.json                     # schema version, tier caps, counters
  long_term/
    index.md                        # compact human index (MEMORY.md-style)
    conventions.md                  # small curated human-readable, low-churn
    semantic.jsonl                  # stable repo facts, commands, fragile areas
    procedural.jsonl                # verified workflows / "how X is done here"
    reviewer.jsonl                  # recurring review expectations/checklists
    failures.jsonl                  # recurring failure signatures + canonical remedy
  short_term/
    recent.jsonl                    # append-only episodic deltas, TTL-pruned
    runs/<task-id>/episode.json     # per-run distilled episode (not transcript)
  entities/
    index.md
    entities.jsonl                  # entity cards (split per-file later if needed)
    aliases.json
  audit/
    log.jsonl                       # append-only, hash-chained mutation log
    snapshots/<ts>/                 # pre-cleanup rollback anchors
  quarantine/
    pending.jsonl                   # candidates awaiting validation/human review
    rejected.jsonl
  derived/                          # REBUILDABLE caches — NOT memory truth (P3)
    repo_map.json
    symbol_index.sqlite             # later
    retrieval.sqlite                # later (FTS)
    embeddings/                     # later, optional
```

Per-task **packets are ephemeral**, written under the task's artifact dir (`logs/<task-id>/memory/<stage>.md`), gitignored and rotated like other per-task logs. This reconciles the predecessor draft's correction (the canonical _root_ must be task-independent) with the research recommendation (the per-stage _brief_ is naturally per-task and disposable).

`audit/`, `quarantine/`, and `snapshots/` exist **from day one** — they are what make bad writes reversible and reviewable.

> Note on `derived/`: it lives under `.worc/memory/` only for locality; conceptually it is a separate plane and may be relocated (e.g. `.worc/cache/`) without affecting the memory design. It must never be treated as durable memory truth.

---

## 5. Memory tiers

### 5.1 Overview

| Tier | Stores | Never stores | Aging | Primary readers |
| --- | --- | --- | --- | --- |
| **Short-term episodic** | distilled per-run outcomes, touched paths/symbols, check/review results, fixed/unfixed findings, candidate promotions, artifact pointers | raw transcripts, full diffs/logs, secrets, external facts as durable truth | TTL 14–45 days, aggressive prune | planning, implementation, fixing |
| **Long-term (4 kinds)** | `semantic` (stable facts/commands/fragile areas), `procedural` (verified workflows), `reviewer` (recurring review expectations), `failure` (recurring signatures + canonical remedy) | one-off task trivia, unstable branch facts, doc prose already written elsewhere | no TTL, periodic revalidation | planning, review, fixing |
| **Entity** | cards for file/module/context/dependency/owner: canonical name, aliases, paths/symbols, short summary, relationships, risk notes, commands, links to lessons & evidence, last-validated commit | freeform giant summaries, unverifiable claims, high-churn repo-map snapshots | validate on touch + periodic sweep | implementation, review, fixing |

### 5.2 The store/don't-store boundary

Store only what **repeats**, **stays true**, or **saves rediscovery**:

- build / test / lint / migration / verification commands;
- stable conventions and local rules not already captured in `AGENTS.md`/docs;
- architecture facts unlikely to change week to week;
- fragile areas and integration gotchas ("changing X usually requires Y and Z");
- recurring reviewer expectations;
- recurring failure modes and their canonical remedy;
- important entities and hotspots;
- links to prior tasks that are strong precedents.

Never store:

- secrets, tokens, raw credentials, full environment captures, raw session ids;
- raw provider sessions or reasoning traces as memory;
- full diffs and large logs;
- repo-wide prose restatement of docs that already exist;
- low-confidence facts without evidence;
- facts learned only from external web/MCP unless separately code-validated;
- case-specific conclusions as long-term truth;
- provider-native hidden memory as the canonical source;
- agent-generated procedural instructions that would execute automatically (these stay advisory until a human promotes them into `AGENTS.md` / a role prompt / a repo skill).

### 5.3 Example schemas (indicative)

Short-term episode:

```json
{
  "id": "ep_2026-06-28_task-1234_01",
  "task_id": "task-1234",
  "created_at": "2026-06-28T18:00:00Z",
  "task_type": "bugfix|feature|refactor|review-fix",
  "base_commit": "abc1234",
  "head_commit": "def5678",
  "touched_paths": ["src/wastech_orchestrator/config/schema.py"],
  "touched_symbols": ["MemoryConfig"],
  "stage_outcomes": {
    "planning": "done",
    "testing": "done_with_failures",
    "review": "done_with_findings",
    "fixing": "done"
  },
  "review_findings": [
    {
      "category": "missing-regression-test",
      "severity": "medium",
      "paths": ["..."]
    }
  ],
  "candidate_promotions": ["cand_01"],
  "artifact_paths": [
    ".worc/logs/task-1234/plan.md",
    ".worc/logs/task-1234/review.md"
  ],
  "trust": "artifact-backed",
  "expires_at": "2026-08-12T00:00:00Z"
}
```

Long-term record:

```json
{
  "memory_id": "ltm_000142",
  "kind": "semantic|procedural|reviewer|failure",
  "subject": "config-schema-changes",
  "statement": "Any config schema change must update docs and packaged config examples in the same change.",
  "rationale": "The docs-sync gate and config versioning otherwise break operator workflows.",
  "scope": {
    "paths": ["src/wastech_orchestrator/config/"],
    "symbols": ["OrchestratorConfig"],
    "stages": ["implementation", "review"]
  },
  "evidence": [
    { "type": "repo_doc", "ref": "CLAUDE.md" },
    { "type": "task", "ref": "task-1177" }
  ],
  "trust_level": "human-curated",
  "confidence": "high",
  "first_seen_commit": "abc1234",
  "last_verified_commit": "abc1234",
  "last_verified_at": "2026-06-28T18:00:00Z",
  "usage_count": 0,
  "supersedes": [],
  "status": "active",
  "expiry_policy": { "mode": "revalidate_on_touch" }
}
```

Entity card:

```json
{
  "entity_id": "module:supervisor-memory",
  "entity_type": "file|module|context|dependency|owner",
  "canonical_name": "core/supervisor.py",
  "aliases": ["Supervisor", "__supervisor__ lineage"],
  "paths": ["src/wastech_orchestrator/core/supervisor.py"],
  "symbols": ["Supervisor", "finalize"],
  "summary": "Persistent oversight layer; emits the candidate memory delta at finalize.",
  "relationships": [
    { "type": "writes", "target": "artifact:summary.md" },
    { "type": "depends_on", "target": "module:memory-service" }
  ],
  "risk_notes": [
    "finalize is best-effort; a memory write must never block publish"
  ],
  "hotspot_score": 0.81,
  "memory_refs": ["ltm_000142"],
  "last_seen_task_ids": ["task-1177"],
  "last_validated_commit": "abc1234",
  "status": "active"
}
```

---

## 6. Lifecycle rules

### 6.1 Write path (task finalization)

1. Inputs: task file, plan, final diff/stat, check results, review findings, summary artifact, HITL outcomes.
2. Supervisor emits a **structured** candidate delta (candidate lessons / failures / entities, each with a trust flag and evidence pointers) — never a prose blob, never a transcript.
3. MemoryService: **redact** → **validate** (resolve referenced repo paths/symbols; reject missing evidence; label external-only) → **assign trust** → **merge/dedup** → **promote** to long-term/entity only if rules pass, else **quarantine** → **audit**.
4. Outcome modulates the write: successful close → full write path; failed/manual task → short-term failure memory yes, long-term promotion rarely; tasks with heavy web/MCP/external context → default **quarantine unless code-validated** (mirrors Codex's "disable generated memory under external context").

### 6.2 Read path (per stage)

Each stage gets a narrowly-scoped packet, richest where it pays off:

- **Planning** — top repo conventions for the task area, related prior tasks, architectural hotspots/owners, 3–7 high-value long-term lessons, candidate entities. Bias to long-term + related episodic, not a full entity dump.
- **Implementation** — entity cards for likely-touched files/modules, known coupling/dependency gotchas, prior failure patterns in the area, reviewer lessons tied to those entities. Richest entity content, still capped. Memory biases _where to look_; the live code stays the truth.
- **Review** — recurring review checklist for touched areas, prior findings on the same entities, dependency/security pitfalls, "looks-ok-but-breaks-tests/docs" patterns. Highest marginal value; most useful when specific and prescriptive.
- **Fixing** — same-signature failures seen before, same test/module changed before, canonical remedies, relevant prior review comments. Filtered by finding category + path + stage.

### 6.3 Retrieval: two-stage, precision-first

1. **Deterministic filter** by stage, touched paths/symbols, bounded context, task type, recency, trust level, entity relations.
2. **Optional semantic rerank** — later, and only as a reranker over the filtered set, never as the primary retrieval.

Hard caps for any packet (tunable; deliberately small): ≤ ~120 lines, ≤ ~15 bullets, ≤ 3 long-term lessons, ≤ 5 entity records, ≤ 3 related episodic notes. The packet links to deeper evidence files for an agent that wants to inspect — progressive disclosure, never an inline dump. Default is "pass the path to the generated stage brief"; never "pass the path to the raw memory root".

### 6.4 Cleanup / bounded reconciliation ("autodream", de-mystified)

Runs in the `watch_loop` idle gap, where the single-slot invariant guarantees no active task. It is a **bounded reconciliation job, not freeform dreaming**. It may: validate up to N entries, compact duplicates, expire old non-promoted episodic records, mark stale entries, refresh `last_validated_*` for touched entities, move uncertain cases to quarantine, regenerate small indices, and emit a cleanup audit row. It must: snapshot before a batch, stay within a wall-clock/edit budget, run with no network, and never delay the next task pickup. It must **not**: create new long-term lessons from nothing; edit `AGENTS.md`/docs/skills/code; or write while a task is active.

### 6.5 Promotion / eviction / merge / conflict

| Decision | Rule |
| --- | --- |
| **Promote to long-term** | trust ∈ {repo-observed, human-curated, review-verified, validated artifact-backed}; evidence exists; AND (seen in ≥2 tasks within ~60d OR reviewer/operator marked stable OR it explained/prevented a recurring failure OR it annotates a stable hotspot); statement is one short repo-specific sentence; no contradiction in current repo/docs. |
| **Promote to entity** | knowledge naturally attaches to a file/module/context/dependency/owner and improves future path-scoped retrieval. |
| **Keep short-term only** | task-specific, recent, possibly-superseded, or useful mainly for resume/debug. |
| **Drop as stale** | episodic past TTL and never promoted; entity target removed and no alias/remap; lesson contradicted by current code/docs twice in a row; failure pattern obsolete after N releases/tasks; superseded by a newer canonical entry. |
| **Merge duplicates** | same normalized subject + overlapping entities + compatible (non-contradictory) evidence. Policy: keep oldest stable id, union evidence/entities, prefer newest validated wording, append aliases/sources, log the merge. |
| **Quarantine conflict** | new evidence contradicts active memory but is only agent-inferred/weakly grounded → quarantine; if old proven false → old gets `superseded_by`, new promoted; if unresolved → old marked `disputed`, new stays quarantined; all steps audited. Never silently delete a conflicting fact. |

Strictness over permissiveness is deliberate: the empirical evidence against bloated context and over-retrieval is the reason to keep these gates tight.

---

## 7. Safety & trust model

### 7.1 Redaction & secret handling

Nothing is written into `.worc/memory/` before redaction + a secret scan passes; any suspicious candidate is redacted or rejected. Memory is artifacts-class: no raw secrets, no full environment capture, no raw session ids — in memory, logs, SQLite, or artifacts. This holds _even though memory is gitignored_, because agents read memory back into prompts whose outputs land in committed artifacts.

### 7.2 Deny-by-default storage

Persist only allowlisted source classes and fields: local task artifacts, review/check outputs, repo files/docs, operator/HITL inputs, deterministic repo analysis. Never durable-promote: arbitrary web-search facts, MCP connector output without local validation, or raw agent self-claims without evidence.

### 7.3 Trust levels & provenance (enforced through write→retrieve→act)

| Trust level | Meaning | Durable long-term? |
| --- | --- | --- |
| `repo-observed` | directly verifiable from current code/config | yes |
| `human-curated` | operator wrote/approved | yes (required for procedural) |
| `review-verified` | confirmed by a review/fixing outcome | yes |
| `artifact-backed` | derived from task artifacts/checks | yes, only if validator confirms |
| `agent-inferred` | LLM synthesis, not independently confirmed | no — quarantine / short-term |
| `external-untrusted` | from web/MCP/user/API content | never auto — quarantine, human only |

Low-trust memory must never silently behave like high-trust repo fact. Procedural memory that could change execution requires `human-curated`.

### 7.4 Audit, snapshots, quarantine, rollback (day-one)

Every mutation logs: id, timestamp, actor (`finalizer`/`cleanup`/`operator`), source artifact ids, affected memory ids, action (`append`/`promote`/`merge`/`quarantine`/`prune`/`rollback`), pre/post content hashes, rationale. The log is append-only and hash-chained. Batch cleanup snapshots first. A simple restore command makes any bad write cheap to undo. This is worth more than sophisticated ML scoring.

### 7.5 Bounded autonomy & containment

Autonomous cleanup has hard limits: max entries scanned/pass, max promotions/pass (default 0 — cleanup demotes/prunes, it does not promote), max wall-clock budget, fail-closed when the validator is uncertain, no writes during an active task. Containment assumptions match the agents themselves: no secret-bearing environment, no arbitrary network, no hidden write channels into core state. Enforcement is deterministic (policies/hooks), never memory prose — memory guides, it does not enforce.

---

## 8. What to carry forward into implementation

The concrete things this design **brings along** — the levers, seams, and reusable discipline the build must use. (Repo facts verified 2026-06-28.)

**Reuse, don't reinvent:**

- Redaction: `redact_text` / `redact_mapping` in [providers/redaction.py](../../../../src/wastech_orchestrator/providers/redaction.py), before every write — same discipline as HITL/result-artifact paths.
- Atomic writes: the temp-file-then-rename pattern from [core/hitl.py](../../../../src/wastech_orchestrator/core/hitl.py) (`_atomic_json`).
- Prompt path-variable model: add a single allowlisted `memory_path` to `ALLOWED_PROMPT_VARS` ([core/prompts.py:21](../../../../src/wastech_orchestrator/core/prompts.py#L21)); populate it in the node prompt-variable builders; reference `{memory_path}` in the packaged role prompts (seeded into `.worc/flows/roles/` at install). It points at the per-stage **packet**, never the memory root.
- Supervisor seam: extend `Supervisor.finalize()` ([core/supervisor.py](../../../../src/wastech_orchestrator/core/supervisor.py)) to optionally return a `candidate_memory_delta` from the existing summary turn — **zero new LLM calls**; reuse the durable `__supervisor__` lineage.
- Idle hook: `watch_loop` in [cli.py](../../../../src/wastech_orchestrator/cli.py) — run `CleanupJob.run_once()` after `watch_once(...)` and before the poll sleep; short and interruptible.

**Build new (deterministic, model-free, unit-testable):**

- `MemoryService`, `PacketBuilder`, `CleanupJob`, `DerivedIndex` as separate modules (see §4.1). These need no fake-CLI fixtures — they are pure logic and should be tested without a model.
- `worc memory …` CLI: the first nested subparser (a `memory` parser with its own `add_subparsers`), modeled on `cmd_upgrade_config` (resolve config → validate → `--dry-run` plan → execute). Verbs: `show`, `validate`, `compact`/`defrag`, `restore`. (Distinct from `worc logs clean`, which is disk-space cleanup of artifacts — see [log-management.md](../../log-management.md).)
- `MemoryConfig` dataclass in [config/schema.py](../../../../src/wastech_orchestrator/config/schema.py), wired into `OrchestratorConfig` with a safe default, parsed in [config/loader.py](../../../../src/wastech_orchestrator/config/loader.py), documented in `packaged/config.example.yaml`, with a global enable/disable flag and the tier caps/TTLs/budgets. Bump `CONFIG_SCHEMA_VERSION` (currently **23**). Keep new **fatal** checks to a minimum — fatal only when there is no safe runtime fallback.
- Install-time `.gitignore` seeding for `.worc/memory/` (and the per-task `logs/<task-id>/memory/` packets follow the existing logs gitignore).

**Hard constraints to keep in front of the build:**

- **Do not** put memory in `state.db` — it is the state-machine store. If a DB is ever needed, use a separate `.worc/memory/memory.sqlite` (V2), never a `state.db` schema bump.
- **Do not** route through `task_artifact_dir` for the canonical store — that is per-task by construction; the canonical store is task-independent.
- **Cross-platform (Windows/Linux/macOS) from the start:** `pathlib` + `Path.as_posix()` for any stored/compared/displayed path string in records; explicit encoding and `newline=""` discipline for any templated/committed file; no `os.kill`/`signal` assumptions for the cleanup/idle control (reuse the self-managed PID/stop-file approach).
- **Audit posture:** keep the dedicated append-only `audit/log.jsonl` _and_ a lightweight `evaluations` marker row (the supervisor already writes `supervisor_final` there) so a memory write is visible in the audited-decision trail.

---

## 9. Roadmap (sequential, gated by measured need)

Each stage is unlocked by the evaluation signals in §10, not by a calendar. Earlier stages are not thrown away — later stages are additive.

### V1 — Files-first foundation (the core build)

`.worc/memory/` files; the three tiers (start the long-term tier as the simplest end-to-end loop, then add short-term + entity on the same seams); supervisor `candidate_memory_delta` at finalize; full `MemoryService` (redact → validate → trust → merge → promote/quarantine → audit); `PacketBuilder` deterministic per-stage packets via `memory_path`; metadata-first retrieval; safety from day one (trust levels, audit log, snapshots, quarantine, rollback); `MemoryConfig` with global enable/disable. **Explicitly not in V1:** vector DB, graph DB, cross-repo memory, automatic edits to repo docs/prompts/skills, direct provider write access to memory, hot-path/per-stage writes.

### V1.1 — Curation surfaces

`worc memory show | validate | compact | restore`; the bounded `CleanupJob` in the `watch_loop` idle gap. (Can land close behind V1; the cleanup job is what keeps V1 from rotting.)

### V2 — Structured index (separate SQLite + FTS)

A separate `.worc/memory/memory.sqlite` with FTS5 for long-term lessons and episodic summaries; `json`/`jsonl` retained for snapshots/export/human inspection. **Unlock when:** file-based dedup/merge code gets messy; indexed entity joins or FTS over hundreds–thousands of records are needed; whole-memory validation scans get slow; audit/rollback needs structured queries. Rough thresholds: ≳500 durable lessons or ≳5000 episodic records. Never overload `state.db`.

### V3 — Semantic recall (embeddings as a secondary layer)

Embeddings over **episodic summaries and normalized lessons only** (never raw transcripts), as a reranker/recall accelerator behind the metadata filter — never the primary truth store; retrieved results still pass rerank + validator before entering a packet. **Unlock when:** offline replay shows metadata-first retrieval misses known-relevant prior tasks/lessons (drifted wording, long-tail NL matches) _and_ the recall lift is measurable. Every embedded item keeps stable provenance and stale-handling.

### V4 — Entity graph (relations as first-class)

A richer relation graph / impact analysis ("if module X changes, which tests/configs/owners/history matter?"), built on top of the deterministic code index. **Unlock when:** tasks regularly require multi-hop reasoning across code entities and repo artifacts (issues/PRs ↔ files/functions ↔ owners ↔ hotspots), and the repo trends toward monorepo/service-graph complexity. Research baselines (KGCompass, Prometheus — the latter reports ~1.99s lightweight graph build per instance) show graphs pay off only when relational traversal is routine, not occasional.

A useful framing across stages: **start as files; add structure (SQLite) for query/scale; add embeddings for recall; add a graph for relations — each only when the simpler layer measurably fails.**

---

## 10. Evaluation plan (the gate for the roadmap)

The roadmap advances only on evidence, so the eval plan is part of the design, not an afterthought.

### 10.1 Metrics

| Category | Metrics |
| --- | --- |
| End-to-end quality | task success, first-pass test/review pass rate, fix-loop count, reopen rate, manual-action rate |
| Retrieval quality | packet precision, recall on known-relevant facts, explored-vs-used gap, unused-packet rate |
| Planning quality | first-relevant-file hit rate, files opened before first correct edit, plan acceptance by supervisor |
| Efficiency / cost | tokens per successful task, p50/p95 wall-clock, time-to-first-meaningful-edit, retrieval reads, cleanup time |
| Memory health | active records, promotion-acceptance/precision, duplicate rate, stale rate, contradiction rate, quarantine rate |
| Safety | secret-leak count (target 0), poisoned-write acceptance, external-only promotions (target 0), rollback frequency |
| Operator UX | time-to-debug a wrong retrieval, audit-trace completeness, restore success |

### 10.2 Experiments

- **Offline replay** on historical tasks, fixed models/prompts: memory-off vs long-term-only vs long-term+episodic vs +entity vs memory-on-without-entity-cards.
- **Read-path ablation:** no memory vs raw-root path vs stage-packet path.
- **Write-path ablation:** finalize-only vs step-level vs finalize-only + cleanup compaction.
- **Retrieval ablation:** metadata-only vs metadata+FTS vs metadata+embeddings.
- **Stage-level evals:** planning / implementation / review / fixing in isolation.
- **Staleness drills:** intentionally outdated commands and renamed modules → test stale detection + quarantine.
- **Poisoning drills:** inject low-trust misleading candidates → verify they do not auto-promote or outrank trusted repo-backed memory.
- **Long-horizon:** sequential task batches over the same repo → does memory compound over time, not just within one task.

Conceptual anchors: STATE-Bench (memory ops: update/locate/preserve/use), ContextBench (process-oriented retrieval: recall/precision/efficiency over gold contexts), Cody-style answer metrics (Essential Recall / Essential Concision / Helpfulness), MemoryArena (inter-session consistency).

### 10.3 Success criteria (initial)

- ≥10% reduction in tokens or wall-clock for repeated-repo tasks;
- ≥10% improvement in first-pass review/test success on repeated hotspots;
- stale-contradiction rate <5%; secret-leak rate 0; external-only long-term promotions 0;
- cleanup overhead small enough to stay outside the critical path;
- no vector/graph infra added without a measurable recall/quality lift.

### 10.4 Failure indicators (red flags)

Packets often ignored/irrelevant; memory grows faster than cleanup controls it; many promotions later rolled back; agents follow stale rules more than without memory; operator trust drops because memory is opaque/noisy; memory overrides better current-code evidence; infra added with no measured lift.

---

## 11. Rejected alternatives & trade-offs

| Rejected (for now) | Why |
| --- | --- |
| `state.db` as the memory store | Wrong ownership; breaks "state.db is the state machine only"; schema bump per shape change; un-hand-editable. |
| Vector-first / embeddings from day one | Weak provenance, poor exact-repo-semantics recall, extra infra/cost — premature before the shape is known. |
| Knowledge-graph-first | High maintenance/schema burden before a proven multi-hop need; pays off only when traversal is routine. |
| Provider-native memory as canonical layer | Machine-local / instruction-layer, not portable; violates the supervisor-owned, provider-neutral design. |
| Session-resume-as-memory | Architecture rejects the vendor session as truth; not transferable between providers. |
| One giant `MEMORY.md` / append-only dump | Prompt bloat + staleness; rots without structured metadata, promotion logic, and stale handling. |
| Supervisor-heavy (LLM packet building / cleanup) | Extra tokens, nondeterministic quality, hidden promotion logic, larger poisoning surface. |
| Aggressive autonomous "autodream" | Unjustified safety risk given memory-poisoning evidence; replaced by a bounded deterministic job. |
| A dedicated per-task memory-synthesis LLM turn | Taxes every task; replaced by piggybacking the supervisor's existing finalize turn. |

Accepted trade-offs: we give up some semantic recall (no day-one embeddings) for provenance, simplicity, and safety; some query elegance (no day-one SQLite/graph) for inspectability and path-based compatibility; and full autonomy (memory is advisory) for debuggability. These are the right trades for a single-repo, single-active-task, supervisor-owned orchestrator.

---

## 12. How this refines the predecessor draft

[orchestrator-memory.md](../../orchestrator-memory.md) was a sound stake-in-the-ground; this blueprint keeps its spine (supervisor-distilled, `.worc/memory/` files, three tiers, redaction, piggyback on `finalize`, `memory_path`, idle cleanup, config enable/disable, no `state.db`, no embeddings day-one) and sharpens it where the research pointed:

- **Narrow supervisor + deterministic services.** "Supervisor owns it" → supervisor _distills_ (emits a candidate delta); `MemoryService`/`PacketBuilder`/`CleanupJob`/`DerivedIndex` own validation, promotion, retrieval, and cleanup. Cheaper, safer, testable.
- **Memory ≠ derived index** is now a first-class principle (P3); repo map/symbol index are a separate rebuildable plane.
- **Read path is a stage packet, not the memory root.** Precision-first two-stage retrieval with hard caps and progressive disclosure replaces "inject a path to the curated files".
- **Full trust/provenance model + quarantine + snapshots + rollback** from day one, beyond redaction + a single audit row.
- **"Autodream" is de-mystified** into a bounded, deterministic reconciliation job that never creates long-term lessons and never edits docs/code/skills.
- **Provider-neutral canonical format**, curated views to providers, made explicit.
- **An evaluation plan gates the roadmap** — each storage-ladder step is unlocked by measured failure of the simpler design.
- **Empirical grounding** (AGENTbench, ContextBench, memory-poisoning work) is now the stated _reason_ for "small, precise, strict".

---

## 13. Open questions for the ADR

- **Autodream cadence & budget.** What fires it (every idle tick / after N tasks / a time threshold) and the exact per-pass scan/edit/wall-clock budget. (Default promotions-per-pass = 0.)
- **Codebase reconciliation source of truth.** How `DerivedIndex` decides an entity/lesson is stale (path/symbol existence, rename remap, convention re-check) and the confidence threshold for "stale".
- **Promotion thresholds.** The concrete numbers (≥2 tasks? window length? reviewer-signal weighting) — expect to tune once memory is live.
- **CLI verbs & scheduling.** Final `worc memory …` surface and whether scheduling is external cron vs. the autodream hook.
- **Packet variable & caps.** Final name/semantics of `memory_path` and the exact per-stage caps.
- **Audit home.** Dedicated `audit/log.jsonl` vs. `evaluations` rows vs. both (this blueprint recommends both).
- **Episodic detail home.** Where resume/debug-grade detail lives so short-term memory stays a distillation layer, not a transcript store.

---

## 14. Sources

### Production / official docs

1. Anthropic — Claude Code memory: https://docs.anthropic.com/en/docs/claude-code/memory
2. Anthropic — Automate actions with hooks (deterministic enforcement).
3. Anthropic — Effective harnesses for long-running agents (compaction alone is insufficient).
4. OpenAI — Codex memories: https://developers.openai.com/codex/memories
5. OpenAI — Codex best practices / `AGENTS.md`: https://developers.openai.com/codex/learn/best-practices
6. OpenAI — Agent Skills (progressive disclosure) & Codex prompting guide.
7. OpenAI — Building reliable agents with memory and compaction (reviewed artifact = truth; reusable workflow lessons).
8. OpenAI — `AGENTS.md` + MCP as open, provider-agnostic conventions (portability).
9. GitHub — Copilot Memory (facts/preferences with citations, review/delete, 28-day retention): https://docs.github.com/en/copilot/concepts/agents/copilot-memory
10. GitHub — Repository indexing: https://docs.github.com/en/copilot/concepts/context/repository-indexing
11. OpenAI Agents SDK — memory sandbox (summaries/rollups, background formation): https://openai.github.io/openai-agents-python/sandbox/memory/
12. LangGraph / LangMem — semantic/episodic/procedural memory; hot-path vs background writes: https://docs.langchain.com/oss/python/concepts/memory
13. Letta — memory blocks & archival memory: https://docs.letta.com/guides/agents/memory-blocks
14. Devin — knowledge onboarding / DeepWiki: https://docs.devin.ai/onboard-devin/knowledge-onboarding
15. Augment Code — memory review & context lineage: https://www.augmentcode.com/blog/how-we-built-memory-review
16. Sourcegraph — Cody context engineering & "toward infinite context" (retrieval quality dominates; long context has cost/latency).

### Research / evaluation

17. Gloaguen et al. — Evaluating `AGENTS.md` (marginal/negative context-file gains; +20% cost).
18. Li et al. — ContextBench (process-oriented retrieval; over-retrieval; recall-over-precision).
19. RepoGraph — repository-level code graphs for SWE: https://arxiv.org/html/2410.14684v1
20. KGCompass — repo-aware KG linking PR/issues ↔ code entities for repair.
21. Prometheus — working memory + lightweight repository graph for long-horizon navigation.
22. MemCoder — structured memory from historical commits / human-verified solutions.
23. AWM (Agent Workflow Memory) — reusable procedural workflows as memory.
24. Memory survey — Memory for Autonomous LLM Agents (write–manage–read; substrate trade-offs).
25. MemoryArena — long-term memory & inter-session consistency: https://arxiv.org/abs/2602.16313
26. STATE-Bench — benchmark for AI agent memory operations: https://opensource.microsoft.com/blog/2026/05/19/introducing-state-bench-a-benchmark-for-ai-agent-memory/
27. MPBench — memory poisoning attacks against language agents: https://arxiv.org/html/2606.04329v1
28. Agent-Native Memory Systems Evaluation: https://arxiv.org/html/2606.24775v1
29. MCP security best practices + Unit 42 / memory-poisoning field writeups (persistent memory as attack surface).

### Repo context

30. Predecessor direction — [orchestrator-memory.md](../../orchestrator-memory.md); supervisor role split — [supervisor-role-split.md](./supervisor-role-split.md).
31. Internal deep research — [worc-report/](./worc-report/worc-deeep-research-memory-report.md); external deep research — [3rd-party-report/](./3rd-party-report/00-3rd-party-deep-research-memory-report.md).
32. Seams — [core/supervisor.py](../../../../src/wastech_orchestrator/core/supervisor.py), [core/prompts.py](../../../../src/wastech_orchestrator/core/prompts.py), [providers/redaction.py](../../../../src/wastech_orchestrator/providers/redaction.py), [cli.py](../../../../src/wastech_orchestrator/cli.py), [config/schema.py](../../../../src/wastech_orchestrator/config/schema.py).
