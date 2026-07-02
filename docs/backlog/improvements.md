# Improvements

Status: **5 of 9 implemented** (2, 4, 6, 7, 8 done; 1, 3, 5, 9 open) Date: 2026-07-01 Owner: Vladimir Makarevich

Items 1, 3, 5, and 9 share the prompt/supervisor authoring surface and are worked through together in one refinement ADR: [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md).

This file aggregates improvement ideas captured after real `worc` usage. The source intake list is [00-raw-topics.md](00-raw-topics.md). We process one item at a time: inspect the current implementation, define a bounded improvement task, record constraints and scope, then move to the next item.

## Intake queue

| # | Topic | Status |
| --- | --- | --- |
| 1 | Supervisor finalization should also surface technical debt and refactor candidates | done |
| 2 | `config.example.yaml` should be copied from packaged data during install, with comments intact | done |
| 3 | Supervisor/summary prompts should be overrideable per flow, with fallback to root/static prompt | done |
| 4 | Remove stale historical comments and implementation notes from the codebase | done |
| 5 | Document all prompt variables available to role files | done |
| 6 | Rework delivered role/flow directory layout so each flow owns its prompt folder, including `implementation` | done |
| 7 | Re-evaluate whether the repo-root `tasks/processing` folder is still needed | done |
| 8 | Write a tutorial and best-practices guide for custom flows | done |
| 9 | More flexible prompt-variable substitution: no unknown-var leaks, sanctioned optional-var pattern | done |

## 01. Supervisor finalization: technical debt and refactor signals

Status: **done** (2026-07-02, Cluster B) Source: [00-raw-topics.md](00-raw-topics.md) Refined in: [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md). Landed as the per-flow `supervisor.emit_follow_ups` opt-in: the existing finalize turn (no extra LLM call) emits an evidence-gated `follow_ups` array into `summary.json` + a "Technical debt / follow-ups" section in `summary.md`; the packaged `implementation` flow sets it, research/prose flows do not. Schema hardcoded in `core/supervisor.py`.

### Current state

- `Supervisor.finalize()` writes only a human-readable `summary.md` plus local `summary.json`.
- The packaged supervisor prompt asks for "what / how / integration / why" and advisory caveats, but it has no dedicated contract for technical debt, refactor candidates, or code areas that should be revisited.
- The supervisor is the only layer that sees the whole run end to end: plan, implementation, fix loops, review findings, and final outcome. That context is currently preserved mostly as prose, not as a stable operator-facing artifact.

### Problem

Today the orchestrator can finish a task with useful narrative context, but it does not reliably extract "what should be improved next" from that run. As a result, technical debt and refactor signals stay implicit inside `summary.md`, step observations, or operator memory. That wastes the supervisor's whole-task vantage point.

### Decision

Extend the existing supervisor finalization seam so the same finalize turn produces two outputs:

- the current plain-language summary used as `summary.md` and PR body;
- a best-effort structured improvement payload focused on technical debt, refactor candidates, risky shortcuts, and missing follow-up work observed during the run.

This stays advisory-only. The supervisor suggests improvements; it does not block publishing, route the flow, or auto-create tasks.

### Proposed shape

- Keep `summary.md` as the human-facing handoff artifact.
- Make the closing section explicitly call out "Technical debt / follow-ups" when evidence exists.
- Extend `summary.json` or add a sibling artifact such as `improvements.json` with machine-readable records like:
  - `technical_debt`
  - `refactor_candidates`
  - `missing_tests`
  - `scope_concerns`
- Each record should be evidence-backed and minimal: `title`, `rationale`, `paths`, `evidence`, `severity`, `action_hint`.

### Constraints

- No extra LLM turn. Reuse the existing `Supervisor.finalize()` call.
- Missing or malformed structured output must never block `summary.md` or publishing.
- No weakening of the supervisor invariant: advisory only, read-only, no routing power.
- No raw secrets, full diffs, or unbounded transcripts in the new artifact.
- Suggestions must be grounded in touched files or observed run artifacts, not speculative filler.

### Scope

In scope:

- supervisor finalize contract;
- packaged supervisor prompt;
- local artifact format for structured improvement hints;
- docs that describe the supervisor output contract.

Out of scope:

- automatic creation of backlog tasks from every completed run;
- using these hints as a hard quality gate;
- a separate second-pass "debt analyzer" stage.

### Acceptance criteria

- A successful task can emit both the normal summary and a structured improvement payload in the same finalize call.
- When the structured payload is absent or invalid, the current summary fallback behavior remains unchanged.
- The packaged supervisor prompt explicitly asks for technical debt/refactor candidates only when supported by evidence.
- Operators can inspect a stable machine-readable artifact instead of mining the debt signal from free-form prose.
- The new artifact and prompt contract are documented in the backlog/architecture docs touched by the change.

