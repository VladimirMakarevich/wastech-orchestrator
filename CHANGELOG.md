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
- Tag-driven releases: a `release` GitHub Actions workflow ([.github/workflows/release.yml](.github/workflows/release.yml))
  that, on a `v*` tag, runs the checks, builds the wheel + sdist, and creates a GitHub
  (pre)release (`aN`/`bN`/`rcN` tags are marked pre-release). Process documented in
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
- The package version is now derived from the Git tag via `hatch-vcs` (`pyproject.toml` has no static
  `version`); between tags, builds get a dev version like `0.1.0a1.dev3+g<sha>`.
- `watch` reads `tasks/pending` from the configured artifact root (the workspace), not the current
  directory, so it works from anywhere inside an installed project.
- A backward-incompatible `config.yaml` / `state.db` now fails loud at the CLI boundary (exit 2)
  rather than surfacing as a traceback.

## [0.0.1]

- Initial pre-MVP scaffolding: provider contracts, config schema + validator, task model, the
  deterministic pipeline, `init` / `run` / `watch` / `preflight` / `status`, and the security and
  observability layers. See `docs/implementation_stages/` for the phased build.
