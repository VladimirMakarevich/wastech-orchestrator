# Changelog

All notable changes to **wastech-orchestrator** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project aims to follow
[Semantic Versioning](https://semver.org/) once it leaves the `0.x` pre-release line.

The persisted artifacts that outlive an upgrade carry their own independent schema versions
(see the spec's "Versioning and compatibility" section and
[docs/operations.md](docs/operations.md#upgrading-the-orchestrator)):

- `config.yaml` — top-level `schema_version` (current: **5**)
- `state.db` — SQLite `PRAGMA user_version` (current: **3**)
- registry (`registry.json`) — `version` (current: **1**, read forward-tolerantly)

The maintainer bumps the package version in `pyproject.toml` on release; `wastech-orchestrator
--version` reports the installed version.

## [Unreleased]

### Added
- **`install-templates` command — deliver the packaged `templates/` tree into an existing install**
  (backlog: `task_install_templates_command.md`). `worc install-templates` copies the packaged
  `templates/` tree (stage prompts, skills, agent stubs) beside the resolved `config.yaml`,
  **add-missing-only**: absent files are written, existing files are **skipped** to preserve operator
  edits. The install location is resolved like `upgrade-config`/`upgrade-docs` (`--config` →
  `./config.yaml` → the repo→config registry binding) and the command is fail-closed (exit 2) with an
  actionable hint when none resolves. This fills the gap that only `init` copied templates and the
  wizard-based `install` never did, so an install-based setup can now obtain the templates and an
  upgraded orchestrator can refresh them. Unlike `upgrade-docs` it **never removes** operator-added
  files (templates are operator-editable) and it never touches `config.yaml`/`prompts.overrides`
  (activating an edited template stays an operator decision). `--force` overwrites existing files
  (like `init --force`); `--dry-run` previews the add/skip(/overwrite) plan. `init` and
  `install-templates` now share one copy helper (`_copy_templates_tree`) so the two cannot drift.
- **`finalize` command — record + tidy a human-handled task** (backlog: `task_finalize_command.md`).
  `worc finalize <task-id> --as <done|failed|abandoned>` reconciles the orchestrator's bookkeeping for a
  task the operator resolved out-of-band — it **records and tidies only**, never running the pipeline or
  committing/pushing/PR-ing. It sets the declared terminal status, runs terminal cleanup (back to
  `base_branch`, **fail-closed** on an unaccounted-dirty tree), moves the task file, closes any waiting
  HITL prompt, and appends a `manual` ledger record (with `note`/`outcome`/`pr_url`). For `--as done`
  the PR URL is resolved by precedence (`--pr-url` > the URL a crashed run recorded in
  `publish_operations` > none, which warns + asks), and a **read-only** `gh pr view` merge check runs by
  default (`--no-verify-pr` to skip; warns+asks if not merged). `--as abandoned` is variant A
  (`manual_action_required` + an `outcome: abandoned` ledger marker). Refuses while the `watch` daemon is
  live, on a dirty tree, and on a re-finalize; the agent branch is kept unless `--delete-branch`. Flags:
  `--pr-url`, `--note`, `--delete-branch`, `--keep-branch`, `--no-verify-pr`, `--dry-run`, `--yes`.
- **`rerun` command — re-attempt a terminal task** (backlog: `task_rerun_command.md`). `worc rerun
  <task-id>` launches a supported re-attempt of a `failed`/`manual_action_required` task without
  hand-editing `state.db`/the ledger/git, in two modes on one command: **fresh** (default) resets the
  agent branch to the current `base_branch`, clears the per-attempt state (counters, decomposition,
  `subtasks`, `publish_operations`), archives the prior `logs/<id>/` to `logs/<id>/attempt-<N>/`, and
  drives the pipeline from the start; **`--continue`** keeps the branch and the work already done and
  re-enters at the stage the task failed (for an infra failure the operator fixed), reusing the crash
  recovery engine. The §19 duplicate-id gate is bypassed for exactly the named id; the command refuses
  while the `watch` daemon is live or another task is active, is **fail-closed** on an unaccounted-dirty
  tree, and refuses (fresh mode) on a prior remote branch / open PR unless `--force-reset-remote`. The
  ledger gains `attempt`/`rerun_of` linking each re-attempt to the prior record. Flags: `--continue`,
  `--force-reset-remote`, `--dry-run`, `--yes`.
- **Check discovery v2 — runtime, agent-assisted, human-checked** (post-test-run §1.2). Check
  discovery now runs inside the state machine at task start (`checks.discovery.run_at_task_start`,
  default on), so `auto` mode can resolve (and, when opted in with a cheap `checks.discovery.model`,
  agent-assist) when the task begins — install-time discovery stays a cache-warming option. The
  commands are re-resolved **only on real infrastructure proof** — a check *launch* failure (bounded
  to once per task), a changed config/CI fingerprint, or low-confidence detection — and **never**
  because a check reported failures (a quality failure routes to `fixing`, which must not rewrite its
  own command). Any **change to the set of check commands** is now a sensitive change: it is written
  to the resolved profile (`commands_signature` + `approved`/`approved_at`/`approved_interaction_id`,
  profile schema v2) and requires human approval on first use (fail-closed on denial, timeout, or no
  notifier); the first-ever resolution is auto-approved and recorded. In `auto` mode a configured
  command **pins** only the check it names and lets detection fill the rest — a pin is never silently
  replaced by a detected fallback. The discovery agent is told machine config (`[tool.*]`,
  package.json scripts, Makefile/lock files) takes precedence over prose; every proposal still goes
  through the same validate-argv → probe → human-approve funnel. New `checks.discovery.model`/
  `reasoning`/`agent_fallback`/`run_at_task_start`/`approve_command_changes` are surfaced in
  `config.example.yaml`. **`config.yaml` schema_version → 5** (adds the `skills:` block and the new
  discovery keys; `upgrade-config` adds them to an older config). New `CheckResolver.reresolve` /
  `ReResolveReason`.
- **Planning-selected repo skill references** (post-test-run §2.1/§2.2): at task start the orchestrator
  scans the **target repo's** `.claude/skills/*/SKILL.md` for name+description (a cheap, bounded,
  frontmatter-only inventory, mirroring the check inventory scan), surfaces the relevant ones to the
  `planning` stage, and planning emits the chosen skills in its structured output. The Core
  deterministically accepts only names the scan actually found (the "agent proposes, Core decides"
  rule — an unknown or gate-duplicating name is dropped and recorded in `plan.md`), then passes the
  chosen `SKILL.md` files to `implementation`/`fixing` as **read-only reference paths** via a new
  allowlisted `{skills_path}` prompt variable and a path-only `AgentRunRequest.skill_reference_paths`
  (both providers render an identical advisory "do not execute" footer — never the Claude-only Skill
  tool, which Codex lacks). Gate-duplicating skills (`run-checks`/`test`/`sync-docs`) are excluded by
  default. A deterministic, heading-level **de-duplication** (§2.2) records in `plan.md` when a chosen
  skill's section heading overlaps the operator's appended planning guidance, so the operator's text
  takes precedence and the agent is not handed the same instruction twice. New `skills:` config block
  (`scan_root`, `exclude`); new `core/skills.py`. Skill bodies are repo-controlled and only ever
  surfaced by path — never executed, never used to build argv/env.
- Add optional `pr_title` front-matter field to override the generated PR title (falls back to `title` when absent or blank).
- **Agent task-authoring docs (`worc/`)**: a compact, rule-first, copy-paste-oriented guide an AI
  agent can be pointed at to author valid, well-scoped task files without reading the whole `docs/`
  tree. Authored in [`docs/worc/`](docs/worc/README.md) (the task contract + hard validation rules, a
  "when to use what" decision guide, best practices, and two ready-to-adapt example tasks) and shipped
  as package data so it is available from an installed wheel. `init` writes it beside the generated
  `config.yaml` (a `worc/` folder); `install` writes it into the control workspace beside that
  workspace's `config.yaml` (`--reconfigure` refreshes it). Under an in-repo footprint the copy is
  git-ignored alongside the other runtime files (added to `append_runtime_excludes`), so it never
  pollutes the operator's `git status` or enters a code commit. The shipped example tasks are verified
  to pass the §19 `ValidationGate`, and a test keeps the authored and packaged copies in sync.
- **`upgrade-docs` command**: refreshes the installed `worc/` docs (beside `config.yaml`) to the
  current packaged version after a package upgrade. Because the docs are generated content with no
  operator edits to preserve, it is a straight overwrite — it writes missing/changed files, removes
  files no longer shipped, makes no backup, is idempotent (already-current → no-op), previews with
  `--dry-run`, and fails closed (exit 2 with an actionable hint) when no install location can be
  resolved, mirroring `upgrade-config`. Run it alongside `upgrade-config` after `pipx upgrade`. See
  [docs/operations.md](docs/operations.md#upgrading-the-orchestrator).
- **`upgrade-config` command**: brings an operator's `config.yaml` up to the current format after a
  package upgrade. It adds any keys the new version introduced (from the packaged template's defaults),
  **preserves every existing operator value**, stamps the current `schema_version`, and backs up the
  original to `config.yaml.bak-<UTC>` before writing atomically. Idempotent (an already-current config
  is left untouched, comments and all) and fail-closed (refuses an unparsable or newer-than-supported
  config; validates the result before writing). It does **not** itself bump any schema version. Caveat:
  when it rewrites the file it re-emits via YAML and drops inline comments (the simple migration path).
  `--dry-run` previews the additions. New module `config/upgrade.py` holds the pure add-missing-only
  merge. See [docs/operations.md](docs/operations.md#upgrading-the-orchestrator).
- **Stage-skip control**: operators can skip pipeline stages that add no value for a given workload,
  globally or per-task. Skippable stages are `planning`, `testing`, `review`, `fixing`, `summary`
  (`implementation`/`publishing` are never skippable; `refinement` keeps `refined: true`). Global:
  `agents.skip_stages: [...]`. Per-task: `enabled: false` on the existing `stages:` block (e.g.
  `stages: { testing: { enabled: false } }`). The effective skip set is the **union** of the two —
  a global skip cannot be re-enabled per task. The high-risk `review` skip (no agent gate before
  commit/PR) is fail-closed behind a new `agents.allow_review_skip` flag, required for a review skip
  from either source (`review_skip_not_allowed`). Behaviour: `planning` → stub `plan.md`, single
  unit; `testing` → straight to review (Check Runner bypassed); `review` → commit without an agent
  gate; `fixing` → first failure goes to `manual_action_required` (0 fix iterations); `summary` →
  stub summary. Every skip is logged at `WARNING`, recorded in `state.db`
  (`stage_runs.skipped`/`skip_reason`), and listed in a `## Pipeline stages skipped` PR-body section;
  a `review` skip under auto-merge emits a second prominent warning. Bumps `config.yaml`
  `schema_version` → **4** (new optional `agents.skip_stages`/`allow_review_skip`, safe defaults) and
  `state.db` `user_version` → **2** (in-place `ALTER TABLE` adds the two `stage_runs` columns; the
  per-stage skip intent and per-stage model/reasoning now round-trip through `task.normalized.json`
  for crash recovery). See [docs/task-authoring.md](docs/task-authoring.md#stages),
  [docs/operations.md](docs/operations.md), and
  [docs/backlog/stage_skip_control.md](docs/backlog/stage_skip_control.md).
- **Per-stage model/reasoning overrides**: a task can now tune `model` and `reasoning` per agent
  stage via a `stages:` front-matter block (e.g. Opus + `high` for `planning`/`review`, a lighter
  model + `low` for `implementation`/`fixing`). Each field resolves independently, most-specific
  first: `stages.<stage>.<field>` → task-wide `model`/`reasoning` → `agents.providers.<provider>`
  default. Keys are limited to the agent-routed stages (`refinement`, `planning`, `implementation`,
  `review`, `fixing`, `summary`); `testing`/`publishing` run no agent and are rejected fail-closed
  (`invalid_stage_override`), as are unknown sub-keys and non-mapping values. Per-stage values
  travel on `AgentRunRequest` only — they cannot change the provider, `extra_args`, or any security
  policy. No `config.yaml` schema change (per-task only; config-level `stage_defaults` remains a
  follow-up). See [docs/task-authoring.md](docs/task-authoring.md#stages) and
  [docs/backlog/per_stage_model_reasoning.md](docs/backlog/per_stage_model_reasoning.md).
- **Operator-customizable stage prompts**: a new optional `prompts:` block lets operators extend
  (`mode: append`) or replace (`mode: replace`) the per-stage instructions for the agent-routed
  stages (`refinement`, `planning`, `implementation`, `review`, `fixing`, `summary`) without editing
  Python. Packaged templates under `templates/prompts/<stage>.md` are the default (scaffolded by
  `init`); `overrides.<stage>` points at a `.md` file inside `prompts.templates_dir`. `strict: true`
  fails closed at startup on a missing override; `strict: false` warns and falls back to the default.
  Templates may interpolate an allowlisted set of metadata/artifact-**path** variables only
  (`{task_id} {stage} {repo_path} {task_path} {plan_path} {diff_path} {checks_path} {review_path}
  {subtask_order} {subtask_count} {subtask_spec_path}`); unknown names and literal braces pass
  through verbatim. The rendered prompt is delivered on stdin only — it can never reach provider
  argv, change the provider/`extra_args`/sandbox/approvals/denied lists/env allowlist/fallback
  policy, or be selected by task front matter; override paths are confined to `templates_dir`. Each
  rendered prompt is written (redacted) to `logs/<task-id>/stages/<stage>/rendered-prompt.md` for
  audit. Replaces the previously hardcoded `_STAGE_PROMPTS` (behavior unchanged with the defaults).
  Bumps `config.yaml` `schema_version` **2 → 3** (old configs load unchanged; the block defaults to
  packaged templates + append mode). See
  [docs/configuration.md](docs/configuration.md#prompts),
  [docs/cookbook.md](docs/cookbook.md#7a-customize-stage-prompts), and
  [docs/backlog/prompt_template_customization.md](docs/backlog/prompt_template_customization.md).
- **Auto-merge bypass (opt-in, off by default)**: after a successful publish the orchestrator can
  merge the PR itself instead of waiting for a human. Four new `git:` keys — `auto_merge`,
  `auto_merge_strategy` (`merge`|`squash`|`rebase`), `auto_merge_allow_per_task`, and
  `auto_merge_wait_for_checks` (arm GitHub-native `gh pr merge --auto`) — plus a per-task front-matter
  `auto_merge` tri-state. A per-task `true` is honored **only** when the operator sets
  `auto_merge_allow_per_task`; a per-task `false` always opts out (resolution: per-task false →
  per-task true if allowed → global `git.auto_merge` → false). The merge runs on the existing
  `creating_pr → done` edge (no new status), is idempotent via a `pr_merge` publish op, **never**
  uses `--admin`/force, and degrades to `manual_action_required` (PR left open) when blocked — never
  `failed`. The mid-pipeline dangerous-diff approval gate is unaffected. Audited via a `[AUTO-MERGE]`
  WARNING log, the append-only ledger (`auto_merged` + `merge_outcome`), and `state.db`. Bumps
  `config.yaml` `schema_version` **1 → 2** (old configs load unchanged; the keys default to `false`)
  and adds `gh pr merge` to the default `security.denied_commands` so agents cannot self-merge. See
  [docs/operations.md](docs/operations.md#auto-merge-to-the-base-branch-danger-bypasses-human-review)
  and [docs/backlog/auto_merge_bypass.md](docs/backlog/auto_merge_bypass.md).
- **`worc` short alias**: a second `console_scripts` entry point (`pyproject.toml`) maps `worc` to
  the same CLI as the canonical `wastech-orchestrator` (e.g. `worc watch`, `worc status`). The long
  name remains canonical; `pip install -e .` registers both.
- **`stop` / `restart` commands** for the `watch` daemon: a looping `watch` now writes
  `<artifacts_root>/orchestrator.pid` and installs a `SIGTERM` handler that stops the loop
  gracefully *between* ticks (an in-flight task finishes its current stage). `worc stop` sends
  SIGTERM and escalates to SIGKILL after `--timeout` (default 30 s); `worc restart` stops the
  running daemon then starts a fresh `watch` with the given flags. A second `watch` for the same
  artifact root is refused while one is live; a stale PID file is reclaimed automatically. The
  PID-file plumbing lives in a new `process_control` module; `orchestrator.pid` is excluded from
  commits.
- **`gh` pre-flight gate**: when `git.create_pull_request` is enabled, `run`/`watch` now fail fast at
  startup with an actionable message if the GitHub CLI (`gh`) is not on `PATH`, instead of surfacing
  a confusing `GitCommandError` deep inside the publish stage (`detect.require_gh`).
- **Runtime-file ignores on setup**: `init`/`install` (in-repo footprint) now append the
  orchestrator's runtime files (`state.db`, `state.db-wal`, `state.db-shm`, `config.yaml`,
  `config.yaml.bak-*`, `orchestrator.pid`) to `.git/info/exclude` (per-clone) so an operator's
  `git status` stays clean. `--gitignore-tracked` writes them to the tracked `.gitignore` instead.
- **CI quality gate** (`.github/workflows/ci.yml`): runs `ruff check`, `ruff format --check`,
  `mypy src`, and `pytest` on every pull request and on pushes to `main` (Python 3.12 / 3.13 matrix),
  mirroring the gate in `release.yml` so regressions are caught before a release tag.
- **Automatic check discovery and environment resolution** (Phases 1–3 of
  [Stage 09](docs/implementation_stages/09_automatic_check_discovery.md)): a new
  provider-agnostic `checks/` package resolves the repository's quality-gate commands without
  hand-written, technology-specific paths. It inspects manifests/lock files/`make`·`just`·`tox`·`nox`
  wrappers/local `.venv` interpreters, validates and **probes launchability** without running the
  suite, and persists a fingerprinted profile at `<workspace>/checks/resolved-profile.json`
  (recomputed per `checks.discovery.refresh`). A new `checks.discovery` config block selects the mode
  (`auto` / `deterministic` / `configured` / `disabled`); `install` writes `auto` when it cannot pin
  explicit checks. `checks.commands` now accepts a **backward-compatible union** of legacy strings and
  structured `{name, argv}` entries (no `schema_version` bump). `preflight` reports the resolved
  commands, evidence, and probe status; `status` shows the cached profile read-only. An optional,
  read-only, **advisory** agent fallback (cheap model via `checks.discovery.{model,reasoning}`,
  strict schema-validated, runs only at install) proposes candidates that pass the same validation and
  probing — it can never mark a check passing or execute anything.
- **Reasoning/effort level config**: a `reasoning` field (`low` / `medium` / `high` / `xhigh` /
  `max`) can now be set globally under `agents.providers.<provider>.reasoning` in `config.yaml` or
  per-task via `reasoning:` front-matter. For Claude, this maps to `--effort <level>` (Claude Code
  CLI v2.1+, implicitly enables adaptive thinking). For Codex, it maps to `--reasoning-effort`
  (Codex supports up to `xhigh`; `max` is Claude-only and is clamped to `xhigh` for Codex). A `model:` field can also be set per-task in front-matter
  to override the provider model for a single run.
- **Session persistence across stages**: within a single task run the orchestrator now re-uses the
  Claude session from the previous stage by passing `--resume <session_id>`. The session ID emitted
  in the `stream-json` output is captured, validated, and stored in memory on `_Pipeline`; on
  provider fallback the primary session is cleared so the fallback starts fresh. Sessions are not
  persisted across orchestrator restarts.
- **Telegram HITL completion**: terminal notifications plus typed `refinement`/`planning`
  questions and approvals, ForceReply/inline-button correlation to the configured chat and exact
  prompt, durable atomic `logs/<task-id>/hitl/*.json` recovery, redacted answer reinjection through
  `AgentRunRequest.human_input_path`, and fail-closed timeout/transport/ambiguity handling. The Core
  now approves tracked-file deletion and dependency manifest/lock changes before tests, reuses only
  exact approved planning scope, and gives a denied edit one safe reconsideration. Added strict
  Telegram config validation, webhook/chat/polling preflight, and
  `wastech-orchestrator telegram-test` for a real send/reply smoke test. Routine commit/push/PR
  remains automatic.
- `orchestrator.poll_interval_seconds` (default **300**, integer `>= 0`): `watch` is now a
  long-running loop that runs `git fetch` + `pull --ff-only` on `base_branch` and re-scans every
  interval, so a task committed and pushed to the repo after `watch` started is discovered **without
  a manual pull** — discovery is no longer limited to the local filesystem. `0` keeps the previous
  single-pass behavior (for cron-style invocation); the `watch --poll-seconds N` flag overrides the
  config value. After terminal cleanup the Git Manager also refreshes `base_branch` before the next
  task may start (spec §8.3).
- Tag-driven releases: a `release` GitHub Actions workflow ([.github/workflows/release.yml](.github/workflows/release.yml))
  that, on a `v*` tag, runs the checks, builds the wheel + sdist, and creates a GitHub
  (pre)release (`aN`/`bN`/`rcN` tags are marked pre-release). The release notes are taken from this
  version's CHANGELOG section (falling back to auto-generated notes). Process documented in
  [docs/RELEASING.md](docs/RELEASING.md).
- `wastech-orchestrator install [repo-path]` — interactive two-stage installer that binds an
  existing Git repository to a sibling control workspace, generates a validated `config.yaml`,
  and records a per-user `repo → config` binding so later commands need no `--config`.
- Config discovery: explicit `--config` → `./config.yaml` → registry binding → "run install" hint.
- `wastech-orchestrator --version`, single-sourced from the installed distribution metadata.
- Compatibility gates: `config.yaml` `schema_version` and `state.db` `PRAGMA user_version` are
  stamped and verified; a workspace written by a **newer** orchestrator is refused with a clean
  `error:` message and exit code 2 instead of being misread.

### Changed
- **`state.db` schema → v3** (`PRAGMA user_version` 2 → 3): adds `tasks.interrupted_status` (the stage a
  task was on before going terminal) so `rerun --continue` can re-enter at the failed stage. Migrated in
  place by an idempotent `ALTER TABLE ADD COLUMN`; existing databases open and upgrade cleanly.
- **Raised the default fix budget**: `agents.max_fix_cycles` `3 → 15` and
  `agents.max_total_fix_iterations` `5 → 30` (the loader defaults, both `config.example.yaml` copies,
  and the `install` generator). Existing configs are unaffected. The validator invariant
  `max_total_fix_iterations >= max_fix_cycles` still holds (30 ≥ 15).
- **Default git footprint is now in-repo audit** (`location: in_repo`, `tracking: commit`), so the
  **task file and its `summary.md`** live in the same repository as the modified code and are stored
  in git. On a terminal outcome the orchestrator finalizes **before** the commit: it moves the task
  to `tasks/done/` (or `tasks/failed/`), writes `tasks/<dir>/<id>.summary.md` beside it, then makes a
  scoped **code** commit plus a separate **`tasks/`** commit (the task + summary). The rest of `logs/`
  — plan, review, diffs, stage logs, **`summary.json`**, `terminal-cleanup.json` — and `workspace/`
  stay local via `.git/info/exclude` and **never** enter git history. The loader default, both
  `config.example.yaml` copies, `init --git-mode` (default `in_repo_commit`), and `install` reflect
  this; `external` and `in_repo`+`exclude_local` remain available. The orchestrator's root runtime
  files (`state.db` + sidecars, `config.yaml`) are excluded from the code commit and the cleanup
  dirty-check so they never reach git.
- A **failed** task that already has a branch is now finalized like a success — moved to
  `tasks/failed/`, summary written, code + task committed and pushed (best-effort) so the attempt and
  its summary are recorded in git — but **no PR** is opened. A `failed` outcome with no branch and
  `manual_action_required` are not published.
- The footprint preflight is now mode-aware: under `commit` it checks only `logs/` (a tracked `tasks/`
  is the expected audit trail, so the second task in a repo is no longer wrongly sent to
  `manual_action_required`); under `external`/`exclude_local` it still rejects a tracked `tasks/` or
  `logs/`.
- The package version is now derived from the Git tag via `hatch-vcs` (`pyproject.toml` has no static
  `version`); between tags, builds get a dev version like `0.1.0a1.dev3+g<sha>`.
- `watch` reads `tasks/pending` from the configured artifact root, not the current directory, so it
  works from anywhere inside an installed project (under the in-repo footprint default that root is
  the bound repo itself).
- A backward-incompatible `config.yaml` / `state.db` now fails loud at the CLI boundary (exit 2)
  rather than surfacing as a traceback.

### Fixed
- Reserve a persistent stage-run ID before provider execution and include it in artifact paths, so
  repeated fixing cycles and recovery runs cannot collide on `<attempt>-<provider>` directories.
- Resume tasks from their persisted pipeline status instead of always restarting implementation;
  fixing recovery now restores its failed-check or review context without double-counting a cycle.
- Isolate rejected-task quarantine paths in test fixtures so test runs cannot write generated task
  files into the repository's real `tasks/rejected/` directory.
- **Check detection now respects the project's configured tool scope** (post-test-run §1.1). Detection
  built `mypy .` / `ruff check .` from tool-presence alone; an explicit `.` *overrides* a configured
  `[tool.mypy] files`/`exclude` (or `[tool.ruff]` `src`/`include`/`exclude`), so the type gate
  type-checked `tests/` and could loop `testing→fixing` forever. Detection now reads the scope from
  `pyproject.toml` and emits a scoped command (`mypy src` / a bare `mypy` for exclude-only / `ruff
  check`) so the gate honors the project's own configuration; with no scope configured the historical
  `.` target is unchanged. `pyproject.toml` is already in the discovery fingerprint, so the cache
  invalidates automatically. Unsafe scope paths (absolute / `..`) are rejected, never passed to argv.
- **`checks/resolved-profile.json` no longer leaks into commits or the operator's `git status`**
  (post-test-run §3.1). The generated resolved check profile (a runtime cache, like `logs/`) was not
  ignored and not excluded from the code-commit guard, so it rode a real failed run's commit. `checks/`
  is now in `EXCLUDED_DIRS`, `_local_only_dirs` (all footprints) and `RUNTIME_GITIGNORE_LINES`, and the
  Git Manager guarantees the runtime-file ignores exist in the clone it operates in (new
  `ensure_runtime_excludes`, called at branch-prep). Per-task `logs/<id>/checks/*.log` is unaffected.
  An already-committed profile from a prior run is left untracked-on-request — preflight deliberately
  does not gate on it; the operator runs `git rm --cached checks/resolved-profile.json` once.
- **Telegram Approve/Deny presses are no longer silently lost** (post-test-run §4.1). `poll_reply`
  advanced the update offset for every update and acknowledged only an exact match, so a near-miss
  press (stale/duplicate button, wrong message, or an inaccessible `callback_query.message`) was
  consumed and dropped with no feedback — to the operator it looked like "nothing happened". Now every
  callback in the configured chat is acknowledged (the matching one continues/denies; a near-miss shows
  a "this approval is no longer active" alert) and near-misses are logged with a secret-free reason
  (`wrong_message_id`/`unexpected_data`/`message_none`); a foreign chat's callback is never
  acknowledged. A second `getUpdates` consumer on the same bot token (Telegram HTTP 409 Conflict — e.g.
  two orchestrator clones sharing one token) is now detected and surfaced with a clear "only one poller
  may run per bot token" message at both preflight and run time, instead of a generic transport error.
  No bot token or raw chat id is ever logged.
- **Deterministic failure summary is now compact** (post-test-run §5.1). When no agent-authored summary
  exists, the fallback inlined the full task description *and* the entire diff into the committed
  `<id>.summary.md` (a real run produced a ~580-line file that was almost all raw diff). It now shows a
  `git diff --stat` (files + line counts) and links to the task file and to the already-redacted
  `logs/<id>/current.diff`. This also closes a latent gap where the committed summary could contain an
  *unredacted* diff (the old fallback inlined the diff without the `current.diff` redaction).

## [0.0.1]

- Initial pre-MVP scaffolding: provider contracts, config schema + validator, task model, the
  deterministic pipeline, `init` / `run` / `watch` / `preflight` / `status`, and the security and
  observability layers. See `docs/implementation_stages/` for the phased build.