### Affected seams

- `src/wastech_orchestrator/core/supervisor.py`
- `src/wastech_orchestrator/packaged/flows/roles/supervisor.md`
- `docs/functional/blocks/B31-supervisor.md`
- `docs/worc_architecture.md`
- future alignment point: `docs/backlog/memory/` structured finalize output

### Open question

Should these structured hints remain local task artifacts only, or should a later operator-reviewed workflow promote selected items into a durable backlog automatically? For now the safer scope is local artifact only.

### Why this shape

- It uses an existing seam instead of inventing a second supervisor phase.
- It matches the accepted memory direction, where `finalize()` is the place for structured whole-task output.
- It preserves the invariant that the supervisor is advisory and read-only.

## 02. Install should ship a local `config.example.yaml`

Status: **done** Source: [00-raw-topics.md](00-raw-topics.md)

### Current state

- The packaged `src/wastech_orchestrator/packaged/config.example.yaml` already exists and is the commented source of truth for field documentation.
- `install` writes only a generated `.worc/config.yaml`, assembled from `build_config_mapping()` and serialized through PyYAML.
- That generated config is executable, but it does not preserve the packaged example's comments and explanations.

### Problem

After install, the operator has a live `config.yaml` but no local, comment-rich reference copy beside it. The commented example exists only in package data and repo docs, so the most useful field-level guidance is not delivered into the installed runtime home.

### Decision

Keep generating the real `.worc/config.yaml`, but also copy the packaged `config.example.yaml` byte-for-byte into the installed `.worc/` home as a reference artifact.

Recommended target: `.worc/config.example.yaml`.

### Scope

In scope:

- install/reconfigure copy of the packaged example;
- docs pointing operators to the local example copy;
- tests that pin byte-for-byte delivery.

Out of scope:

- preserving comments inside the generated `.worc/config.yaml`;
- rewriting the installer to materialize `config.yaml` from the example template;
- making `upgrade-config` comment-preserving.

### Acceptance criteria

- `install` writes `.worc/config.example.yaml` from packaged data.
- `install --reconfigure` refreshes that file from the packaged copy.
- The copied file preserves comments and formatting byte-for-byte.
- Docs explain the difference between executable `.worc/config.yaml` and reference `.worc/config.example.yaml`.

## 03. Flow-local supervisor/final-summary prompts with fallback

Status: **done** (2026-07-02, Cluster B) Source: [00-raw-topics.md](00-raw-topics.md) Refined in: [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md). Landed as the flow `supervisor:` block: `role_file` (observe lens, fallback flow → `config.supervisor.role_file` → built-in) and `finalize_role_file` (finalize lens, fallback flow → built-in), both flow-dir-contained. Only wording moves to files; the structured-output schemas stay in code.

### Current state

- The supervisor prompt comes from the global config key `supervisor.role_file`, resolved inside the active flow directory.
- In practice the packaged/operator flows use a shared `roles/supervisor.md`, so different flows still inherit the same supervisor framing.
- Final whole-task synthesis uses `_finalize_prompt()` code text rather than a flow-local prompt contract.

### Problem

`implementation`, `deep_research`, `security_audit`, and future custom flows do not necessarily want the same supervisor lens or the same final-summary emphasis. Today the seam is technically present, but the contract is not flow-local and is not explicit for flow authors.

### Decision

Add an explicit flow-local prompt override contract for the supervisor, with fallback:

1. flow-local prompt declared by the flow;
2. shared configured prompt (`config.supervisor.role_file`);
3. minimal built-in hardcoded fallback.

The same principle should cover both step observation and whole-task final synthesis.

### Scope

In scope:

- flow schema/config contract for supervisor prompt overrides;
- fallback resolution order;
- docs for flow authors.

Out of scope:

- turning the supervisor into a graph node;
- adding a second LLM call just for summary prompt specialization.

### Acceptance criteria

- A flow can declare its own supervisor prompt(s) without changing global config.
- When a flow-local prompt is absent, the current shared prompt still works.
- When both file-based options fail, the supervisor still falls back to the minimal built-in instruction and never blocks the run.
- Docs explain where a flow author puts these prompts and how fallback works.

### Note

This task aligns naturally with task 06 (flow-owned prompt directories), but the override contract should be defined independently of the final on-disk layout.

## 04. Remove stale historical comments and implementation notes

Status: **done** Source: [00-raw-topics.md](00-raw-topics.md)

### Current state

The codebase still contains history-oriented comments and notes such as:

- `supervise_impl/supervise_fix` references;
- `summary-as-node` / `old summary provider` language;
- dated revision notes like `2026-06-19 revision`;
- phrases like `legacy ordering` in runtime code and packaged flow assets.

### Problem

