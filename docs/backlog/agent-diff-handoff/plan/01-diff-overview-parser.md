# Phase 01 — Diff overview parser

- **Status:** ☑
- **Depends on:** none
- **Delivers:** one pure module that knows what a unified diff touched, per file, and renders the bounded overview text; the supervisor packet uses it and its output does not change.

## Goal

Give the project a single parser for "what a diff touched" (design D-2) and the overview renderer with its bound (D-8), with no caller yet changing behavior. Moves toward AC-1, AC-E4, AC-E5, AC-N1, AC-N3.

## Steps

1. Create `src/wastech_orchestrator/diff_overview.py` (stdlib only, no I/O): a frozen `DiffEntry` (`path`, `previous_path`, `kind` in `A`/`M`/`D`/`R`, `added`, `removed`) and `parse_diff(diff_text) -> tuple[DiffEntry, ...]`, reading `diff --git`, `new file mode`, `deleted file mode`, `rename from`/`rename to`, `---`/`+++` and hunk lines; a mode-only change is `M` with `+0 −0`.
2. Add `OverviewSection` (title, base SHA, entries, denied set) and `render_overview(sections, *, diff_size) -> str`: the header line, one line per entry (`R old → new` for renames, `[denied]` suffix), `MAX_LISTED_PATHS = 200` per section with the exact `… and N more paths (+A −R) not listed` line; `\n` newlines; POSIX paths as given by git.
3. Reimplement `summarize_diff` in `src/wastech_orchestrator/core/supervisor_packet.py` as a fold over `parse_diff`, keeping `DiffSummary` and its first-seen path order.
4. Run `lint-imports` to confirm both `git_manager.py` and `core/` may import the new root module.

## Files touched

- `src/wastech_orchestrator/diff_overview.py` (new)
- `src/wastech_orchestrator/core/supervisor_packet.py`
- `tests/test_diff_overview.py` (new)
- `tests/core/test_supervisor_packet.py`

## Invariants in play

- Pure and deterministic: the same text yields the same bytes (NFR-3).
- Cross-platform: no `os.sep`, no platform newline; paths are passed through as git's POSIX strings.
- No secret handling here — the module never sees anything but the already-redacted diff text.

## Tests

- Kinds A / M / D / R, mode-only change, a hunk line that begins with `---` or `+++`, an empty diff.
- The 200-path bound with 250 entries: 200 lines plus the truncation line, totals exact.
- Byte-identical render twice; identical render for the same input on a Windows-style fixture.
- The existing `summarize_diff` tests pass unchanged; a new test asserts its totals equal the sum of `parse_diff` entries.

## Docs to sync in this phase

None — no behavior an operator or agent can see changes yet.

## Acceptance for this phase

- [ ] `parse_diff` and `render_overview` are covered for every kind, the bound and determinism.
- [ ] The supervisor packet's output for the existing fixtures is unchanged.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green.
