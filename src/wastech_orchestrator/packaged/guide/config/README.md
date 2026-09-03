# Building `config.yaml` for wastech-orchestrator

**You are an AI agent helping an operator assemble or tune `config.yaml` for wastech-orchestrator.** Use this folder as the compact, installable reference for configuration work. If you only have a moment, read this file first.

- **[reference.md](reference.md)** — the entry page of the complete field reference: for the meaning, allowed values, default, and constraints of _every_ `config.yaml` field, start there. It carries the rules that apply everywhere, the core identity blocks and the cross-field gotchas, and links one page per concern ([agents.md](agents.md), [security.md](security.md), [checks.md](checks.md), [supervisor.md](supervisor.md), [runtime.md](runtime.md)). This README is the how-to; the reference is the what-each-field-does.
- **[best-practices.md](best-practices.md)** — safe defaults, how to structure checks, and what mistakes to avoid.
- **[../skills/worc-config/SKILL.md](../skills/worc-config/SKILL.md)** — a copy-ready skill that can interview the operator and draft a config.

## Start from the generated file

Prefer editing the `.worc/config.yaml` that `worc install .` already generated for this repository. It uses the detected repo root and base branch and is already validated to the current schema shape.

It is also **deliberately small**. `install` writes only what it resolved (`repo`, the selected `agents.providers`, `git`, the `orchestrator.auto_mode` answer), the one block whose values deviate from the fail-closed defaults (`security` — the advanced mode), and three affordances (`paths.tasks_dir`, the empty `checks.command_sets` gate, the `telegram.enabled` switch). Everything else is absent on purpose: an omitted key runs at the default this reference documents, so there is nothing to read and nothing to keep in step. A comment block at the end of the generated file lists what was left out.

That makes the edit loop simple: to change a default, copy the block for it out of the `config.example.yaml` sitting beside your config and set the one value you want. You never need the whole block — write the keys you are changing and leave the siblings absent.

Only build from scratch when there is no installed config yet. In that case, keep the same block order as the packaged example (every block is optional to the loader and takes its defaults when omitted — the one structural requirement is exactly one `agents.providers.<id>.primary: true`; in practice always write `repo` too, since its defaults are placeholders. See [reference.md](reference.md) for the defaults):

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
11. `supervisor`
12. `logging`
13. `memory`
14. `tools`
15. `prompt_audit`

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
- Leave the shipped `model`, `reasoning`, `timeout_seconds`, `permission_profile`, and `max_turns` defaults unless the operator has a concrete reason to change them.
- There is no `sandbox` key. The access level lives in `permission_profile`; a config that still carries `sandbox:` is rejected on load as an unknown key, and `upgrade-config` does not strip it — delete the line by hand. It is not tolerated on purpose: its one remaining value selected a provider full-access mode, which is now forbidden outright, so keeping the key loadable would only suggest that some value of it still works.
- Leave `decomposition.enabled: false` until the team is ready for one-task-many-subtasks planning.

### 3. `security`

Treat this block as a guardrail, not a convenience area:

