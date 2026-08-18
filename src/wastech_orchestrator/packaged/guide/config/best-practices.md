# Best practices for `config.yaml`

Read [README.md](README.md) first. This file is about keeping a project-specific orchestrator config safe, understandable, and easy to maintain.

## 1. Start minimal

Prefer the smallest config that can run safely:

- one `primary` provider;
- `strict_isolation: true`;
- `security.protected_paths` naming the repo's sensitive surfaces — the default `trust_level: auto` raises the approval gate on nothing else, and the default empty list is no floor at all;
- `auto_merge: false`;
- Telegram disabled until a human-in-the-loop path is really needed;
- one or a few clear `checks.command_sets`.

Every extra knob increases the chance that a later operator misunderstands why it is there.

## 2. Keep one clear provider story

`agents.allowed` should describe reality, not aspiration.

- If the team only uses Codex today, allow only `codex`.
- If both CLIs are installed and intentionally supported, allow both.
- Exactly one provider is `primary: true`; make that the provider the team expects most nodes to run on.

Do not add a provider "for later" if preflight would fail on every machine today. A provider in `allowed` whose CLI reports no stored credentials is fatal in any role: it fails preflight, and `run` / `rerun` / `watch` refuse to start at all — whether or not any node routes to it.

## 3. Design check sets around change ownership

The biggest config quality lever is usually `checks.command_sets`.

- Single-project repo: one catch-all set is fine.
- Monorepo: split by real path ownership, not by wishful architecture.
- Shared root files (`package.json`, `pyproject.toml`, lockfiles, CI config) deserve explicit coverage.
- When in doubt, prefer a catch-all set over accidental gaps.

Keep command names obvious (`tests`, `lint`, `types`, `ios-tests`) so logs stay readable.

## 4. Treat `skip_if_unavailable` as an exception

Use `skip_if_unavailable: true` only for toolchains that are genuinely optional on the current host, such as iOS checks on a Linux machine.

Do not use it to paper over a broken default developer environment. If a check is required for safe delivery, let it fail loudly.

## 5. Keep secrets and machine-local data out of the file

- Secret values belong in environment variables or `.worc/.env`, never in YAML.
- `allowed_environment` should name variables, not embed their contents. Prefer a prefix pattern (`DOTNET_*`) over guessing a toolchain's ten variable names one at a time, and read back what it matched in `worc preflight` before relying on it.
- `extra_environment` is the one place a **value** enters the config, and it is in plaintext: put toolchain roots and cache paths there (`NUGET_PACKAGES`, `npm_config_cache`) and never a credential. Nothing can check a value for secrecy, so this rule is a contract, not a gate — the load-time refusal only covers secret-looking *names*. Every child process the orchestrator starts receives these, `worc preflight` prints their names, and the agent CLIs get their own credentials from their own stores, so a token is never needed here.
- Avoid host-specific absolute paths unless the orchestrator really runs on a single fixed machine.
- Leave `agents.providers.claude.allow_native_memory` off (its default) unless you deliberately accept the risk: turning it on lets Claude's own auto-memory write to a HOME store that is **outside** the orchestrator's redaction net and audit trail. It is off by default and `install` never writes it — enable it only as a conscious choice.

If several operators share the same repo, a config with fewer machine assumptions survives longer.

## 6. Default to conservative Git behavior

The safe default publish shape is:

- create a PR;
- do not auto-merge it;
- keep the audit commit on the task branch.

Move away from that only when the repository already has branch protection and required checks enforcing the same bar.

## 7. Preserve intent when you change defaults

When you deviate from the shipped defaults, keep the reason easy to recover:

- add a short YAML comment for unusual settings;
- keep command-set names descriptive;
- group related settings together instead of scattering overrides.

That matters because `upgrade-config` preserves values but re-emits the file and strips inline comments when it rewrites it.

## 8. Re-run preflight after every meaningful config edit

`worc preflight` is the fastest way to catch:

- provider binaries missing from `PATH`, and any allowed provider whose CLI reports no credentials;
- invalid or unsafe provider settings;
- `gh` missing from `PATH` while `git.create_pull_request` is on;
- Telegram misconfiguration.

It does not probe or run your checks: it lists each configured command set with its `paths`, commands, and flags, which is enough to eyeball selection coverage. A malformed set never gets that far — the config validator rejects it on load, so every command exits 2 with the reason.

Preflight does **not** validate flow files — run `worc validate-flow --all` for that (it is config-aware, so it also catches flows made invalid by a config edit, e.g. a node pinned to a provider you just removed from `agents.allowed`). It covers your own flows under `.worc/flows/` only — the packaged built-ins are excluded, so with no operator flows `--all` reports nothing to check and exits 0. Do not treat config editing as done until both preflight and `validate-flow --all` are green.
