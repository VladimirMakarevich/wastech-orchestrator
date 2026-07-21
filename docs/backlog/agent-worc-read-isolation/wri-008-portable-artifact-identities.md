# WRI-008 — Validate portable artifact path identities

**Status:** open **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** —

## Problem

The current task-id regex allows values that are not portable Windows path components, including device names such as `con`, `nul`, `com1`, and a trailing dot. More critically, flow node ids are converted to strings but receive no path-segment validation before they are joined into `node_run_dir`, `node_history_path`, provider-attempt directories, generic `<node-id>.out.md`, and prompt-audit file names. A custom node id containing `/`, `\`, `..`, an absolute/drive prefix, or an unbounded value can escape or corrupt the artifact layout. A dot in a node id also cannot participate in the renderer's documented `{<node-id>_path}` token grammar.

WRI-001 would copy the same unchecked identities into a second security-sensitive root. Fixing only the new exchange builder would leave the existing private writer vulnerable and would produce different validation between roots.

## Required outcome

Define shared, reject-not-sanitize validators for every dynamic identity used as an artifact path component and apply them before any state row, branch, directory, prompt-variable name, or provider run is created.

## Portable identity contract

- Task ids retain the existing lowercase ASCII vocabulary/length but may not end in `.` and may not be a Windows device name, including a reserved device stem followed by an extension.
- Flow node ids match one bounded lowercase token grammar compatible with both one path component and `_VAR_RE`, for example `[a-z0-9][a-z0-9_-]{0,63}`; they also reject Windows device names.
- Provider, output-slot, and fixed artifact names remain enums/constants. Run/attempt/subtask ids remain bounded integers rendered by fixed formatters.
- Validation is host-independent: a name rejected for native Windows portability is rejected on macOS/Linux too, so the same tracked task/flow behaves on every supported OS.
- Path builders still resolve/check containment after joining. Identity validation is defense in depth, not a replacement for containment and reparse checks.

## In scope

- Add leaf helpers for portable path segments and Windows reserved-name detection without importing CLI/Core layers.
- Strengthen `is_valid_task_id`/the task validation gate and update its error message/tests.
- Validate every flow node kind's id during `load_flow` before building maps or paths; keep the existing reserved prompt-variable collision checks.
- Audit all dynamic artifact path/filename builders in private and exchange roots and add containment assertions at their write boundaries.
- Validate case-folded uniqueness where identities share a directory, even though the canonical grammar is lowercase, and reject Unicode/normalization drift rather than normalizing it.
- Update task/flow authoring docs and shipped skills/examples.

## Acceptance criteria

- [ ] `../x`, `a/b`, `a\b`, drive/UNC/absolute forms, empty/overlong ids, trailing dot, and Windows device names are rejected identically on Windows, macOS, and Linux fixtures.
- [ ] Every flow node kind is validated; an invalid node id fails flow load before any artifact directory or DB run row is created.
- [ ] Every accepted agent/tool node id forms a token the prompt renderer can actually substitute as `{<node-id>_path}`.
- [ ] Private and exchange builders refuse a resolved path outside their expected task/run root even if a caller bypasses the identity validator.
- [ ] All packaged flows and valid existing task fixtures still pass; incompatible custom ids receive a precise upgrade error rather than silent sanitization.
- [ ] Windows device-name tests include `con`, `con.txt`, `prn`, `aux`, `nul`, `com1`–`com9`, and `lpt1`–`lpt9`, case-insensitively.

## Verification

- Table-driven task/node identity tests on every platform-injected flavor.
- Native Windows path-construction tests plus POSIX regression tests.
- Flow-load → node-run-dir tests proving rejection happens before filesystem writes.
- Containment tests against absolute, parent traversal, separators, drive-relative forms, UNC, and case-fold collisions.
- Packaged-flow validation and task parser/validation suites.

## Out of scope

- Encoding arbitrary ids into filesystem-safe names; the repository policy is reject, do not sanitize.
- General Git branch/ref validation beyond the task-id-derived portability effect.
- Exchange publication/content policy (WRI-001).

## Likely implementation areas

- src/wastech_orchestrator/task/model.py and validation_gate.py
- src/wastech_orchestrator/core/flow/snapshot.py and validator.py
- src/wastech_orchestrator/providers/artifacts.py
- the WRI-001 exchange path builders
- tests/task/, tests/flow/, tests/providers/
- docs/configuration.md and packaged flow/task authoring guide/skills
