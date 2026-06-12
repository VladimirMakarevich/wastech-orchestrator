# Changelog

All notable changes to **wastech-orchestrator** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project aims to follow
[Semantic Versioning](https://semver.org/) once it leaves the `0.x` pre-release line.

The persisted artifacts that outlive an upgrade carry their own independent schema versions
(see the spec's "Versioning and compatibility" section and
[docs/operations.md](docs/operations.md#upgrading-the-orchestrator)):

- `config.yaml` — top-level `schema_version` (current: **1**)
- `state.db` — SQLite `PRAGMA user_version` (current: **1**)
- registry (`registry.json`) — `version` (current: **1**, read forward-tolerantly)

The maintainer bumps the package version in `pyproject.toml` on release; `wastech-orchestrator
--version` reports the installed version.

## [Unreleased]

### Added
- Opt-in Telegram terminal notifications and a blocking `ask_human` HITL primitive, with
  environment-only credentials, deterministic timeouts, secret redaction, and no-op behavior when
  disabled or unconfigured.
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

## [0.0.1]

- Initial pre-MVP scaffolding: provider contracts, config schema + validator, task model, the
  deterministic pipeline, `init` / `run` / `watch` / `preflight` / `status`, and the security and
  observability layers. See `docs/implementation_stages/` for the phased build.