These comments are not explaining current behavior; they are narrating how the code used to work. For humans they add noise, and for coding agents they enlarge prompt context with stale concepts that no longer exist in the actual runtime.

### Decision

Run a targeted cleanup pass over runtime code, packaged flows, and non-archival docs:

- keep comments that explain current invariants or non-obvious design choices;
- remove comments that only describe deleted architecture or implementation history that can be recovered from git.

### Scope

In scope:

- `src/` runtime comments and docstrings;
- packaged flow YAML comments and packaged role assets;
- current docs that describe active behavior.

Out of scope:

- archive/backlog/history documents;
- deliberate historical notes in ADR/backlog analysis files.

### Acceptance criteria

- Runtime code and packaged assets no longer mention removed `supervise_*` nodes or similar dead concepts.
- Comments like `legacy ordering` are either rewritten into present-tense rationale or removed.
- Current docs stop referencing deleted architecture as if it were useful runtime context.

## 05. Canonical prompt-variable contract for role authors

Status: **done** (2026-07-02, Cluster A) Source: [00-raw-topics.md](00-raw-topics.md) Refined in: [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md). Delivered as `packaged/guide/flows/prompt-variables.md` (seeded to `.worc/guide/flows/`) with the preflight anti-drift lint (`lint_prompt_variables`) guarding against `ALLOWED_PROMPT_VARS` drift.

### Current state

- `ALLOWED_PROMPT_VARS` in `core/prompts.py` is the actual contract.
- `docs/configuration.md` and `docs/functional/blocks/B15-prompt-templates.md` already document the allowlist.
- The author-facing flow/role-writing surfaces do not present this as a single canonical guide for people creating their own prompts and custom flows.

### Problem

The gap is no longer "variables are undocumented at all"; the gap is discoverability and authoring ergonomics. A flow author should not need to read code or the functional map to know which variables exist, which node kinds populate them, and when they may be empty.

### Decision

Publish one canonical author-facing prompt-variable reference and link to it from the flow-authoring docs. The reference should explain:

- each variable name;
- which runner populates it (`agent`, `evaluator`, `supervisor`);
- when it may be empty;
- conditional block syntax `{?name}...{/name}`.

### Scope

In scope:

- docs consolidation and cross-linking;
- explicit role-author guidance;
- optional low-cost anti-drift guard between docs and `ALLOWED_PROMPT_VARS`.

Out of scope:

- widening the allowlist itself;
- injecting new content variables beyond the current path/metadata model.

### Acceptance criteria

- A role author can find the full variable contract from the normal docs/guide path, not only from the code-derived map.
- The docs distinguish allowlisted names from runner-specific availability.
- Conditional blocks are documented where custom role authors will actually see them.

## 06. Flow-owned prompt directories

Status: **done** Source: [00-raw-topics.md](00-raw-topics.md)

### Current state

- Packaged flow YAML files live at `packaged/flows/<task_type>.yaml`.
- Prompt files are stored in a shared `packaged/flows/roles/` tree with mixed conventions: `deep_research` uses `roles/research/...`, `security_audit` uses `roles/audit/...`, while `implementation` roles sit directly under `roles/`.
- `install` copies this tree as-is into `.worc/flows/`.

### Problem

Prompt ownership is blurred. A flow author cannot glance at one folder and see "these are all prompts for this flow", and the `implementation` flow in particular is not organized the same way as the others. This also makes per-flow supervisor/final-summary prompts harder to reason about.

### Decision

Adopt one canonical layout where each flow owns its prompt assets, while keeping the current registry-friendly YAML location:

- `.worc/flows/<task_type>.yaml` remains the dispatch file;
- prompts move under a flow-owned subdirectory such as `.worc/flows/<task_type>/...`;
- `role_file` paths in the YAML point into that subdirectory.

This keeps `FlowRegistry.resolve(<task_type>.yaml)` simple and avoids a larger registry redesign.

### Scope

In scope:

- packaged flow asset relocation;
- updated `role_file` references;
- install/reconfigure copy behavior;
- docs for the new layout.

Out of scope:

- nested flow YAML discovery;
- automatic migration of every user-edited operator flow.

### Acceptance criteria

- Each packaged flow has a single obvious home for its prompts.
- `implementation` uses the same ownership pattern as the other built-ins.
- `install` still copies packaged flow assets byte-for-byte and the delivered copies remain active/editable.
- Docs show the new layout for both built-in and custom operator flows.

## 07. Remove the repo-root `tasks/processing` lifecycle folder

Status: **done** Source: [00-raw-topics.md](00-raw-topics.md)

### Current state

- `install` scaffolds `tasks/pending`, `tasks/processing`, `tasks/done`, `tasks/failed`.
- Docs and constants still describe a `pending -> processing -> done/failed` lifecycle.
- The actual `run_task()` path processes the pending file directly and terminal relocation moves only to `done`/`failed`; there is no real claim move into `processing`.

