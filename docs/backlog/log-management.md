# Log management: `worc logs clean` and `logging.*` config

Status: **implemented** (2026-06-27, config `schema_version` 23) Date: 2026-06-26 Owner: Vladimir Makarevich

Shipped as specified, with these decisions locked during implementation: default `logging.artifacts` is `standard` (greenfield — no migration concern); `minimal` is strict (only `result.json`, even on failure — no errors-only exception); `--keep 0` confirms like the bare delete-all; a config `schema_version` bump to 23 was made (this repo bumps for every format change). `logging.artifacts` scope is the per-attempt provider files only — prompt-audit stays governed by `prompt_audit`. See [configuration.md §logging](../configuration.md#logging) and [operations.md §logs clean](../operations.md). The original proposal follows.

Two complementary log-management features proposed together because they share a root cause: `.worc/logs/` grows unboundedly and the operator trace verbosity is not persisted between sessions.

## The problem

`.worc/logs/<task-id>/` directories accumulate on disk indefinitely. Each task leaves `stdout.log`, `events.jsonl`, `request.json`, diffs, and prompt-audit files. Long-running deployments fill the disk without any built-in way to reclaim space.

In parallel, `--log-level` must be re-passed on every invocation. There is no config key to persist the preferred verbosity, so operators running with `warning` or `error` level repeat the flag every time.

## Constraints

- No secrets in logs is a hard invariant that must hold at all verbosity levels — the redaction filter already enforces this at call sites and must not be bypassed.
- `completed.jsonl` (the ledger) is the audit trail for task state transitions; it must not be deleted as a side-effect of routine artifact cleanup.
- Artifact levels must degrade gracefully: `minimal` mode must not break any orchestrator logic that reads its own artifacts (e.g. result parsing).

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| `rm -rf .worc/logs/*` manually | No `--keep N`, no protection against removing the ledger, no UX |
| `alias worc="worc --log-level warning"` | Not idiomatic; doesn't appear in config, invisible to other team members using the same `.worc/` |
| Auto-TTL in config (`logs.retain_days: 7`) | More powerful but also more surprising; a one-shot manual command is safer as first step |
| Per-artifact-type flags (`save_events: false`) | Maximum control but high surface area; levels cover the practical use cases |

## Decision

### 1. `worc logs clean [--keep N]`

New `logs` subcommand with a `clean` action:

- `worc logs clean` — prompts `Are you sure? [y/N]`, then removes all task artifact directories under `.worc/logs/`. `completed.jsonl` is preserved by default.
- `worc logs clean --keep N` — keeps the N most recently modified task directories, removes the rest. No confirmation prompt (explicit count = clear intent).
- `worc logs clean --all` — removes everything including the ledger (`completed.jsonl`). Requires confirmation regardless of other flags.

The command is safe to run while `worc watch` is idle; running it while a task is active is undefined — document as unsupported (not a hard guard in v1).

### 2. `logging.*` in `config.yaml`

Two new keys under a `logging` section in `OrchestratorConfig`:

**`logging.level`** (`debug | info | warning | error`, default `info`): persists the operator trace verbosity. The `--log-level` CLI flag overrides this key when provided. No schema version bump required if treated as an additive optional field.

**`logging.artifacts`** (`minimal | standard | full`, default `standard`): controls which per-node artifact files are written under `.worc/logs/<task-id>/stages/`:

| Level | Files written |
| --- | --- |
| `minimal` | `result.json` only |
| `standard` | `stdout.log`, `stderr.log`, `result.json` |
| `full` | all files: `events.jsonl`, `request.json`, `before.diff`, `after.diff`, `result.json`, `stdout.log`, `stderr.log` |

Prompt-audit is controlled by the existing `prompt_audit` key and is independent of `logging.artifacts`.

The cost of `minimal`: losing stdout/stderr from failed runs makes remote debugging harder. Operators should only use `minimal` on well-understood, frequently-run pipelines.

## Open questions

- Should `worc logs clean --keep N` also confirm when N is 0 (equivalent to "delete all")? Probably yes — add the same prompt when N = 0.
- Should `minimal` still write `stderr.log` on non-zero exit? A "errors only" exception would preserve debuggability at the cost of slightly more complexity.
- Config version bump: adding `logging.*` as an optional block with defaults likely requires no version bump (existing loaders tolerate unknown keys); confirm during implementation.

## Implementation notes

`worc logs clean` — add `src/wastech_orchestrator/cli/logs.py` (or extend existing CLI group), sort task dirs by `os.path.getmtime`, `shutil.rmtree` the ones outside `--keep`. The ledger path (`Ledger` class) already knows its own file path — use that reference to exclude it by default.

`logging.*` config keys — add the `logging` section to `OrchestratorConfig` in `schema.py`; read in `configure_logging()` (operator trace level) and in the artifact-writing sites inside the provider layer (artifact level). CLI `--log-level` flag merges at the call site, not in the loader.
