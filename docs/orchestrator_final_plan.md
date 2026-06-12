# Final plan for the multi-agent Git orchestrator

Date: 2026-06-11

## 1. Goal

Refine the architecture of the console Git orchestrator so that it can execute task stages through two interchangeable CLI agents:

- OpenAI Codex CLI;
- Anthropic Claude Code CLI.

The orchestrator remains the owner of the process: it accepts the task, manages the Git branch, selects an agent for each stage, runs checks, persists state, performs commit/push, and creates a Pull Request when needed.

The agents work only with the repository contents. They do not manage the Git task lifecycle and do not make decisions about publishing the result.

## 2. Scope of the first version

The first version includes:

- running Codex and Claude Code as child CLI processes;
- selecting a primary and fallback provider separately for each stage;
- overriding the route at the task level through an allowlist;
- a unified format for provider input, result, and errors;
- a sequential pipeline `refinement -> planning -> implementation -> testing -> review -> fixing` (the `refinement` stage is skipped for tasks that are already complete, see §5);
- processing at most one task at a time (a single active task; the rest wait in `pending`, see §8.2);
- terminal cleanup after a finished task: when safe, the Git Manager switches the working copy back to `repo.base_branch` before any next task can start (§8.3);
- optional auto mode for taking the next pending task after terminal cleanup; it is controlled by `orchestrator.auto_mode.enabled` and is off by default (§8.3, §11);
- bounded retry/fix cycles;
- an append-only ledger of completed tasks (see §10);
- optional, flag-gated task decomposition: when enabled and the planning agent judges the task very large, it produces an ordered list of subtasks executed sequentially on the single task branch, yielding one Pull Request (off by default, see §5.1);
- a final plain-language summary stage that explains what was done / how it works / how it integrates / why, and becomes the Pull Request body (see §5.2);
- recovery after a restart;
- auditing of runs, commands, and artifacts;
- publishing the result only after successful checks.

The first version does not include:

- the Claude Agent SDK or the OpenAI API;
- concurrent operation of several agents in a single working copy;
- automatic merging of the Pull Request;
- a Web UI;
- dynamic agent selection by the model;
- automatic installation or authorization of the CLIs;
- transferring an active vendor session between Codex and Claude Code;
- human-in-the-loop: answering agents' clarifying questions and approving specific actions (deferred; see §18.2);
- per-task reasoning/complexity levels (deferred; see §18.2);
- Telegram integration for results and prompts (deferred; see §18.2).

## 3. Core principles

1. The orchestrator core must not know the syntax of any specific CLI.
2. Each stage is an independent run and receives all the context it needs through files and a prompt.
3. Fallback is allowed only for infrastructure errors of a provider.
4. Implementation errors, test failures, and review findings are handled through the `fixing` stage.
5. Commit, push, and PR creation are performed only by the orchestrator.
6. Every per-task automatic cycle has a configurable limit, and the total number of fix iterations per task is bounded by a single global cap (`agents.max_total_fix_iterations`); when it is exhausted the task stops in `manual_action_required` with a recorded failure report (see §8.1). The outer queue loop is separate, opt-in, and controlled by `orchestrator.auto_mode.enabled` (§8.3).
7. All routing and provider-switching decisions are persisted to the state store and the logs.
8. The next development stage begins only after the DoD of the previous one has been met.

## 4. Components

```text
Task Source
    |
    v
Orchestrator Core
    |-- Task Parser
    |-- State Machine
    |-- Agent Router
    |     |-- CodexProvider
    |     `-- ClaudeCodeProvider
    |-- Git Manager
    |-- Check Runner
    |-- Artifact Store
    `-- State Store
```

### 4.1. Orchestrator Core

Manages the sequence of stages, attempt limits, state transitions, and publishing conditions. The Core invokes only the `AgentProvider` interface and does not build provider-specific commands.

### 4.2. Agent Router

For each stage it determines:

- the primary provider;
- the fallback provider;
- the route source: global config or task override;
- provider availability per the allowlist;
- the next permitted attempt.

### 4.3. AgentProvider

A common contract for Codex and Claude Code:

```text
AgentProvider
  id
  preflight() -> ProviderHealth
  run(AgentRunRequest) -> AgentRunResult
```

`ProviderHealth` contains:

- presence of the executable;
- the detected version;
- the authorization status;
- support for the required CLI capabilities;
- a diagnostic message without secrets.

`AgentRunRequest` contains:

- `task_id`;
- `stage`;
- `working_directory`;
- `prompt`;
- paths to the task, plan, diff, check, and review artifacts;
- `permission_profile`;
- `timeout_seconds`;
- `attempt`;
- `output_schema`;
- a provider-specific model and safe additional parameters.

`AgentRunResult` contains:

- `status`: `succeeded` or `failed`;
- `provider`;
- `stage`;
- `attempt`;
- `exit_code`;
- `started_at` and `finished_at`;
- `final_message`;
- `structured_output`;
- `usage`, if the CLI reported it;
- `session_id`, for auditing only;
- paths to stdout/stderr/raw event log;
- the normalized error.

### 4.4. Provider adapters

`CodexProvider` is responsible for:

- preflight through the available Codex CLI commands;
- building a safe `codex exec` invocation;
- JSONL/structured output;
- mapping the sandbox and permission profile;
- normalizing the exit code and events.

`ClaudeCodeProvider` is responsible for:

- preflight through the available Claude Code CLI commands;
- building a safe `claude -p` invocation;
- `stream-json`/structured output;
- mapping the permission mode, allowed/denied tools, and sandbox;
- normalizing the exit code and events.

Provider adapters do not perform fallback and do not modify the state machine.

### 4.5. Git Manager

Owns all interaction with the target repository: branch creation, **scoped staging** (an explicit pathspec, never `git add .`, §21.1), commit, push, and PR creation. It also owns the git footprint mode (§21) — appending to `.git/info/exclude` in `exclude_local` mode and making the separate audit commit in `commit` mode. Only the Git Manager commits/pushes/creates PRs; agents never do.

## 5. Stages and routing

The supported stages are:

