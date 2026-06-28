# Orchestrator memory (cross-task reuse, defrag, autodream)

Status: **superseded** (2026-06-28) — consolidated into the research-backed [Memory subsystem — architecture blueprint](memory/memory-architecture-blueprint.md), which is now the authoritative design to build the implementation plan and ADR(s) from. Date: 2026-06-26 Owner: Vladimir Makarevich

> This document is kept as the historical exploratory predecessor. The blueprint keeps its spine and refines it where the [internal](memory/worc-report/worc-deeep-research-memory-report.md) and [external](memory/3rd-party-report/00-3rd-party-deep-research-memory-report.md) deep-research efforts and the [supervisor role-split note](memory/supervisor-role-split.md) pointed (see the blueprint's "How this refines the predecessor draft").

This is an exploratory stake-in-the-ground, not a build spec. It records the direction for a persistent memory subsystem that lets the orchestrator reuse context across tasks, owned by the supervisor. It is a deliberate superset of the existing [Lightweight project memory](README.md) backlog item — that item is the minimal first slice; this document frames the full vision so the slice is built toward the right shape. This revision grounds the design in the actual codebase (supervisor, artifact/redaction, CLI/watch, config/prompt seams) and corrects where the original framing did not fit how the project is wired.

The subsystem has three tiers — short-term, long-term, entity — plus an operator-run defragmentation/cleanup command and a background "autodream" self-cleanup that runs between task ticks. The supervisor owns it, because it is the one layer with the full picture over every step of every task. All three tiers are in scope for the first build (not a "grow into it later" sequence), though they can be landed in stages.

## The problem

Today each task is an island. Three concrete costs:

- **Repo re-discovery.** Every task re-derives the target repo's structure and conventions from scratch — expensive in tokens and wall-clock, and the cost recurs identically on a repo the orchestrator has already worked on many times.
- **Lost lessons.** Decisions, gotchas, and conventions surfaced during one task (what broke, what the reviewer flagged, which approach was rejected and why) do not carry into the next task. The next agent rediscovers or re-violates them.
- **No entity knowledge.** There is no durable representation of the entities the orchestrator reasons about across runs — key modules, files, people/contacts, and prior tasks — so cross-task reasoning ("we touched this module last week and it has a fragile test") is impossible.

Memory bloat (duplicates, stale entries, contradictions) is _not_ a present pain — there is no memory yet. It is the predictable failure mode once memory exists, which is why defrag and autodream are part of the design from the start rather than bolted on later.

## Constraints

These bound any eventual design and are non-negotiable invariants from [.agents/rules/](../../.agents/rules/):

- **No secrets in memory — even though it is gitignored.** Memory is artifacts-class storage; the "no secrets in logs, SQLite, or artifacts" invariant applies in full. Crucially, gitignoring memory does _not_ remove the redaction requirement: the supervisor writes memory, but agents read it _back into their prompts_, and a node's output lands in committed artifacts (`summary.md` is the PR body; diffs are committed). A secret that enters memory unredacted can therefore re-surface in a committed artifact. Every write must pass `redact_text` / `redact_mapping` (`src/wastech_orchestrator/providers/redaction.py`) before it touches disk, exactly like the HITL and result-artifact paths already do.
- **Memory is not an unbounded context dump.** It must stay small and curated — bounding it is precisely what defrag/autodream exist to do. Because memory is injected into prompts by _path_ (the agent reads the file), an unbounded file becomes an unbounded read cost on every downstream task; curation is what keeps that read cheap. Persisting raw agent transcripts or whole vendor sessions is forbidden (the vendor session is not a source of truth — architecture §"Contracts").
- **Supervisor stays advisory.** The supervisor is a persistent oversight layer, not a node (`src/wastech_orchestrator/core/supervisor.py`); it cannot rework a step. Memory may shape the prompts a node receives, but the Core still decides. Memory must never become a side channel that lets the supervisor mutate task control flow — it injects context, it does not route.
- **Autodream self-modification must be bounded and audited.** Any automatic editing of memory between ticks is autonomous self-modification and must be subject to a bound and an audit record, consistent with "every per-task automatic loop has a configurable limit" and the audited-decision posture. It runs only in the idle gap between tasks (`watch_loop`, `src/wastech_orchestrator/cli.py`) — never against an active task's working tree, which the single-slot invariant (`Orchestrator.acquire_slot`) already guarantees is quiescent in that window.
- **One repo, single active task (v1).** Cross-repo memory transfer and concurrent-task memory access are out of scope while the single-active-task / single-repo invariants hold.

## Alternatives considered

| Option | Why not (yet) |
| --- | --- |
| Do nothing | Leaves all three costs in place; re-discovery alone is a recurring token tax on every task against a known repo. |
| Ship only "Lightweight project memory" (small lessons file) | Covers the lost-lessons cost but not repo re-discovery or entity knowledge, and has no curation story — it will rot. This vision keeps that as the long-term tier but lands all three tiers together. |
| Store memory in `state.db` (new tables) | Transactional, but breaks the "state.db is the state machine only" invariant, needs a schema bump per shape change, and makes memory impossible to hand-edit or diff. Rejected in favour of file artifacts the operator can inspect directly. |
| Store memory under `logs/<task-id>/` (the original framing) | Wrong home: `task_artifact_dir` is **per-task** and `.worc/logs/` is gitignored and rotated. Cross-task memory must outlive any single task, so it cannot be scoped under a task id. Corrected below to a task-independent `.worc/memory/` root. |
| Per-task vendor-session resume as the memory | Rejected by architecture: the vendor session is not a source of truth; artifacts are. Sessions are not transferable between providers. |
| Embeddings / vector store from day one | Premature. Adds a dependency and a retrieval-quality problem before we know the memory's shape or size. Deferred until a concrete need is proven. |
| A dedicated memory-synthesis LLM turn per task | Cleaner separation of summary vs memory, but adds one LLM call (tokens + wall-clock) to every task. Rejected in favour of piggybacking on the synthesis the supervisor already runs at task close. |

## Decision

Build a supervisor-owned, cross-task memory subsystem with three tiers and explicit curation (operator command + autodream), because the supervisor is the only layer with the whole-task picture and curation is the difference between durable memory and a slowly-rotting dump. The cost of the rejected alternatives: doing nothing keeps the token tax and re-violated conventions; the lessons-only slice cannot reduce re-discovery or support entity reasoning; vector storage spends a dependency and a retrieval problem before the shape is known; a separate synthesis turn taxes every task.

**Storage home — corrected.** Memory lives in a **task-independent** directory `.worc/memory/`, a sibling of `state.db`, `flows/`, and `config.yaml`, and is **gitignored** like `state.db` and `logs/`. It is local orchestrator state, not committed and not part of any PR — which keeps the leak surface small and avoids PR noise. (The original "modeled on the `hitl`/`summary` artifact paths under `logs/`" was a category error: those paths are per-task via `task_artifact_dir(artifacts_root, task_id)` → `<root>/logs/<task-id>/`, and so cannot hold state that must survive across tasks. We reuse the _redaction and atomic-write discipline_ of those paths, but not the per-task location.)

The three tiers, as files under `.worc/memory/` (names indicative, not final):

- **Short-term** — `recent.jsonl`: per-task memory-deltas for a small window of recent tasks; cheap to append, expected to be pruned aggressively.
- **Long-term** — `lessons.md`: durable lessons and conventions promoted out of short-term once they prove stable/repeated. This is the [Lightweight project memory](README.md) slice, now one tier of three.
- **Entity** — `entities.json`: a curated map of the things reasoned about across runs (key modules, files, contacts, prior tasks) with links between them.

**Write path — piggyback on `finalize()`.** The supervisor already runs one LLM synthesis turn at task close to write `summary.md`. That same turn additionally emits a structured memory-delta (lessons learned, entities touched), which is redacted and merged into the tier files. This adds **zero new LLM calls per task** — memory is a byproduct of the whole-task review the supervisor already performs, and uses its existing durable session (the `__supervisor__` lineage in `node_lineage`). A `supervisor_final`-style audit record in `evaluations` notes that a memory write occurred, keeping the audited-decision posture.

**Read path — inject a path, never content.** Prompt rendering uses a strict allowlist (`ALLOWED_PROMPT_VARS` in `src/wastech_orchestrator/core/prompts.py`) and deliberately never injects large content. Memory therefore reaches working nodes as a new allowlisted variable `memory_path`, populated in the node prompt-variable builders, with role prompts referencing `{memory_path}` so the agent reads the curated files itself. This keeps memory consistent with how task/plan/diff are already passed (by path) and keeps the prompt small.

**Curation — operator command + autodream.** A new `worc memory …` CLI surface (manual or scheduled) checks memory against the real codebase and drops stale/duplicate/contradicted entries; and `autodream`, a bounded, audited background pass in the `watch_loop` idle gap, does the same self-cleanup opportunistically while no task is active.

**Enable/Disable in config.yaml** Should be able to disable all memory functionality.

## Open questions

- **Autodream trigger and safety.** What exactly fires it (every idle tick? after N tasks? a time threshold?), and what is the bound + audit record for autonomous memory edits? It hooks the idle gap in `watch_loop` (after `watch_once`, before the sleep), where the single-slot invariant guarantees no active task — but the cadence and the per-pass edit budget are unresolved.
- **Codebase reconciliation.** How does entity-memory get verified against the actual repo during cleanup — does a referenced file/module still exist, has it moved, does a recorded convention still hold? What is the source of truth for "this entry is stale"?
- **Promotion boundary.** What promotes a short-term entry to long-term, and when — frequency, recency, explicit reviewer signal? This is decided before the first real lesson exists, so expect to tune it once memory is live.
- **CLI surface and schedule.** Exact verbs (`worc memory clean` / `defrag` / `show`?), and whether scheduling is an external cron vs. the autodream hook. (Note the related `worc logs clean` in [log-management.md](log-management.md) — disk-space cleanup of artifacts is a distinct concern from memory curation.)
- **Scope of supervisor expansion.** Exactly which new powers the supervisor gains as memory owner, kept within "advisory, cannot rework, Core decides."

## Implementation notes

Not a full spec — the seams this lands on, named against the current code.

**Storage.** New directory `.worc/memory/` (task-independent, gitignored). Reuse `redact_text` / `redact_mapping` (`providers/redaction.py`) before every write and the atomic temp-file-then-rename pattern from `core/hitl.py` (`_atomic_json`). Do **not** route through `task_artifact_dir` — that is per-task by construction. Add `.worc/memory/` to the install-time `.gitignore` seeding.

**Owner / write.** `core/supervisor.py` — extend the existing `finalize()` (built per task in `orchestrator.py` `_build_supervisor`, run from the publish-node finalize hook) so the synthesis turn that writes `summary.md` also produces a structured memory-delta applied to the tier files. Audit the write as an `evaluations` row (the supervisor already writes `supervisor_final` there). No new LLM turn; reuse the `__supervisor__` durable session.

**Read / prompt injection.** Add `memory_path` to `ALLOWED_PROMPT_VARS` (`core/prompts.py`); populate it in `_prompt_variables` of `core/flow/nodes/agent.py` and `core/flow/nodes/evaluator.py`; reference `{memory_path}` in the packaged role prompts under `packaged/flows/roles/` (these are seeded into `.worc/flows/roles/` at install). Content stays out of the prompt — only the path goes in.

**CLI.** The parser is flat argparse with no command groups yet (`build_parser`, `cli.py`). `worc memory …` needs the first nested subparser (a `memory` parser with its own `add_subparsers`) plus dispatch in `main()`. Model the command body on `cmd_upgrade_config` (resolve config → validate → plan with `--dry-run` → execute).

**Autodream.** Hook the idle gap in `watch_loop` (`cli.py`) — after `watch_once(...)` returns and before the poll-interval sleep (`sleep_fn(poll_interval)` / `stop_event.wait(poll_interval)`). Must be short / interruptible so it never delays the next task pickup, and bounded + audited per the constraint above.

**Config.** Add a `MemoryConfig` dataclass to `config/schema.py`, wire it into `OrchestratorConfig` with a safe default, parse it in `config/loader.py`, document it in `packaged/config.example.yaml`, and bump `CONFIG_SCHEMA_VERSION` (currently 16). Keep new fatal checks to a minimum — only fatal if there is no safe runtime fallback.

Start from the [Lightweight project memory](README.md) long-term tier as the simplest end-to-end loop (write at finalize → read via `memory_path`), then add the short-term and entity tiers and the curation surfaces on the same seams.
