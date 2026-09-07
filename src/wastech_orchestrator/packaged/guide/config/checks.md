# `checks` — the quality gate

**You are an operator (or an agent helping one) configuring wastech-orchestrator.** This page documents the `checks` block: the operator-authored command sets that decide whether a change passes, how they are selected from the diff, and what each command field means.

For the fields not on this page see [reference.md](reference.md), which also carries the cross-field rules that apply across blocks; for the how-to walkthrough see [README.md](README.md) and for safe defaults [best-practices.md](best-practices.md).

## `checks` — the quality gate (per-project command sets)

Empty / omitted `command_sets` means **no gate** (every task passes the checks node). The runner runs the union of sets whose `paths` glob the task diff; a set with no `paths` always runs on a non-empty diff; an empty diff runs nothing. Nothing is auto-discovered — you author this.

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `checks.timeout_seconds` | int | `7200` | `> 0` | Global per-command timeout (argv, no shell). |
| `checks.command_sets` | mapping `<name>` → set | `{}` | (per-set below) | Named per-project sets. Single-root repo: one catch-all set (no `paths`). Monorepo: one set per real path-ownership boundary. **If any flow you run produces documents rather than code** (a `deep_research`-style flow committing Markdown), the catch-all recommendation stops being enough on its own: with no `paths` it fires on that diff too, so a research run pays for the whole code gate — and if the set contains a command that *rewrites* files, that run parks on the green-but-dirtying guard. Either scope the catch-all's `paths` to code, or keep it and add a documents set (e.g. `paths: ["**/*.md"]` running a Markdown format **check**). |

### `checks.command_sets.<name>` — one command set

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `.commands` | list of command specs | — (required) | Non-empty. | The commands in the set. |
| `.paths` | list of repo-relative globs | `[]` | Each non-empty. | Diff-path selectors deciding when the set runs. Empty = always runs on any non-empty diff. `**` crosses dirs, `*` stays within a segment. |
| `.timeout_seconds` | int \| null | `null` (→ global) | If set, `> 0`. | Per-set override of the global per-command timeout (e.g. a slow iOS build). |
| `.skip_if_unavailable` | bool | `false` (fail-closed) | — | `true` = when the toolchain binary is absent, skip loudly (never silently "passed"). Use only for genuinely optional toolchains (e.g. iOS checks on Linux), never to paper over a broken required suite. **It is not an escape hatch for a missing toolchain:** the gate is fail-closed on an incomplete run, so a set that was the *only* one the diff selected and is then skipped leaves nothing run and parks the task at `manual_action_required` — the same place the launch failure would have. When you genuinely want a gate not to run, disable the node per task (`nodes.<checks-node-id>.enabled: false`), do not skip your way there. |

### `checks.command_sets.<name>.commands[]` — one command

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `.argv` | list[str] | — (required) | Non-empty; no shell metacharacters; no forbidden/sandbox-weakening args; must not match a `denied_commands` entry. | The command as an explicit argv list (no shell string), e.g. `[ruff, check, .]`. |
| `.name` | string \| null | `null` | — | A readable logical name (`tests`, `lint`, `types`) — keeps logs legible. |
| `.cwd` | string \| null | `null` (= clone root) | Repo-relative, no `..`/absolute. | Working dir for the command. Use only when it must run below the repo root (monorepo subproject). |