```text
refinement
planning
implementation
testing
review
fixing
summary
publishing
```

`testing` is performed by the Check Runner, and `publishing` is performed by the Git Manager. For the agent stages, the default route is:

| Stage | Primary | Fallback |
|---|---|---|
| `refinement` | Claude Code | Codex |
| `planning` | Claude Code | Codex |
| `implementation` | Claude Code | Codex |
| `review` | Codex | Claude Code |
| `fixing` | Claude Code | Codex |
| `summary` | Claude Code | Codex |

The `refinement` stage runs first and enriches the task before planning: it clarifies the description, fixes the scope (in/out), derives acceptance criteria, lists the affected modules and the assumptions/risks, and writes the result to `task.enriched.md` (§10). It performs no code edits.

`refinement` is **skipped** (the Core goes straight from `preparing` to `planning`) when the task is already sufficiently complete — that is, it contains both a description and acceptance criteria (plus an explicit scope/constraints), or it is explicitly flagged `refined: true` in the front matter. The skip decision is deterministic, made by the Core, and recorded in the state store and the audit. In v1 refinement is **autonomous**: it does not ask the human clarifying questions (still deferred, §18.2); it proceeds with documented assumptions, and only when the scope cannot be determined at all does the task move to `manual_action_required`.

A route may be overridden in a task only:

- for known stages;
- by a provider from `agents.allowed`;
- without changing the security policy, command path, or credentials;
- after the task has been fully validated and before the branch is created.

Example YAML front matter:

```yaml
---
id: task-001
refined: false          # set true to skip the refinement stage for an already-complete task
decompose: false        # tri-state: true forces / false disables decomposition; omit = config default (§5.1)
agents:
  refinement: claude
  planning: claude
  implementation: codex
  review: claude
  fixing: codex
---
```

For a JSON task, an `agents` object with the same keys is used.

### 5.1. Decomposition (flag-gated)

Decomposition lets the orchestrator split a very large task into an ordered list of subtasks that are executed sequentially. It is **off by default** and controlled by `agents.decomposition.enabled` (§11), overridable per task via the tri-state `decompose` front-matter field (`true` forces it, `false` disables it, omitted = the global config value). The effective flag cannot change `max_subtasks`, routes, or any security setting.

**Where it runs.** Decomposition is a sub-phase of `planning`, not a separate stage. When the gate is on, the planning prompt first asks the agent to assess task size; the agent returns, in its structured output, either a single plan or an ordered subtask list. `refinement` always runs before planning, so subtasks inherit the enriched task.

**Deterministic acceptance (mirrors the refinement-skip rule).** The split is proposed by the agent but accepted by the Core only when all hold:

- the gate is on for this task;
- the agent recommends a split and returns `2 <= n <= agents.decomposition.max_subtasks` subtasks;
- every subtask declares `order`, `title`, `slug`, `acceptance_criteria`, and `depends_on` that references only earlier orders (linear ordering only; no forward or cyclic dependency).

If any condition fails, the Core rejects the split and runs the task as a single unit (`planning -> implementation`). The accept/reject decision, `n`, and the reason are persisted to the state store and the audit. Decomposition is autonomous in v1 (no interactive confirmation); planning performs no code edits.

**Execution.** Subtasks run strictly sequentially within the single processing slot (§8.2). For each subtask in order, the pipeline runs `implementation -> testing -> review -> fixing`; on review success the Git Manager makes one local commit on the single task branch `agent/<task-id>-<slug>` (`commit_per_subtask`, §11). After the last subtask the normal `ready_to_publish -> committing -> pushing -> creating_pr` produces a single push and a single Pull Request for the whole parent task. Subtask specifications are artifacts under `logs/<task-id>/` (§10) and are never written into the target repository.

**Loop budget.** Per-subtask `stage_attempts` and `fix_cycles` reset when the Core advances to the next subtask; the global per-task `fix_iterations` does not reset and remains bounded by `agents.max_total_fix_iterations` (§8.1). A decomposed task therefore cannot evade the hard stop by splitting: exhausting the global budget at any subtask moves the whole parent to `manual_action_required`, preserving the commits of the subtasks already completed (§7.4).

### 5.2. Final summary (handoff)

After the review passes and before publishing, a final **`summary` stage** produces a plain-language explanation of the change for a human reviewer. It is agent-driven and performs no code edits. It answers four questions:

1. **What** was done (in plain language, not a diff dump);
2. **How** it works;
3. **How** it integrates into the overall system (which components / flows it touches);
4. **Why** — the intent and the rationale behind the approach.

The stage reads the task, the enriched specification, the plan, the final `git diff`, and the review findings, and writes `summary.md` (human-readable) plus `summary.json` (the four fields, machine-readable) under `logs/<task-id>/` (§10). For a decomposed task (§5.1) it produces one summary for the whole parent (optionally with a short per-subtask section), matching the single PR.

The summary is a **handoff artifact, not a quality gate**: an infrastructure failure falls back to the other provider (§7.2), and if no provider can produce it the Core writes a minimal deterministic summary from the task and diff and proceeds — a reviewed, passing change is never blocked by the prose step. The `publishing` stage uses `summary.md` as the Pull Request body, so reviewers read the explanation directly on the PR (§10, §21).

## 6. Context between stages

The vendor session is not the source of truth. Each new run receives its context from artifacts:

- the original task;
- the normalized task manifest;
- the enriched task specification produced by `refinement`, when that stage ran;
- the approved plan;
- the current `git diff`;
- the results of tests and linters;
- the findings of the previous review;
- a description of the previous error or partially completed attempt;
- when the task was decomposed (§5.1): the active subtask specification and the cumulative diff of the subtasks already committed.

This makes it possible to execute the next stage with a different provider and to recover the pipeline after a restart.

## 7. Fallback and errors

### 7.1. Error classes

`ProviderError` is normalized into one of the following classes:

- `binary_not_found`;
- `unsupported_version`;
- `authentication_failed`;
- `authorization_failed`;
- `rate_limited`;
- `network_unavailable`;
- `provider_unavailable`;
- `timeout`;
- `process_crashed`;
- `invalid_output`;
- `permission_denied`;
- `configuration_error`;
- `task_failure`.

