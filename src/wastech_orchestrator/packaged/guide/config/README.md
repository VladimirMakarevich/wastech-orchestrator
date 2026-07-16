# Building `config.yaml` for wastech-orchestrator

**You are an AI agent helping an operator assemble or tune `config.yaml` for wastech-orchestrator.** Use this folder as the compact, installable reference for configuration work. If you only have a moment, read this file first.

- **[reference.md](reference.md)** — the complete field reference: for the meaning, allowed values, default, and constraints of _every_ `config.yaml` field, read this. This README is the how-to; `reference.md` is the what-each-field-does.
- **[best-practices.md](best-practices.md)** — safe defaults, how to structure checks, and what mistakes to avoid.
- **[skills/worc-config/SKILL.md](skills/worc-config/SKILL.md)** — a copy-ready skill that can interview the operator and draft a config.

## Start from the generated file

Prefer editing the `.worc/config.yaml` that `worc install .` already generated for this repository. It starts from the packaged defaults, uses the detected repo root and base branch, and is already validated to the current schema shape.

Only build from scratch when there is no installed config yet. In that case, keep the same block order as the packaged example (only `schema_version`, `repo`, `agents`, and `security` are required — every other block is optional and takes its defaults when omitted; see [reference.md](reference.md) for the defaults):

1. `schema_version`
2. `orchestrator`
3. `repo`
4. `paths`
5. `agents`
6. `security`
7. `validation`
8. `checks`
9. `git`
10. `telegram`
11. `skills`
12. `supervisor`
13. `logging`
14. `memory`
15. `tools`
16. `prompt_audit`

## Build in this order

### 1. `repo`

Fill in the repository identity first:

- `url` — the Git remote the orchestrator will push to.
- `local_path` — the repo path the orchestrator should operate in.
- `base_branch` — the branch to refresh before work and, by default, return to after cleanup (see `checkout_base_on_cleanup`).
- `branch_prefix` — usually leave the default `worc`.
- `branch_mode` — leave the default `new` (fork a fresh task branch from base). Only change it if most tasks on this repo should work in an existing or current branch; individual tasks can override it per-task.
- `checkout_base_on_cleanup` — leave unset (the default). Unset means each task returns to `base_branch` when it finishes only in `new` mode; `existing`/`current` stay on the branch. Set `false` to never switch back (handy when every task runs on one shared branch), or `true` to force `new` and `existing` back to base.

If the target project needs customer-specific branch names, keep `branch_prefix` default and let individual tasks set `branch_name`.

### 2. `agents`

Decide which CLIs are actually available and keep the set small:

- `allowed` — only the providers the operator can really run (`codex`, `claude`).
- Exactly one provider must be `primary: true`.
- Leave the shipped `model`, `reasoning`, `timeout_seconds`, `sandbox`, `permission_profile`, and `max_turns` defaults unless the operator has a concrete reason to change them.
- Leave `decomposition.enabled: false` until the team is ready for one-task-many-subtasks planning.

### 3. `security`

Treat this block as a guardrail, not a convenience area:

- Keep `strict_isolation: true` unless the operator consciously accepts full-access runs.
- Pass only names in `allowed_environment`; secret **values** never belong in the file.
- Keep `denied_commands` complete; it replaces the default list rather than extending it.
- Keep Codex `extra_args` within its documented closed allowlist; authority-bearing options fail
  config validation. Claude retains the common sandbox/approval/rule-bypass screen.
- `trust_level` sets the approval threshold for the mid-task dangerous-diff gate: `auto` (default) lets routine in-repo deletions/edits proceed; `strict` gates every deletion or dependency-manifest edit. It never lowers the hard ceiling — only which diffs raise the gate.
- `protected_paths` is the always-ask floor: repo-relative globs (same dialect as `checks.command_sets[].paths`) that require approval on **any** change regardless of `trust_level`. Default `[]` (no floor); add sensitive surfaces here (e.g. `.github/workflows/**`, `src/security/**`).

### 4. `checks`

Model the repository's real ownership boundaries:

- A single-project repo can start with one catch-all set and no `paths`.
- A monorepo should usually have one set per project or surface (`backend/**`, `mobile/**`, `docs/**`).
- Every command is an argv list, never a shell string.
- Use `cwd` only when a command must run below the repo root.

Typical first-pass sets:

- Python: `pytest`, `ruff check .`, `mypy src`
- Node/TypeScript: `npm run lint`, `npm test`
- Go: `go test ./...`
- Rust: `cargo test`, `cargo clippy -- -D warnings`

If you are unsure which checks are trustworthy yet, it is better to leave `command_sets: {}` and say "no quality gate configured yet" than to invent a fake gate.

### 5. `git`

Keep publishing conservative by default:

- `create_pull_request: true` when `gh` is available and the team reviews in GitHub.
- `auto_merge: false` unless protected branches and required checks already enforce the right quality bar.
- Leave `footprint.audit_on_branch: task` unless the team explicitly wants the audit trail on a sibling branch.

### 6. Optional blocks

Only enable these when the operator asked for them (each field is documented in full in [reference.md](reference.md)):

- `orchestrator` — the `watch` loop cadence (`poll_interval_seconds`), the instance `queue` selector, and `auto_mode` task chaining.
- `paths` — `tasks_dir`, the repo-relative home of the task lifecycle (rename only to avoid clashing with an existing `tasks/`).
- `telegram` — real human-in-the-loop and notifications.
- `skills` — repo-local `.claude/skills` inventory for planning.
- `supervisor` — non-default model/reasoning for the read-only oversight layer.
- `logging` — operator log `level` and per-attempt artifact retention (`artifacts`).
- `memory` — persistent, repo-scoped memory (`enabled` plus retrieval/promotion/cleanup caps).
- `prompt_audit` — prompt recording for debugging or compliance.
- `tools` — only its `default_timeout_seconds` (default `3600`), the flow-wide timeout for custom `tool` nodes. The tool feature itself is enabled per-flow (a `kind: tool` node reading `.worc/tools/`), not here — see `flows/README.md`. Set this only to change the default timeout.

## Questions worth asking

Ask only for inputs that cannot be discovered safely from the repo:

- Which provider(s) should be enabled right now?
- Should PR creation stay on?
- Is auto-merge allowed here, or should it stay off?
- Which command sets are mandatory for a change to be considered safe?
- Is Telegram needed for HITL, or should it stay disabled?

If the repo already answers the question (`origin`, current branch, `pyproject.toml`, `package.json`, existing `.worc/config.yaml`), inspect first and ask later.

## What not to do

- Do not put tokens, passwords, chat ids, or secret values in `config.yaml`.
- Do not invent provider ids, stage names, or model ids.
- Do not weaken security through `extra_args`; Codex rejects unknown options and config keys
  fail-closed.
- Do not make `skip_if_unavailable: true` the default for required test suites.
- Do not turn on `auto_merge` just to reduce friction.

## Finish

After editing the config:

1. Run `worc preflight`.
2. Fix every reported provider, isolation, flow, or Telegram issue.
3. If the package was upgraded, run `worc upgrade-config` first so the file has the current schema shape.

[reference.md](reference.md) is the complete field reference — you should not need anything outside this guide to configure the orchestrator. The orchestrator repository's `docs/configuration.md` and `docs/operations.md` carry the same material with extra contributor-facing detail (design rationale, internals); reach for them only if you are working on the orchestrator itself.