### Problem

The documented lifecycle and the runtime behavior have drifted apart. `processing` currently behaves more like a historical contract than an active runtime requirement, but it still complicates docs, git staging logic, source-path lookup, and operator understanding.

### Decision

Simplify the task-file lifecycle to:

- `pending`
- `done`
- `failed`
- `.worc/tasks/rejected` for quarantine

Use `state.db` task status as the source of truth for "currently running", instead of a physical `processing/` move.

### Scope

In scope:

- remove `processing` from scaffolding, docs, and code paths that only preserve the old contract;
- simplify lookup/staging logic that still special-cases `processing`.

Out of scope:

- adding a new physical "claimed" lifecycle folder instead;
- changing the single-slot state-machine contract.

### Acceptance criteria

- New installs no longer create `tasks/processing`.
- Docs stop promising a move into `processing`.
- Runtime code and tests no longer require `processing` as a valid lifecycle state on disk.
- Active-task visibility remains available through `status`/SQLite, not through a physical folder.

## 08. Custom flow tutorial and best-practices guide

Status: **done** Source: [00-raw-topics.md](00-raw-topics.md)

### Current state

- The docs explain that `task_type` selects a flow and that operator flows live under `.worc/flows/`.
- The repo does not provide a real end-to-end authoring tutorial for creating, validating, registering, and debugging a custom flow.

### Problem

The capability exists, but the authoring path is still expert-only. A new operator can discover that custom flows are possible without learning how to build one safely and idiomatically.

### Decision

Add a dedicated custom-flow authoring guide plus a short best-practices checklist. The guide should cover:

- minimal custom flow structure;
- where YAML and role files live;
- `task_type` registration;
- how `preflight`/validation catches flow errors;
- how to inspect rendered prompts and artifacts;
- common invariants and foot-guns.

### Scope

In scope:

- repo docs and installed `.worc/guide/` docs;
- links from configuration/task-authoring docs;
- practical examples.

Out of scope:

- a flow scaffolding CLI;
- GUI tooling for flow authoring.

### Acceptance criteria

- A new operator can create a minimal custom flow from the docs without reading the code first.
- The guide explains how to register the flow and invoke it via `task_type`.
- The best-practices section covers security ceilings, `role_file` discipline, prompt variables, validation, and debugging.

## 09. More flexible prompt-variable substitution

Status: **done** (2026-07-02, Cluster A) Source: real `worc` usage Refined in: [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md). Landed as adoption of the existing `{?name}…{/name}` optional-var pattern in the packaged prompts + the preflight anti-drift lint (`referenced_variables` / `lint_prompt_variables`); the flow-derived valid-set (`valid_prompt_vars`) is the seam the node-output channel extends.

### Current state

- The Core populates the full allowlisted variable set for every node in `_prompt_variables`; operators do not "declare" variables in the flow — they only choose which to reference in a `role_file`.
- `render_prompt` has three behaviors: an allowlisted `{name}` with a value is substituted; an allowlisted `{name}` that is empty/`None` renders as the empty string; an unknown `{name}` (a typo, or a variable that does not exist) is left verbatim so code/JSON braces in a template survive.
- A conditional block `{?name}...{/name}` already keeps its body only when the value is present — the existing drop-if-empty mechanism.

### Problem

An unknown or misspelled variable ships to the agent as literal placeholder text, and an inline empty variable leaves dangling prose (`See the plan at .`). The `{?name}...{/name}` pattern that would prevent the second case is undiscoverable, so authors do not use it.

### Decision

Keep `render_prompt` and `ALLOWED_PROMPT_VARS` unchanged (the fixed security core). Make `{?name}...{/name}` the sanctioned pattern for optional variables and document it, and add a preflight/validate-time lint that warns (never fatal) when a role file references a `{name}` outside the allowlist. An auto-append "context references" model was considered and deferred. Full analysis in the refinement ADR.

### Scope

In scope:

- validate-time lint for unknown `{name}` tokens in role files;
- documenting `{?name}...{/name}` and adopting it in packaged prompts;
- the lint doubling as the docs↔`ALLOWED_PROMPT_VARS` anti-drift guard shared with task 05.

Out of scope:

- widening `ALLOWED_PROMPT_VARS`;
- an auto-append reference model;
- injecting content (non-path) variables;
- making the lint a fatal error.

### Acceptance criteria

- A role file referencing a variable outside the allowlist produces a preflight warning that names the file and token.
- The lint is a warning, not a failure: a verbatim `{name}` render (code/JSON braces) still passes.
- The optional-variable pattern `{?name}...{/name}` is documented where role authors will see it, and the packaged prompts use it for optional variables.