### 7.2. Errors that permit fallback

Fallback is allowed for:

- `binary_not_found`;
- `unsupported_version`;
- `authentication_failed`;
- `rate_limited`;
- `network_unavailable`;
- `provider_unavailable`;
- `timeout`;
- `process_crashed`;
- `invalid_output`.

`authorization_failed` and `permission_denied` permit fallback only when the denial applies to a specific provider and the fallback provider operates in the same or a stricter permission profile. Relaxing the security policy is prohibited.

### 7.3. Errors without fallback

Fallback does not apply to:

- failed tests or linters;
- code review findings;
- incomplete fulfillment of the task requirements despite a successful CLI exit;
- Git errors;
- an invalid task or configuration;
- exhaustion of the fix cycles;
- a violation of the security policy.

Such cases are routed to `fixing`, `failed`, or `manual_action_required` depending on the state machine:

- failed tests/linters and code review findings -> `fixing`, bounded by the fix-loop limits (§8.1);
- exhaustion of a fix loop or of the global fix-iteration budget -> `manual_action_required`, with a recorded failure report (§9, §10);
- an invalid task/configuration, a security violation, or an unrecoverable Git state -> `failed`.

### 7.4. Partial changes

Before an agent run the following are saved:

- the current commit SHA;
- `git status --porcelain`;
- a checksum of the diff;
- the list of existing artifacts.

If the primary provider failed with an infrastructure error after files had been changed:

1. The orchestrator does not roll back the changes automatically.
2. A post-attempt snapshot and diff are saved.
3. The fallback receives the current diff and a message about the partially completed attempt.
4. The fallback result goes through the full set of checks.
5. Both runs remain in the audit.

When a task is decomposed (§5.1), commits from subtasks `1..k-1` that already passed their checks are preserved on the branch and are never rolled back automatically; only the in-flight subtask `k`'s uncommitted changes are subject to the partial-change rules above.

## 8. State machine

Task statuses:

```text
new
validated
preparing
refining
planning
implementing
testing
reviewing
fixing
summarizing
ready_to_publish
committing
pushing
creating_pr
done
failed
manual_action_required
```

Main transitions:

```text
new -> validated -> preparing -> refining -> planning -> implementing
new -> failed                    (rejected by the §19 validation gate; quarantined to tasks/rejected/)
preparing -> planning            (refinement skipped for an already-complete task)
implementing -> testing
testing(success) -> reviewing
testing(failure) -> fixing -> testing
reviewing(success) -> summarizing -> ready_to_publish
reviewing(success, decomposed, subtask k<n) -> implementing   (commit subtask k, advance to k+1)
reviewing(success, decomposed, subtask k=n) -> summarizing -> ready_to_publish
reviewing(blocking findings) -> fixing -> testing
ready_to_publish -> committing -> pushing -> creating_pr -> done
any active stage -> failed
any active stage -> manual_action_required
```

Conditions:

- a transition is performed transactionally;
- a re-run does not create a second commit, push, or PR;
- after a restart the unfinished stage is resumed or its result is safely reconciled;
- publishing is allowed only when checks succeed and there are no blocking findings;
- the number of agent attempts and fix cycles is bounded by the configuration (see §8.1);
- a decomposed task (§5.1) repeats the `implementing -> testing -> reviewing` cycle once per subtask in order; no new statuses are introduced — the active subtask index `k` of `n` is carried in the state store (`active_subtask`), and `ready_to_publish` is reached only after the last subtask passes review.

### 8.1. Loop control and the stuck condition

Both fix loops are driven by the deterministic Orchestrator Core (there is no autonomous supervisor agent in v1, see §18.1):

- the test-driven loop `testing(failure) -> fixing -> testing`;
- the review-driven loop `reviewing(blocking findings) -> fixing -> testing -> reviewing`.

Three counters are persisted (§9):

- `stage_attempts` — attempts of a single stage run, including provider fallback within that stage; bounded by `agents.max_stage_attempts`. This is independent of the fix loops.
- `fix_cycles` — the length of the current consecutive fix loop, counted separately for the test-driven and the review-driven loop; each is bounded by `agents.max_fix_cycles`.
- `fix_iterations` — a single global per-task counter, incremented on every entry into `fixing` regardless of which loop triggered it; bounded by `agents.max_total_fix_iterations`.

The task stops as **stuck** and transitions to `manual_action_required` as soon as either:

- a single fix loop reaches `max_fix_cycles` without resolving its trigger; or
- the global `fix_iterations` reaches `max_total_fix_iterations`.

The global cap is the hard stop: it guarantees termination even if the review-driven loop keeps producing new blocking findings on every pass (no infinite ping-pong between `reviewing` and `fixing`). On the stuck transition the Core writes a failure report (§9, §10) that records why the task is stuck and the unresolved problems. `failed` is reserved for unrecoverable errors (invalid task/config, security violation, inconsistent Git state, no provider available) and is not used for an exhausted fix budget. A task rejected by the §19 validation gate is also terminal `failed`, quarantined to `tasks/rejected/`, and never receives a branch.

When a task is decomposed (§5.1), `stage_attempts` and both `fix_cycles` are scoped to the active subtask and reset when the Core advances to the next subtask; the global `fix_iterations` is **not** reset and accumulates across all subtasks, remaining bounded by `agents.max_total_fix_iterations`. A counter `subtasks_completed` (`k`) is persisted for observability. Reaching `max_total_fix_iterations` at any subtask stops the whole parent in `manual_action_required` even if later subtasks were never started.

### 8.2. Single active task

The first version processes **at most one task at a time**. The Core holds a single processing slot:

- a task may be in an active (non-terminal) status only while it owns the slot;
- additional tasks remain in `pending` until the active task reaches a terminal status (`done`, `failed`, or `manual_action_required`);
- the `watch` loop may pick a pending task only when the slot is free and terminal cleanup has returned the working copy to `repo.base_branch` (§8.3).

