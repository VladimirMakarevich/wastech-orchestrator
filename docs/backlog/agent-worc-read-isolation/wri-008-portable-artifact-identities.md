# WRI-008 — Validate portable artifact path identities

**Status:** implemented **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** —

## Problem

The current task-id regex allows values that are not portable Windows path components, including device names such as `con`, `nul`, `com1`, and a trailing dot. More critically, flow node ids are converted to strings but receive no path-segment validation before they are joined into `node_run_dir`, `node_history_path`, provider-attempt directories, generic `<node-id>.out.md`, and prompt-audit file names. A custom node id containing `/`, `\`, `..`, an absolute/drive prefix, or an unbounded value can escape or corrupt the artifact layout. A dot in a node id also cannot participate in the renderer's documented `{<node-id>_path}` token grammar.

WRI-001 would copy the same unchecked identities into a second security-sensitive root. Fixing only the new exchange builder would leave the existing private writer vulnerable and would produce different validation between roots.

## Required outcome

Define shared, reject-not-sanitize validators for every dynamic identity used as an artifact path component and apply them before any state row, branch, directory, prompt-variable name, or provider run is created.

## Gap handed over by WRI-001 (decoupled build) — resolved

WRI-001 shipped **before** this task, so its exchange builders and publisher initially trusted the ids they were handed and carried only a local, exchange-specific safety net. WRI-008 has consolidated this:

- The shared segment-grammar validators now live in `security/identifiers.py` (`is_valid_task_id`, `is_valid_node_id`, `is_portable_path_segment`, `is_windows_reserved_name`) — a stdlib-only leaf that `providers`, `core`, and `task` all import (so the same rules apply in every root without breaking `providers-are-leaf`). Task ids are validated at the §19 gate and node ids at flow load, so every id reaching the private or exchange builders is already portable; `task.model` re-exports the task-id validators for its long-standing import site.
- The publisher `publish_to_exchange` in `providers/exchange.py` now delegates its per-segment relpath grammar to the shared `is_portable_path_segment` (dropping the duplicated `..`/backslash/drive/`:` checks) and keeps only the exchange-specific parts: the empty/absolute whole-string checks, and the symlink/reparse/hard-link/NTFS-ADS refusal plus case-fold/NFC sibling collision in `build_exchange_manifest`.
- Containment is now one shared helper, `providers.artifacts.assert_contained_path` (raising `PathIdentityError`): the private write boundary (`create_attempt_dir`) calls it, and the exchange's `_assert_contained` delegates to it (re-raising as `ExchangeError`), so there is a single containment belt across both roots rather than a second one.

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

- [x] `../x`, `a/b`, `a\b`, drive/UNC/absolute forms, empty/overlong ids, trailing dot, and Windows device names are rejected identically on Windows, macOS, and Linux fixtures. (The identity validators are pure and host-independent — no platform seam to inject — so the same string is rejected on every OS; containment covers absolute/parent-traversal escapes.)
- [x] Every flow node kind is validated; an invalid node id fails flow load before any artifact directory or DB run row is created. (`_validate_node_id` runs in all six node parsers during `load_flow`, ahead of the map/graph build and every orchestrator side effect.)
- [x] Every accepted agent/tool node id forms a token the prompt renderer can actually substitute as `{<node-id>_path}`. (Verified against the real renderer token grammar via `referenced_variables`.)
- [x] Private and exchange builders refuse a resolved path outside their expected task/run root even if a caller bypasses the identity validator. (`assert_contained_path` at `create_attempt_dir` and the exchange publisher.)
- [x] All packaged flows and valid existing task fixtures still pass; incompatible custom ids receive a precise upgrade error rather than silent sanitization. (All packaged flow node ids already satisfy the grammar; the gate/flow-load errors name the exact rule.)
- [x] Windows device-name tests include `con`, `con.txt`, `prn`, `aux`, `nul`, `com1`–`com9`, and `lpt1`–`lpt9`, case-insensitively.

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
