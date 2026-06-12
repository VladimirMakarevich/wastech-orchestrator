# Phase 5 — Pipeline and recovery

**Goal:** assemble the deterministic Orchestrator Core that drives a task end to end — validation
gate → state machine → the full stage pipeline (with decomposition and loop control) → Git
publishing → restart recovery — plus the State Store, Check Runner, Git Manager, ledger, and the
`run`/`watch` CLI commands.

This is the largest phase. Work its blocks in order; each builds on the previous. Close each
sub-block's tests before moving on, even though the phase DoD is evaluated as a whole.

**Spec:** §5, §5.1, §5.2, §6, §8, §8.1, §8.2, §8.3, §9, §10, §13, §19, §21. **Rules:**
[architecture.md](../rules/architecture.md), [git-workflow.md](../rules/git-workflow.md),
[testing.md](../rules/testing.md).

**Prerequisites:** Phases 1–4 — config, both adapters, the Router. The Core calls **only** the
Router and the component interfaces below; it never builds CLI commands and is the **only** caller
that commits/pushes/PRs (via the Git Manager).

---

## Logical blocks

### 5.1 State Store (`state_store.py`, §9)
- SQLite with the entities: `tasks`, `stage_runs`, `provider_attempts`, `check_runs`, `artifacts`,
  `publish_operations`, `subtasks`.
- Persist at minimum (§9): task/stage/attempt ids; selected primary/fallback and the provider
  actually used; status + error class; timestamps + exit code; commit SHA before/after a stage;
  artifact paths; the commit/push/PR operation **fingerprint**; the `stage_attempts`, per-loop
  `fix_cycles`, and global `fix_iterations` counters; the `failure_report` reference when stuck;
  terminal cleanup state (target `repo.base_branch`, completed timestamp, last cleanup error);
  whether `refinement` ran or was skipped + the reason; decomposition fields on `tasks`
  (enabled/accepted + reason, `n`, `active_subtask` `k`, `subtasks_completed`) and one `subtasks`
  row each (`order`, `slug`, `title`, `status`, `depends_on`, `commit_sha` (null until committed),
  artifact path); `validation_passed` and, on reject, `validation_reason`.
- **No secrets / tokens / full env** in SQLite (§9, §12.6). Transitions are **transactional** (5.3).

### 5.2 Task Parser + Validation gate (`task/parser.py`, `task/validation_gate.py`, §19)
Runs on `new -> validated`, **before** the processing slot is acquired and before any branch/provider.
- **Parser:** read `.md` (leading `---` front matter + body) or `.json` (object); extract the §5/§19.3
  fields into the P1 `NormalizedTask`; write `task.normalized.json` (§10).
- **Phase A — structural, hard reject, deterministic, no agent** (§19.2): each failure → a
  machine-readable `validation.reason`, first failure short-circuits — `file_too_large`, `not_utf8`,
  `binary_or_control_chars`, `too_long`, `frontmatter_missing`, `frontmatter_malformed`,
  `unknown_top_level_field`, `missing_required_field`, `invalid_field_type`, `invalid_task_id`,
  `duplicate_task_id` (vs. `tasks` table + `completed.jsonl`; a recovery re-run of the same in-flight
  task is **not** a duplicate), `invalid_route_override`, `injection_suspected` (the frontmatter
  argv-shaped-token scan; the scanner module is shared with P6 — §19.5).
- **Phase B — semantic completeness, never rejects** (§19.1): classify `complete` vs.
  `needs_enrichment` to feed the deterministic refinement-skip decision (5.5). Missing acceptance
  criteria/constraints is **not** a reject.
- **Outcome** (§19.4): a Phase-A failure is terminal `failed`, the file moves `processing/ ->
  tasks/rejected/`, the gate writes `validation_report.json` (the only artifact; no `stages/` dir),
  and the ledger record carries `final_status: failed` + the `validation_reason`. **No branch is
  ever created** for a rejected task.

### 5.3 State machine (`core/state_machine.py`, §8)
- The statuses and transitions exactly as §8 (no invented statuses). The decomposition cycle reuses
  `implementing -> testing -> reviewing` per subtask; `active_subtask` carries `k` of `n`.
- Transitions are **transactional** (write status atomically); a re-run does **not** create a second
  commit/push/PR; publishing only from `ready_to_publish` when checks pass and no blocking findings.
