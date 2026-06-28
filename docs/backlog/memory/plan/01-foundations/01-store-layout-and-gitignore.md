# 01.1 — Store layout & gitignore

[phase](index.md) · [design §3](../../design.md) · [acceptance: AC-S1/S2](../../acceptance-criteria.md)

**Goal:** create the canonical `.worc/memory/` directory shape and make sure it is never committed.

## Scope

In: the directory scaffolding helper, a `manifest.json` (schema version + tier caps), and install-time gitignore seeding. Out: any record writing (task 03), config keys (task 02).

## Approach

- Add a small helper that resolves and (lazily) creates the `.worc/memory/` tree: `long_term/`, `short_term/`, `entities/`, `audit/`, `quarantine/`, `derived/` (see design §3). Task-independent — **not** via `task_artifact_dir`.
- Write `manifest.json` (memory schema version, tier caps/TTLs placeholders, created-at) atomically.
- Seed `.worc/memory/` into the install-time `.gitignore` writing (alongside `state.db`/`logs/`). Per-task packets under `logs/<task-id>/memory/` follow the existing logs ignore.
- All paths via `pathlib`; any stored path string normalized with `as_posix()`.

## Files

- `src/wastech_orchestrator/...` new `memory/paths.py` (or similar) for layout resolution.
- Install/gitignore seeding site (where `state.db`/`logs/` are currently ignored).

## Tests

- Tree is created idempotently; re-run does not clobber existing files.
- `.worc/memory/` matches a gitignore rule after install (no accidental tracking).
- Path strings round-trip identically on Windows and POSIX (AC-X1).

## Done when

AC-S1 (tree populated on demand) and AC-S2 (never committed; install seeds ignore) hold; suite green.
