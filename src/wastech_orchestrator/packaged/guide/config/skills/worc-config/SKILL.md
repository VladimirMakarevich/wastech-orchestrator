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
3. Start from the existing `.worc/config.yaml` when present. If there is no installed config yet, draft one in the packaged block order (`schema_version`, `orchestrator`, `repo`, `agents`, `security`, `validation`, `checks`, `git`, `telegram`, `skills`, `supervisor`, `prompt_audit`).
4. Keep the safe defaults unless the operator overrides them deliberately.
   - exactly one provider is `primary: true`;
   - `strict_isolation: true`;
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
- Do not enable full-access runs casually.
- Do not turn on `auto_merge` just to save time.
- Do not add providers that are not actually installed or intended for use.
- Do not replace a working operator value unless the user asked for that exact change.