This keeps a single working copy consistent (concurrent execution and `git worktree` are deferred to v2). Subtasks of a decomposed task (§5.1) execute strictly sequentially within this single slot; they never run in parallel and do not introduce additional active tasks or worktrees. On restart, recovery resumes the one active task; finding more than one task in an active status is an inconsistent state and is routed to `manual_action_required` (§13).

### 8.3. Terminal cleanup and auto mode

When the pipeline reaches a terminal outcome, the Core finishes terminal handling before releasing the processing slot: final artifacts are registered, and the Git Manager performs **terminal cleanup** by safely switching the target repository back to `repo.base_branch`. The completed-tasks ledger is appended exactly once, and any final notification/reporting hook runs, after the final terminal outcome and cleanup state are known.

Terminal cleanup is mandatory when it is safe. The Git Manager must not discard uncommitted changes, remove partial work, or hide an ambiguous branch state. If the checkout back to `repo.base_branch` cannot be proven safe — for example because the working tree is dirty in a way not accounted for by the terminal outcome, the expected task branch cannot be reconciled, or the base branch checkout would overwrite files — the Core does not start another task and records the final outcome as `manual_action_required`.

`orchestrator.auto_mode.enabled` controls only whether the orchestrator automatically picks the next pending task after terminal cleanup:

- `false` (default): process/resume one task, perform terminal cleanup, then leave any further pending tasks untouched for the operator to start explicitly.
- `true`: after successful terminal cleanup and slot release, pick the next pending task and run it through the same single-task pipeline.

Auto mode does not change routing, security, stage attempts, fix-loop budgets, or Git ownership. It never processes more than one task at a time, and any `manual_action_required` task blocks automatic continuation until an operator resolves it.

## 9. State Store

SQLite remains sufficient for the first version. In addition to `tasks`, the following entities are needed:

```text
tasks
stage_runs
provider_attempts
check_runs
artifacts
publish_operations
subtasks
```

At a minimum the following are stored:

- the identifiers of the task, stage, and attempt;
- the selected primary/fallback and the provider actually used;
- the status and error class;
- timestamps and the exit code;
- the commit SHA before and after the stage;
- paths to artifacts;
- the fingerprint of the commit/push/PR operation;
- the terminal cleanup outcome: target base branch, whether checkout back to `repo.base_branch` completed, timestamps, and the last cleanup error if any (§8.3, §13);
- the `stage_attempts`, per-loop `fix_cycles`, and global `fix_iterations` counters (§8.1);
- a reference to the failure report artifact when the task ends in `manual_action_required` (§10);
- whether the `refinement` stage ran or was skipped, with the skip reason (§5);
- for decomposition (§5.1): on the `tasks` row whether it was enabled/accepted with the reason, the subtask count `n`, the `active_subtask` index `k`, and `subtasks_completed`; and one `subtasks` row per subtask with its `order`, `slug`, `title`, `status`, `depends_on`, `commit_sha` (the idempotency marker, null until committed), and artifact path;
- the validation outcome on the `tasks` row (`validation_passed`) and, on rejection, the failing `validation_reason` (§19).

Secrets, access tokens, and the full process environment are not stored in SQLite.

A ledger of finished tasks is kept as an append-only file outside SQLite (`logs/completed.jsonl`, §10); SQLite remains the authoritative state, and the ledger is a convenience index for tracking what has been done.

## 10. Artifacts and logs

```text
logs/
  completed.jsonl
  <task-id>/
    task.normalized.json
    validation_report.json
    task.enriched.md
    plan.md
    current.diff
    summary.md
    summary.json
    failure_report.json
    stuck.md
    subtasks/
      index.json
      01-<slug>.md
      02-<slug>.md
    checks/
      <run-id>.log
    review/
      findings.json
      summary.md
    stages/
      <stage>/
        <attempt>-<provider>/          # single-task run
        sub-<NN>/<attempt>-<provider>/ # one set per subtask when decomposed (§5.1)
          request.json
          stdout.log
          stderr.log
          events.jsonl
          result.json
          before.diff
          after.diff
    publish/
      commit.json
      push.json
      pull-request.json
      terminal-cleanup.json
```

Rules:

- all paths are relative to the task artifact directory;
- logs are not overwritten;
- the request artifact stores a redacted representation of the run;
- the machine-readable result is separated from the human-readable summary;
- artifacts are registered in SQLite with a checksum;
- `publish/terminal-cleanup.json` records the checkout target (`repo.base_branch`), outcome, timestamps, and any safe-check failure that prevented auto mode from continuing (§8.3);
- when a task stops in `manual_action_required` (§8.1), the Core writes `failure_report.json` (machine-readable) and `stuck.md` (human-readable) that summarize: which loop and which limit was exhausted, the values of all loop counters, the last failing check output, the last blocking review findings, and the final diff at the point of giving up;
- on every terminal transition (`done`, `failed`, `manual_action_required`) the Core appends one record to `logs/completed.jsonl` after the terminal cleanup outcome is known (append-only, never rewritten): task id, title, branch, PR URL if any, final status, `fix_iterations`, terminal cleanup status, the finished-at timestamp, and a link to `failure_report.json` when the task is stuck;
- `refinement`, when it runs, writes `task.enriched.md`; the original task file is never overwritten;
- when a task is decomposed (§5.1), the Core writes `subtasks/index.json` (ordered list with `order`, `slug`, `title`, `depends_on`, `status`, `commit_sha`) and one `NN-<slug>.md` spec per subtask at the end of planning; `index.json` is updated transactionally as each subtask is committed, the per-subtask `.md` files are never overwritten, and all subtask artifacts live only under `logs/<task-id>/` (never in the target repository);
- for a decomposed task the `failure_report.json` / `stuck.md` and the `completed.jsonl` record additionally carry the failing subtask index `k` of `n`, the count of already-committed subtasks (with their SHAs), and `decomposed` / `subtask_count` / `subtasks_completed`;
- the §19 validation gate writes `validation_report.json` for every task (pass or reject); on a reject this is the only artifact, no `stages/` directory is created, the file is moved to `tasks/rejected/`, and the `completed.jsonl` record carries `final_status: failed` plus the `validation_reason`;
- the `summary` stage (§5.2) writes `summary.md` and `summary.json`; the `publishing` stage uses `summary.md` as the Pull Request body, and the `completed.jsonl` record carries a one-line gist with a pointer to it. Like all artifacts these live under `logs/<task-id>/` and are not committed into the target repository except in the `in_repo` + `commit` audit footprint (§21); the PR description carries the summary regardless of footprint mode.