- `failed` is reserved for unrecoverable errors (invalid task/config, security violation,
  inconsistent git/branch, no provider available); `manual_action_required` for an exhausted fix
  budget or an ambiguous/inconsistent recovery state.

### 5.4 Single active-task slot (`core/orchestrator.py`, §8.2)
- The Core holds **one** processing slot: a task is in an active (non-terminal) status only while it
  owns the slot; others wait in `pending`. `watch` may pick a pending task only when the slot is
  free **and** terminal cleanup has returned the target repo to `repo.base_branch`.
- Subtasks of a decomposed task run **strictly sequentially** within this one slot — never in
  parallel, no extra worktrees (that's v2).

### 5.5 The agent stages — Core pipeline (`core/orchestrator.py`)
Drive each agent stage through the Router (P4), feeding context **only as artifact file paths**
(§6): the original task, `task.normalized.json`, `task.enriched.md` (if refinement ran), the plan,
the current `git diff`, check/review results, the previous error/partial-attempt note, and (when
decomposed) the active subtask spec + cumulative committed diff.
- **`refinement`** (§5): runs first to enrich an incomplete task into `task.enriched.md` (no code
  edits). **Deterministically skipped** by the Core (`preparing -> planning`) when the task is
  already complete or flagged `refined: true`; the skip decision + reason are persisted/audited.
  Autonomous in v1 (documents assumptions, no interactive questions); only a fully indeterminable
  scope → `manual_action_required`.
- **`planning`** (§5): produces `plan.md` (no code edits). Hosts the decomposition sub-phase (5.6).
- **`implementation`**: applies code edits in the clone.
- **`testing`**: the Check Runner (5.7), not an agent.
- **`review`**: writes `review/findings.json` + `review/summary.md`; blocking findings → `fixing`.
- **`fixing`**: bounded by loop control (5.8).
- **`summary`** (§5.2): after review passes, a no-edit agent stage writing `summary.md` +
  `summary.json` (what / how / integration / why). It is **best-effort, not a quality gate** — an
  infra failure falls back (P4), and if no provider can produce it the Core writes a **deterministic
  minimal summary** from the task + diff and proceeds. `publishing` uses `summary.md` as the PR body.

### 5.6 Decomposition (`core/decomposition.py`, §5.1)
A flag-gated sub-phase of planning, **off by default** (`agents.decomposition.enabled`; per-task
tri-state `decompose` flips only the gate, never `max_subtasks`/routes/security).
- **Deterministic acceptance** — accept the agent's split **only** when: the gate is on; the agent
  recommends a split with `2 <= n <= max_subtasks`; every subtask declares `order`, `title`, `slug`,
  `acceptance_criteria`, and `depends_on` referencing **only earlier orders** (linear; no forward or
  cyclic dependency). Otherwise run as a single unit (`planning -> implementation`). Persist the
  accept/reject decision, `n`, and the reason.
- **Execution:** for each subtask in order, run `implementation -> testing -> review -> fixing`; on
  review success the Git Manager makes **one local commit** on the single branch
  `agent/<task-id>-<slug>` (`commit_per_subtask`). After the last subtask → the normal
  `ready_to_publish -> committing -> pushing -> creating_pr`: **one push, one PR** for the whole
  parent.
- **Artifacts:** `subtasks/index.json` (ordered, updated transactionally as each subtask commits) +
  one immutable `NN-<slug>.md` spec each, all under `logs/<task-id>/` — **never** in the target repo.

### 5.7 Check Runner (`check_runner.py`, §4.8 / testing stage)
- Run `checks.commands` (config, not hardcoded) through the P2 process runner (argv list, timeout,
  allowlisted env), writing each run to `checks/<run-id>.log` (§10).
- Failure → `fixing` (a quality error → **no** provider fallback). Success → `reviewing`.

### 5.8 Loop control (`core/loop_control.py`, §8.1)
Three persisted counters drive the two fix loops (deterministic Core, no supervisor agent):
- `stage_attempts` (from P4) — per stage incl. fallback, bounded by `max_stage_attempts`.
- `fix_cycles` — current consecutive fix-loop length, counted **separately** for the test-driven and
  review-driven loops; each bounded by `max_fix_cycles`.
- `fix_iterations` — a **single global per-task** counter, incremented on **every** entry into
  `fixing`, bounded by `max_total_fix_iterations` (the hard stop guaranteeing termination — no
  infinite reviewing↔fixing ping-pong).
- **Stuck →** `manual_action_required` as soon as a single fix loop hits `max_fix_cycles` **or** the
  global `fix_iterations` hits `max_total_fix_iterations`. On the stuck transition write the failure
  report (5.10).
- **Decomposed scoping:** `stage_attempts` and both `fix_cycles` reset when advancing to the next
  subtask; the global `fix_iterations` does **not** reset and accumulates across all subtasks.
  `subtasks_completed` is persisted. Hitting the global cap at any subtask stops the whole parent.

### 5.9 Git Manager (`git_manager.py`, §21, git-workflow.md)
The **only** component that commits/pushes/PRs; agents never do.
- **Branch:** `git fetch` → checkout `base_branch` → `pull` → create `agent/<task-id>-<slug>`.
- **Scoped staging (all modes, §21.1):** stage only the agent's intended code paths via an explicit
  pathspec computed from the post-implementation diff after the output guardrails
  (`only_allowed_paths`, `no_unexpected_files`), plus belt-and-braces
  `:(exclude)tasks/ :(exclude)logs/ :(exclude)workspace/`. **Never** `git add .`/`-A`.
- **Footprint modes (§21):** `external` (default, artifacts under `external_root` outside the
  clone — zero footprint); `in_repo` + `exclude_local` (idempotently append `tasks/`,`logs/`,
  `workspace/` to `.git/info/exclude` **before** staging); `in_repo` + `commit` (after the code
  commit, the **orchestrator** makes a separate audit commit `git add -- tasks/ logs/` with
  `audit_commit_message`, on the task branch or `sibling`). Preflight edge: if the repo already
  **tracks** a `tasks/`/`logs/` path → `manual_action_required` (§21.4).
- **Publish:** commit → push → `gh pr create` with `summary.md` as the body, **only** from
  `ready_to_publish`. No direct push to `base_branch`. Every operation uses an **idempotency
  fingerprint** + remote-state check so a restart never double-commits/pushes/PRs.
- **Terminal cleanup (§8.3):** after final artifacts are prepared and before the ledger append, safely checkout
  `repo.base_branch`. Do not discard uncommitted changes or hide an ambiguous branch state; if the
  checkout cannot be proven safe, write `publish/terminal-cleanup.json`, stop in
  `manual_action_required`, and do not pick another task.

### 5.10 Failure report + ledger (`ledger.py`, §10)
- On the stuck transition write `failure_report.json` (machine) + `stuck.md` (human): which loop and
  limit was exhausted, all counter values, the last failing check output, the last blocking review
  findings, and the final diff. For a decomposed task add the failing subtask `k` of `n` and the
  already-committed SHAs.
- On **every** terminal transition (`done`/`failed`/`manual_action_required`) append **one** record
  to `logs/completed.jsonl` (append-only, never rewritten): id, title, branch, PR URL if any, final
  status, `fix_iterations`, finished-at, a `failure_report` link when stuck, a summary gist, and the
  decomposition fields when applicable. Append the ledger only after the terminal cleanup outcome is
  known, so a cleanup failure produces one final `manual_action_required` record rather than a
  duplicate terminal record.

### 5.11 Recovery + idempotency (`core/recovery.py`, §13)
On startup: find the **one** active task (>1 active → `manual_action_required`); reconcile SQLite ↔
task files ↔ working branch ↔ artifacts; check whether the external process finished and a valid
result artifact exists; repeat **only** the unfinished idempotent operation; for commit/push/PR use
the stored fingerprint + remote check; if publishing already completed but terminal cleanup did not,
perform the checkout back to `repo.base_branch` once when safe; an ambiguous state →
`manual_action_required`.
- **Decomposed (§5.1):** resume at `active_subtask = k`; a subtask is done only when its
  `commit_sha` is set **and** that commit exists on the branch (never re-commit a recorded SHA); an
  interrupted in-flight subtask re-runs from its spec (prior changes = partial attempt, §7.4); an
  inconsistent subtask state → `manual_action_required`.
- **Never** auto: republish an unknown commit, delete partial changes, retroactively change a
  started stage's route, or continue past a detected inconsistent branch state.

### 5.12 CLI wiring (`cli.py`)
Replace the `run`/`watch` stubs:
- `run <task_file>` — process exactly one task through the Core pipeline, then perform terminal
  cleanup back to `repo.base_branch` when safe.
- `watch` — process/resume one pending task when the slot is free; after terminal cleanup, pick the
  next pending task only when `orchestrator.auto_mode.enabled: true`. With auto mode disabled
  (default), leave additional pending tasks untouched for an explicit later run.

---

## Tests

**Unit:** state-machine transitions (incl. the decomposed subtask cycle and skip/decompose
branches); the §19 gate (each Phase-A reason, required/optional fields, duplicate-id, the injection
scan, Phase-B classification); the refinement-skip decision; the single-active-task slot; loop
control (each limit, the global cap, per-subtask reset vs. global accumulation, the stuck
condition); the decomposition accept/reject decision (gate off; on+recommended; `n` out of range;
forward/cyclic dependency); the git footprint (scoped staging excludes the three dirs, idempotent
`.git/info/exclude` append, orchestrator-only audit commit, validator rejects illegal pairings); the
summary stage (handoff produced; provider failure → deterministic minimal summary without blocking);
terminal cleanup and auto mode (auto off leaves the next task pending; auto on starts the next task
only after successful checkout to `repo.base_branch`; unsafe cleanup blocks continuation).

**Integration (fake CLIs):** the full fix/fallback/snapshot scenarios now exercised through the Core.

**End-to-end (temporary git repo, §14):** vague task → `refinement`+`task.enriched.md`, complete
task skips it; Claude plans/implements, Codex reviews; failed checks → `fixing`; success → exactly
one commit/push/PR; terminal cleanup checks out `repo.base_branch`; restart doesn't duplicate
publishing or cleanup; ledger gains exactly one record per
terminal transition; a decomposed large task → `n` subtasks, `n` commits on one branch, one PR, and
a mid-subtask restart resumes at `k` without a duplicate commit; a broken task → `tasks/rejected/`
as `failed` with `validation_report.json` and no branch/provider; every footprint mode keeps
`tasks/`/`logs/`/`workspace/` out of the code commit; success → `summary.md` becomes the PR body;
with auto mode enabled, two pending tasks run sequentially with a base-branch checkout between them;
with auto mode disabled, the second task remains pending; exhausting a fix loop or the global budget
→ `manual_action_required` + failure report, while an unrecoverable error → `failed`.

## Definition of Done

- [ ] State Store persists all §9 entities/fields transactionally; no secrets in SQLite.
- [ ] Validation gate (§19) runs before the slot/branch; Phase-A rejects → `failed` +
      `tasks/rejected/` + `validation_report.json`, no branch; Phase-B feeds the skip decision.
- [ ] State machine implements §8 exactly; re-runs never duplicate commit/push/PR; single active slot.
- [ ] Full pipeline runs refinement (with deterministic skip) → planning → implementation → testing
      → review → fixing → summary → publishing, context passed only as file paths.
- [ ] Decomposition is off by default, accepted only under the deterministic rule, runs subtasks
      sequentially on one branch into one PR, with the global budget spanning all subtasks.
- [ ] Loop control bounds both fix loops + the global cap; the stuck condition writes the failure
      report and moves to `manual_action_required`.
- [ ] Git Manager: branch flow, scoped staging (never `git add .`/`-A`), all three footprint modes,
      orchestrator-only audit commit, idempotent publish via fingerprints, terminal cleanup back to
      `repo.base_branch`.
- [ ] Ledger gains exactly one append-only record per terminal transition; summary is the PR body.
- [ ] Recovery resumes the one active task / one subtask idempotently; ambiguity →
      `manual_action_required`; no forbidden automatic action.
- [ ] `run` and `watch` wired; `watch` respects `orchestrator.auto_mode.enabled`; the full §14 e2e
      suite green. `/run-checks` green.

## Not in this phase

- Cross-cutting security hardening + adversarial negative tests, audit-completeness verification, and
  the operations documentation — Phase 6.
- Human-in-the-loop, Telegram, reasoning/complexity levels, parallel/worktree decomposition — v2
  (spec §18.2; tracked in [../backlog/product_backlog.md](../backlog/product_backlog.md)).
