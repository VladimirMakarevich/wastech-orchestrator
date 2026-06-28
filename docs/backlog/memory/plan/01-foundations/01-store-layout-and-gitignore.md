# 01.1 — Store layout & gitignore

[phase](index.md) · [design §3](../../design.md) · [acceptance: AC-S1/S2](../../acceptance-criteria.md)

**Goal:** create the canonical `.worc/memory/` directory shape and make sure it is never committed.

## Scope

In: the directory scaffolding helper, a `manifest.json` (schema version + tier caps), and a test that confirms the tree is never committed. Out: any record writing (task 03), config keys (task 02).

## Approach

- Add a small helper that resolves and (lazily) creates the `.worc/memory/` tree: `long_term/`, `short_term/`, `entities/`, `audit/`, `quarantine/`, `derived/` (see design §3). Task-independent — **not** via `task_artifact_dir`.
- Write `manifest.json` (memory schema version, tier caps/TTLs placeholders, created-at) atomically.
- **Gitignore: no new rule needed.** `install` already ignores the **whole** `.worc/` home wholesale (`RUNTIME_GITIGNORE_LINES = (".worc/",)` in `src/wastech_orchestrator/git_manager.py`, applied via `append_runtime_excludes` to `.gitignore` and `.git/info/exclude`), and per-task `logs/` live **under** `.worc/`. So both `.worc/memory/` and the per-task packets under `logs/<task-id>/memory/` are already covered — AC-S2's "install seeds the gitignore entry" is satisfied by the existing `.worc/` line. This task only **verifies** that coverage, it adds no rule.
- All paths via `pathlib`; any stored path string normalized with `as_posix()`.

## Files

- `src/wastech_orchestrator/...` new `memory/paths.py` (or similar) for layout resolution.
- (No gitignore-seeding change — `.worc/` is already wholesale-ignored in `git_manager.py`.)

## Tests

- Tree is created idempotently; re-run does not clobber existing files.
- `.worc/memory/` is matched by the existing `.worc/` gitignore rule after install (no accidental tracking) — assert against `append_runtime_excludes` coverage, not a new entry.
- Path strings round-trip identically on Windows and POSIX (AC-X1).

## Done when

AC-S1 (tree populated on demand) and AC-S2 (never committed; already covered by the `.worc/` ignore) hold; suite green.