## 11. Configuration

Target structure:

```yaml
orchestrator:
  auto_mode:
    enabled: false

repo:
  url: "git@github.com:OWNER/REPO.git"
  local_path: "./workspace/repo"
  base_branch: "main"
  branch_prefix: "agent"

agents:
  allowed:
    - claude
    - codex

  max_stage_attempts: 2
  max_fix_cycles: 3
  max_total_fix_iterations: 5

  decomposition:                # optional task decomposition (§5.1); OFF by default
    enabled: false
    max_subtasks: 8             # a split with n > this (or n < 2) is rejected; runs as a single task
    min_size_signal: "large"    # advisory threshold passed to the planning prompt
    commit_per_subtask: true    # one local commit per subtask on the single task branch

  routing:
    refinement:
      primary: claude
      fallback: codex
    planning:
      primary: claude
      fallback: codex
    implementation:
      primary: claude
      fallback: codex
    review:
      primary: codex
      fallback: claude
    fixing:
      primary: claude
      fallback: codex
    summary:
      primary: claude
      fallback: codex

  providers:
    claude:
      command: "claude"
      model: ""
      timeout_seconds: 1800
      max_turns: 50
      max_budget_usd: null
      permission_profile: "workspace-write"
      extra_args: []
    codex:
      command: "codex"
      model: ""
      timeout_seconds: 1800
      sandbox: "workspace-write"
      permission_profile: "workspace-write"
      extra_args: []

security:
  strict_isolation: true
  allowed_environment:
    - "PATH"
    - "HOME"
    - "USERPROFILE"
    - "CODEX_HOME"
    - "CLAUDE_CONFIG_DIR"
  denied_read_paths:
    - ".env"
    - "secrets/**"
  denied_commands:
    - "git commit"
    - "git push"
    - "gh pr create"

validation:                       # input hardening gate (§19); rejects broken tasks before any branch
  max_task_bytes: 262144
  max_task_lines: 5000
  max_line_bytes: 8192
  max_control_ratio: 0.01
  required_fields: ["id", "title"]
  reject_unknown_fields: true
  quarantine_folder: "./tasks/rejected"

checks:
  commands:
    - "npm test"
    - "npm run lint"

git:
  create_pull_request: true
  pr_base: "main"
  footprint:                      # where orchestration/task artifacts live vs. the target repo (§21)
    location: external            # external | in_repo
    tracking: none                # none | exclude_local | commit
    external_root: "./"           # location=external: where tasks/ & logs/ live, OUTSIDE the clone
    audit_commit_message: "chore(orchestrator): audit trail for {task_id}"  # tracking=commit only
    audit_on_branch: task         # task | sibling   (tracking=commit only)
```

Configuration requirements:

- `orchestrator.auto_mode.enabled` defaults to `false` and must be a boolean;
- unknown route keys are treated as an error;
- the primary and fallback cannot reference a forbidden provider;
- a task override cannot change the provider command, extra args, or security;
- `extra_args` are validated against the provider allowlist and must not allow disabling the sandbox/permissions;
- a legacy Codex-only config is migrated to a Codex route for all agent stages with a warning;
- `max_total_fix_iterations` must be >= `max_fix_cycles`; it is the hard global cap that stops a task in `manual_action_required` (§8.1);
- `agents.decomposition.max_subtasks` must be >= 2; decomposition is off unless `agents.decomposition.enabled` is true, and the per-task `decompose` override flips only the gate (never `max_subtasks`, routes, or security);
- `git.footprint`: reject `location: external` with `tracking: exclude_local|commit`, and `location: in_repo` with `tracking: none`; when `location: external`, `external_root` must resolve outside `repo.local_path` (§21).

## 12. Security model

1. The working area is confined to a separate clone/worktree.
2. Agents are forbidden to commit, push, merge, and create PRs.
3. The orchestrator passes only allowlisted environment variables.
4. Secret files are excluded from reading and logging.
5. CLIs are launched without shell interpolation of user strings.
6. The task ID, branch name, and paths undergo strict normalization.
7. Options that bypass the sandbox/permissions are forbidden by the configuration validator.
8. With `strict_isolation: true`, an inability to enable the required isolation fails preflight with an error.
9. Git credentials and the agents' credentials are configured outside the orchestrator.
10. Staging in the target repo is a scoped explicit pathspec that excludes `tasks/`/`logs/`/`workspace/`; blanket `git add .`/`-A` is forbidden, so orchestration and task artifacts never enter a code commit. In audit-footprint mode only the orchestrator (never an agent) makes the separate artifact commit (§21).
11. The Pull Request and CI remain a mandatory control layer.

## 13. Recovery and idempotency

On startup the orchestrator:

1. Finds the active task (at most one, see §8.2); more than one task in an active status is an inconsistent state and is moved to `manual_action_required`.
2. Reconciles SQLite, the task files, the working branch, and the artifacts.
3. Checks whether the external process has finished and whether a valid result artifact exists.
4. Repeats only the unfinished idempotent operation.
5. For commit/push/PR it uses the saved fingerprint and checks the remote state.
6. If a task is already terminal but terminal cleanup was interrupted, performs the checkout back to `repo.base_branch` once when safe.
7. In an ambiguous state it moves the task to `manual_action_required`.

For a decomposed task (§5.1), recovery resumes at the `active_subtask = k` recorded in the state store. A subtask is treated as already done only when its `subtasks.commit_sha` is set and that commit exists on the branch; the Core never re-commits a subtask with a recorded SHA. An interrupted in-flight subtask (uncommitted changes, no SHA) is re-run from its spec, treating prior changes as a partial attempt (§7.4). An inconsistent subtask state (a recorded SHA missing from the branch, or more committed SHAs than `subtasks_completed`) moves the task to `manual_action_required`.

