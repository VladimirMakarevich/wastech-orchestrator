---
name: worc-config
description: Build or revise a wastech-orchestrator `config.yaml` for a specific repository. Use when an operator wants help choosing providers, checks.command_sets, Git publishing, Telegram, or safe defaults for their repo, and wants a ready-to-edit config with the orchestrator's security invariants preserved.
---

# worc-config

Help an operator assemble or revise `.worc/config.yaml` for their repository. Speak in the user's language (default to the language they wrote in).

## Goal

Produce a project-specific config that:

- matches the repository's actual tooling;
- preserves the orchestrator's security invariants;
- keeps defaults unless the operator has a concrete reason to change them;
- is ready for `worc preflight`.

Before drafting or changing any field, consult `reference.md` in the packaged config guide (`.worc/guide/config/reference.md`) for that field's allowed values, default, and constraints — do not guess a field's meaning or invent values.

## How to run

1. Inspect the repo before asking questions. Look for:
   - an existing `.worc/config.yaml`;
   - `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, lockfiles, CI config;
   - the Git remote and the likely base branch.
2. Ask only the missing essentials. Usually:
   - which provider(s) should be enabled now;
   - whether PR creation should stay on;
   - whether auto-merge is allowed here;
   - which checks are mandatory for safe delivery;
   - whether Telegram should stay disabled.
3. Start from the existing `.worc/config.yaml` when present. If there is no installed config yet, draft one in the packaged block order (`schema_version`, `orchestrator`, `repo`, `paths`, `agents`, `security`, `validation`, `checks`, `git`, `telegram`, `supervisor`, `logging`, `memory`, `tools`, `prompt_audit`). The only thing a config **must** carry is `agents.providers` with exactly one `primary: true` — every other block, `schema_version` and `repo` and `security` included, takes its defaults when omitted, and a file carrying that one block alone loads and validates clean. So write the blocks this repository actually needs rather than a full skeleton of defaults.
4. Keep these unless the operator overrides them deliberately. Three of them are what `install` writes rather than what is safest — say which, so the operator is choosing rather than inheriting.
   - exactly one provider is `primary: true`, and it is listed in `agents.allowed` — both are hard validation rules, not preferences: zero or two primaries refuses the config outright;
   - `strict_isolation`: `install` writes `false`, which **is** the advanced mode, not a milder sandbox — `true` is the fail-closed one and is what an omitted key means;
   - `disable_read_isolation`: `install` writes `true`, and `true` is also the code default — read-isolation is **off** out of the box. It is the key that makes hardening the master switch a two-line change rather than one: the effective state is `disable_read_isolation OR NOT strict_isolation`, so an operator who sets `strict_isolation: true` on the advice above and leaves this key as installed still runs with read-isolation off, and nothing says so. Set it to `false` in the same edit;
   - `allow_git_evidence`: `install` writes `true`; it is inert beside `strict_isolation: false` and becomes a real grant the moment that is `true`, so turn it off then unless a flow here audits delivery history;
   - `auto_merge: false`;
   - no forbidden `extra_args`;
   - secrets stay in env vars, not YAML.
5. Author `checks.command_sets` from the repo layout.
   - single-project repo: one catch-all set is often enough;
   - monorepo: split by `paths` ownership;
   - every command is an argv list, never a shell string;
   - use `cwd` only when needed.
6. Write or patch `.worc/config.yaml`.
7. Tell the operator to run `worc preflight`.

## Heuristics

- Prefer leaving a block at its generated default over inventing a clever override.
- Do not invent model ids; keep the existing/default ones unless the operator explicitly wants a change.
- If a check is required for safe delivery, do not hide it behind `skip_if_unavailable: true`.
- If the repo has no trustworthy check story yet, it is acceptable to leave `command_sets: {}` and say clearly that no quality gate is configured.

## What not to do

- Do not put token values, chat ids, or passwords into the file.
- Never write a `sandbox` key or a full-access `extra_args` selector: the first is an unknown key, the second is forbidden outright, so either one stops the config before it runs.
- Do not turn on `auto_merge` just to save time.
- Do not add providers that are not actually installed or intended for use.
- Do not replace a working operator value unless the user asked for that exact change.