- Know what `strict_isolation` is set to before you run anything: `install` writes `false`, and `false` **is** the advanced mode — the parent environment goes to the agent whole, read-isolation is forced off, every node gets a shell, the agent writes outside the clone and reaches the whole network. Read [`security.md`](security.md) and decide whether you want it; set `true` for the fail-closed sandbox instead. It unlocks no provider full-access mode either way: those are forbidden outright.
- Know that `disable_read_isolation` is `true` — read-isolation is **off** out of the box, and `install` writes the key so you can find it. Set it `false` to turn read-isolation on, which takes effect only alongside `strict_isolation: true` (the master switch forces read-isolation off at `false`). The write side (commit/staging gates, PR control, `denied_read_paths`) is in force either way.
- Pass only names in `allowed_environment`; secret **values** never belong in the file. The list replaces the default wholesale, so keep `PATH` (a config without it is rejected at load) and, on Windows, `SystemRoot` (preflight and `run`/`watch`/`rerun` refuse without it). `install` does not write the key at all — the loader resolves the host OS default at load time, so a config moved between machines picks up the right names; the longer list in `config.example.yaml` is the cross-platform union. Write the key only to narrow or extend it, and then write the whole list. An entry may be a prefix pattern (`DOTNET_*`, `npm_config_*`) instead of an exact name — resolved against your environment, with the same secret-name filter applied to every name it produces, so `NUGET_*` forwards `NUGET_PACKAGES` and refuses `NUGET_API_KEY`. Under strict isolation a pattern alone cannot pull a name loaded from `.worc/.env` into an agent child; name it exactly when that is intentional. In advanced mode the list gates only orchestrator-owned `git`/`gh`.
- Use `extra_environment` when an agent/check/tool child needs a variable **set**, not forwarded — a toolchain root or a cache path (`NUGET_PACKAGES`, `npm_config_cache`). Forwarding only passes on what your shell exported, so it is unset on the next machine and a forgotten `export` is skipped in silence. Orchestrator-owned `git`/`gh` also sees assignments, except in the `GIT_*`/`GH_*`/`GITHUB_*` namespace, where only `GIT_CONFIG_GLOBAL` and the two token names get through. YAML keys and values must be strings (quote names such as `on` and values such as `1`), and **no credentials** — the value is plaintext in this file, and only secret-looking *names* are refused at load.
- Keep `denied_commands` complete; it replaces the default list rather than extending it.
- `allow_git_evidence` (`install` writes `true`) is the master switch for the read-only git-evidence grant: only with it on does a flow node's `git_evidence: true` actually give that node the read-only git verbs. It never makes a node writable — under `strict_isolation: true`, where the key is the only setting it means anything in, the sandbox still denies every write, and `denied_commands` still adds its friction (a refusal that shows up in the log). Two things to know: beside the shipped `strict_isolation: false` it does nothing at all — every node already has an unscoped shell there — and it starts mattering the moment you set `strict_isolation: true`, so turn it off then unless a flow you run genuinely audits delivery history.
- Do not add `extra_args` that disable sandboxing, approvals, or rule enforcement.
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

Touch these only when the operator asked for them (each field is documented in full in [reference.md](reference.md)). One is already on after `install` — `supervisor` (`enabled: true`) — so for that one the question is whether to turn it off, not on:

- `orchestrator` — the `watch` loop cadence (`poll_interval_seconds`), the instance `queue` selector, and `auto_mode` task chaining.
- `paths` — `tasks_dir`, the repo-relative home of the task lifecycle (rename only to avoid clashing with an existing `tasks/`).
- `telegram` — real human-in-the-loop and notifications.
- `supervisor` — the read-only oversight layer, **on by default**. This is where you turn it off (`enabled: false`) or set per-phase model/effort under `observe` / `finalize` / `handoff`.
- `logging` — operator log `level` and per-attempt artifact retention (`artifacts`).
- `memory` — persistent, repo-scoped memory (`enabled` plus retrieval/promotion/cleanup caps). **Experimental and off out of the box** — turn it on only when the operator explicitly asks to experiment with it.
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
- Do not invent provider ids, flow node ids, or model ids.
- Do not weaken security through `extra_args`.
- Do not make `skip_if_unavailable: true` the default for required test suites.
- Do not turn on `auto_merge` just to reduce friction.

## Finish

After editing the config:

1. If the package was upgraded, run `worc upgrade-config` first so the file has the current schema shape. It adds new keys from the template and strips the ones that were retired with a migration; a key that was removed **without** one — `agents.providers.<provider>.sandbox` — it leaves in place, so that line has to be deleted by hand before the config will load at all. It asks before writing, and the prompt is worth reading: the merge takes _every_ key from the packaged template, so keys your small config deliberately left at their defaults come back written out, and your inline comments do not survive the rewrite (a timestamped backup is kept). Decline it and nothing is touched; `-y` skips the prompt in a script.
2. Run `worc preflight`. Fix every reported provider, credential, isolation, `gh`, or Telegram issue.
3. Run `worc validate-flow --all` — preflight does not look at flows, and a config edit can invalidate one (a node pinned to a provider you just removed from `agents.allowed`). It checks the operator flows under `.worc/flows/` only.

The config is done when both are green.

[reference.md](reference.md) is the complete field reference — it documents every key, so this guide is all you need to configure the orchestrator. The orchestrator's own repository adds contributor-facing material on top of the same fields (design rationale, internals); that is only worth reading if you are working on the orchestrator itself.