The following must not be done automatically:

- republishing an unknown commit;
- deleting partial changes;
- changing the provider route retroactively for a stage that has already started;
- continuing after an inconsistent branch state has been detected;
- picking the next pending task before terminal cleanup has returned the working copy to `repo.base_branch`.

## 14. Checks and acceptance

### Unit

- validation of the configuration and task overrides;
- route resolution and the allowlist;
- the command builder of both providers;
- parsing of structured output;
- error classification;
- state machine transitions;
- redaction and path normalization;
- retry/fallback limits, the global fix-iteration budget, and the stuck condition (§8.1);
- the `refinement` skip decision (already-complete task vs. needs enrichment);
- the single-active-task slot (a new task does not start while another is active);
- terminal cleanup and auto mode: `auto_mode.enabled` defaults to `false`, rejects non-booleans, leaves the next task pending when off, and starts the next task only after a successful checkout to `repo.base_branch` when on;
- the decomposition accept/reject decision (gate off; gate on and recommended; `n` out of range; a forward/cyclic dependency) and the per-subtask counter reset versus the global budget accumulation (§5.1, §8.1);
- the §19 validation gate: each Phase-A reason code, required/optional fields, duplicate-id detection, the injection-token scan, and the Phase-B complete/needs-enrichment classification;
- `init` (§20): an idempotent second run skips everything and exits 0, never overwrites `config.yaml`, `--dry-run` writes nothing, each `--git-mode` writes the matching `git.footprint` defaults, and the packaged templates are discoverable from an installed wheel;
- the git footprint (§21): scoped staging excludes `tasks/`/`logs/`/`workspace/`, the `.git/info/exclude` append is idempotent, the audit commit is made only by the orchestrator, and the validator rejects the illegal `location`/`tracking` pairings;
- the `summary` stage (§5.2): the handoff artifact is produced, and a provider failure falls back to a deterministic minimal summary without blocking publishing.

### Integration

Fake CLI executables are used for the scenarios:

- a successful run;
- a missing binary;
- a failed authorization;
- a rate limit;
- a timeout;
- a process crash;
- malformed output;
- an infrastructure error after files were changed;
- a successful fallback;
- denial of fallback for a quality failure.

### End-to-end

On a temporary Git repository, verify that:

- a vague task triggers `refinement` and produces `task.enriched.md`, while an already-complete task skips it;
- Claude Code performs planning and implementation;
- Codex performs review;
- failed checks trigger fixing;
- a successful result leads to a single commit, push, and PR;
- after a terminal task the working copy is checked out back to `repo.base_branch` before any next task starts;
- with `orchestrator.auto_mode.enabled: true`, two pending tasks are processed sequentially with a base-branch checkout between them; with it disabled, the second task remains pending;
- a restart does not duplicate publishing;
- the completed-tasks ledger gains exactly one record per terminal transition;
- a large task with decomposition enabled produces `n` subtasks, `n` sequential commits on one branch, and a single PR, and a restart mid-subtask resumes at `k` without duplicating a subtask commit (§5.1);
- a broken task is quarantined to `tasks/rejected/` as `failed`, writes `validation_report.json`, and never creates a branch or calls a provider (§19);
- in every git footprint mode the code commit contains no `tasks/`/`logs/`/`workspace/` paths; `exclude_local` adds them to `.git/info/exclude`; audit mode adds one orchestrator-made artifact commit (§21);
- a successful task produces `summary.md` (what / how / integration / why) which becomes the PR body (§5.2);
- exhaustion of a fix loop or of the global fix-iteration budget moves the task to `manual_action_required` and writes a failure report, while an unrecoverable error moves the task to `failed`.

## 15. Implementation stages

The stages are executed strictly in sequence:

1. [Contracts and configuration](implementation_stages/01_contracts_and_config.md)
2. [Provider layer and the Codex adapter](implementation_stages/02_provider_layer.md)
3. [Claude Code adapter](implementation_stages/03_claude_code_adapter.md)
4. [Routing and fallback](implementation_stages/04_routing_and_fallback.md)
5. [Pipeline and recovery](implementation_stages/05_pipeline_and_recovery.md)
6. [Security and observability](implementation_stages/06_security_and_observability.md)

Advancing to the next stage is allowed only after every item of the current stage's DoD has been documented as complete.

## 16. Final Definition of Done

The project is considered complete when:

- Codex and Claude Code are accessible only through the common `AgentProvider`;
- the Claude route for refinement/planning/implementation/fixing and the Codex route for review work by default;
- task-level overrides are limited by the allowlist;
- infrastructure fallback works and is fully audited;
- quality failures do not cause a provider switch;
- the state machine recovers after a controlled restart;
- the `refinement` stage enriches incomplete tasks and is skipped for already-complete ones;
- only one task is processed at a time, and completed tasks are recorded in the append-only ledger;
- terminal cleanup returns the working copy to `repo.base_branch` before another task starts, and auto mode is opt-in through `orchestrator.auto_mode.enabled`;
- flag-gated decomposition is off by default, accepts a split only under the deterministic rule, executes subtasks sequentially on one branch into a single PR, and the global fix-iteration budget bounds the whole decomposed task;
- the final `summary` stage produces a plain-language what / how / integration / why handoff that becomes the PR body and never blocks a reviewed change;
- commit, push, and PR are performed only by the orchestrator and are not duplicated;
- the security policy cannot be relaxed through a task or `extra_args`;
- the unit, integration, and end-to-end tests pass;
- the operations documentation describes the installation, preflight, authorization, and diagnostics of both CLIs.

## 17. Official reference materials

- [OpenAI Codex CLI Reference](https://developers.openai.com/codex/cli/reference)
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code Settings](https://code.claude.com/docs/en/settings)
- [Claude Code Security](https://code.claude.com/docs/en/security)
- [GitHub CLI: `gh pr create`](https://cli.github.com/manual/gh_pr_create)

## 18. Scope decisions and deferred work (v2)

This section records the design decisions and the ideas that are intentionally out of scope for the first version, so that nothing from the original requirements is silently dropped. The fuller designs for the deferred items live in [codex_git_orchestrator_architecture.md](codex_git_orchestrator_architecture.md), and the aggregated backlog is tracked in [backlog/product_backlog.md](backlog/product_backlog.md).

### 18.1. Design decisions

- **Deterministic Core instead of an LLM "supervisor".** The original idea of a supervisor agent that launches and manages the other agents is intentionally not adopted in the first version. Orchestration is handled by a deterministic Orchestrator Core plus the Agent Router (see §4.1–§4.2): predictability and auditability take priority over emergent autonomy. A supervisor-style planning layer may be revisited in v2 on top of this deterministic base.
- **Stateless stage runs cover "recreate agents per task".** The requirement to refresh agent state/context for each new task is satisfied structurally: every stage is an independent run whose context comes only from artifacts (see §3 and §6); there is no shared in-memory agent state to reset.

### 18.2. Deferred to v2

- **Human-in-the-loop: clarifying questions and action approval.** A mechanism for answering agents' clarifying questions and granting approval for specific (especially irreversible) actions. Designed in architecture.md §4.7. In v1, ambiguous or unsafe situations resolve to the `fixing` / `manual_action_required` states instead of an interactive prompt. The v1 `refinement` stage (§5) enriches tasks autonomously and does not introduce interactive clarification.
- **Reasoning / complexity levels per task.** Per-task `reasoning` and `complexity` fields that map to provider model flags and to limits (attempts, timeouts). Designed in architecture.md §4.10. In v1, the model and limits are set globally in the configuration.
- **Telegram integration.** Sending results and human-in-the-loop prompts to a Telegram bot. Designed in architecture.md §4.7. In v1, results are observed through logs and artifacts. It is important to have a separate Telegram chat for each project and repository.
- **Richer task parsing.** Beyond the per-stage routing override (§5), extracting additional fields such as contacts and free-form commands/hints from the task. Designed in architecture.md §4.1.
- **Parallel and graph decomposition.** Parallel subtask execution, per-subtask branches/worktrees, and inter-subtask dependency graphs beyond a linear order. v1 decomposition (§5.1) is strictly sequential on a single branch with linear ordering only.

These items are mirrored in [backlog/product_backlog.md](backlog/product_backlog.md) with the rest of the product backlog.

## 19. Input validation gate

The Task Parser and State Machine apply a **validation gate** on the `new -> validated` transition, before the branch is created and before any provider runs. It protects the pipeline from unclear or broken task input and guarantees that a structurally broken task never reaches an agent. The gate runs **before** the single processing slot is acquired (§8.2), so a flood of broken files cannot starve a valid task.

### 19.1. Two phases

- **Phase A — structural (hard reject, deterministic, no agent).** Any failure ends the task terminally in `failed` and moves the file to the quarantine folder `tasks/rejected/` (§19.4). The branch is never created.
- **Phase B — semantic completeness (never rejects).** Classifies the task as `complete` or `needs_enrichment` and feeds the deterministic refinement-skip decision (§5). Ambiguous-but-valid tasks follow the normal `refinement` path, not a reject.

### 19.2. Phase-A checks

Each failure maps to a machine-readable `validation.reason`; the first failure short-circuits:

- `file_too_large` — size exceeds `validation.max_task_bytes`;
- `not_utf8` — the file does not decode as strict UTF-8;
- `binary_or_control_chars` — contains NUL or disallowed control characters (only `\t \n \r` allowed), or the control-character share exceeds `validation.max_control_ratio`;
- `too_long` — lines exceed `validation.max_task_lines`, or the longest line exceeds `validation.max_line_bytes`;
- `frontmatter_missing` — no leading `---` block (`.md`) or not a JSON object (`.json`);
- `frontmatter_malformed` — YAML/JSON parse error, not a mapping, or duplicate keys;
- `unknown_top_level_field` — a frontmatter key outside the allowed set (fail-closed, like the unknown-route-key rule in §11);
- `missing_required_field` — a required field is absent or empty (§19.3);
- `invalid_field_type` — wrong type (e.g. `agents` not a mapping, `contacts` not a list of strings, `refined`/`decompose` not a boolean);
- `invalid_task_id` — `id` does not match `^[a-z0-9][a-z0-9._-]{0,63}$` (no whitespace, no `..`, no leading dot/separator);
- `duplicate_task_id` — `id` already exists in the `tasks` table or `completed.jsonl` (a recovery re-run of the same in-flight task is not a duplicate, §13);
- `invalid_route_override` — `agents.<stage>` names an unknown stage or a provider not in `agents.allowed`;
- `injection_suspected` — a frontmatter value carries argv-shaped tokens (§19.5).

### 19.3. Required vs optional fields

- **Required:** `id` (normalized, `invalid_task_id`), `title` (non-empty), and a non-empty **Description** section in the body.
- **Optional:** `refined` (bool, default false), `decompose` (tri-state, §5.1), `agents` (per-stage routing map, default empty), `contacts` (list of strings, default empty).
- Missing acceptance criteria / constraints is **not** a reject — it drives `needs_enrichment` (Phase B), i.e. `refinement` runs (§5). Any other top-level key is rejected (`unknown_top_level_field`), which keeps richer task parsing a v2 item (§18.2).

### 19.4. Outcome and recording

- A Phase-A failure is terminal `failed`; the file moves `processing/ -> tasks/rejected/`. `tasks/rejected/` is distinct from `tasks/failed/`: `failed/` holds tasks that died **during** processing (with a branch and a partial diff); `rejected/` holds tasks rejected **at the gate** (no branch, nothing to reconcile on restart). A single status (`failed`) plus the recorded `validation.reason` distinguishes the two — no new status is introduced.
- The gate writes `validation_report.json` for every task; on a reject it is the only artifact (no `stages/` directory) and the `completed.jsonl` record carries `final_status: failed` and the `validation_reason` (§10).

### 19.5. Injection defense

Task content reaches providers **only as file paths** in `AgentRunRequest` (`task_path`, `plan_path`, …); no task field is ever spliced into the CLI argv, the environment, the command path, the working paths, or any security setting — so task body text cannot become a CLI flag (this is the structural guarantee, see security.md). On top of that, the gate scans **frontmatter values** (not the body) for argv-shaped tokens — values beginning with `-`/`--`, or containing newlines, `;`, backticks, `$(`, `|`, or a path separator where a non-path field is expected — and rejects with `injection_suspected`. The body is never rejected for shell-like content (legitimate tasks contain snippets); the body is protected solely by the file-path-only contract. Normalization is reject-don't-sanitize: a value that changes under normalization (other than documented slug folding) is rejected.

### 19.6. Configuration

The `validation:` block (§11) sets `max_task_bytes`, `max_task_lines`, `max_line_bytes`, `max_control_ratio`, `required_fields`, `reject_unknown_fields`, and `quarantine_folder`.

## 20. Project initialization (`init`)

`wastech-orchestrator init [path]` scaffolds a ready-to-run project layout. It is operator-run (never agent-run) and **idempotent**: it never overwrites or deletes an existing file and reports what was created versus skipped.

### 20.1. CLI

```text
wastech-orchestrator init [path]
    [path]                 target directory (default: the current directory)
    --git-mode MODE        external | in_repo_exclude | in_repo_commit   (default: external)
                           seeds git.footprint.* in the generated config.yaml (§21)
    --force                re-copy existing template files (never deletes; never touches config.yaml)
    --dry-run              print the created/skipped plan; write nothing
    --quiet                suppress the per-file report (exit code only)
```

### 20.2. Created layout

```text
<path>/
  config.yaml                      # copied from the packaged config.example.yaml IF ABSENT
  tasks/
    pending/    .gitkeep
    processing/ .gitkeep
    done/       .gitkeep
    failed/     .gitkeep
    rejected/   .gitkeep           # quarantine for tasks rejected by the §19 gate
  logs/         .gitkeep
  workspace/    .gitkeep
  templates/                       # operator-editable copies
    task.md                        # task template (mirrors the §5 front matter + body)
    AGENTS.md                      # stub seeded into the TARGET repo (Codex)
    CLAUDE.md                      # stub seeded into the TARGET repo (Claude Code)
    skills/     .gitkeep
    prompts/
      refinement.md                # per-stage prompt templates with {variables}
      plan.md
      implement.md
      review.md
      fix.md
      summary.md
```

### 20.3. Template source and idempotency

- The source templates ship as **packaged data** under `src/wastech_orchestrator/templates/` and are read via `importlib.resources`, so `init` works from an installed wheel where no repo-root `templates/` exists.
- Per path: if it exists it is **skipped**, otherwise it is **created**. `--force` re-copies only files under `templates/` and never touches `config.yaml`. `--dry-run` writes nothing. The exit code is `0` on success (an all-skipped re-run is a successful no-op); non-zero only on an I/O / permission error or an invalid `--git-mode`.
- The generated `config.yaml` is the packaged `config.example.yaml`, which is kept in sync with §11.

## 21. Git footprint in the target repository

The orchestration and task files can be kept entirely out of the customer's repository, or committed as an audit trail. This is configured by two orthogonal axes under `git.footprint` (§11), yielding three supported modes (a tracked-`.gitignore` mode is intentionally not offered):

| Mode | `location` | `tracking` | Effect |
|---|---|---|---|
| **external** (default) | `external` | `none` | `tasks/` and `logs/` live outside the target clone (`external_root`); zero footprint, nothing to ignore. |
| **in-repo, excluded** | `in_repo` | `exclude_local` | Artifacts inside the clone, listed in `.git/info/exclude`; never committed, no change to tracked files. |
| **in-repo, audit** | `in_repo` | `commit` | Artifacts inside the clone, committed by the orchestrator as a separate audit trail. |

### 21.1. Scoped staging (all modes)

The Git Manager **never** runs `git add .` / `git add -A`. It stages only the agent's intended code paths via an explicit pathspec computed from the post-implementation diff after the output guardrails (`only_allowed_paths`, `no_unexpected_files`), plus belt-and-braces exclude pathspecs `:(exclude)tasks/ :(exclude)logs/ :(exclude)workspace/`. This guarantees, in every mode, that orchestration and task files never enter a code commit. This rule supersedes the illustrative `git add .` in the high-level overview (codex_git_orchestrator_architecture.md §4.2).

### 21.2. `exclude_local`

Before any staging, the Git Manager idempotently appends `tasks/`, `logs/`, `workspace/` to the clone's `.git/info/exclude` (append-only, de-duplicated). These entries are per-clone and are never committed, so the agent's `git status` stays clean and even a stray `git add .` would skip them.

### 21.3. `commit` (audit)

The **orchestrator** (Git Manager), never an agent, makes a separate audit commit of the artifact directories after the code commit (`git add -- tasks/ logs/`), with a fixed message (`audit_commit_message`). This preserves the invariant that agents never commit; the `denied_commands` blacklist targets agent processes, not the Git Manager. By default the audit commit is placed on the task branch (so the PR shows code and audit as separate commits); `audit_on_branch: sibling` keeps it off the code PR. The audit commit uses an operation fingerprint like the code commit, so a restart does not double-commit (§13).

### 21.4. Defaults and validation

- The default is `external` + `none`: `tasks/` and `logs/` live under `external_root`, outside `repo.local_path` (the §10 "paths relative to the task artifact directory" rule is unchanged — that directory is simply outside the clone).
- The validator rejects the illegal pairings `external` + `exclude_local|commit` and `in_repo` + `none`, and requires `external_root` to resolve outside `repo.local_path` (normalization, anti-traversal).
- Preflight edge: if the target repo already **tracks** a `tasks/`/`logs/` path (a name collision that `.git/info/exclude` cannot untrack), the task moves to `manual_action_required` rather than silently committing artifacts.
