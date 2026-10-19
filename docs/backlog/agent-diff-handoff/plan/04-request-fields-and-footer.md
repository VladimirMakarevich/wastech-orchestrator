# Phase 04 — Request fields and footer

- **Status:** ☐
- **Depends on:** none
- **Delivers:** the provider-side half of D-4 and D-7 — a request can carry the overview, the base and the guidance sentence, the footer renders them, containment and `request.json` cover them, and a partial-diff fallback clears them. Nothing sets them yet.

## Goal

Land the neutral seam so that the core wiring in 05 only has to fill fields. Moves toward AC-3, AC-4, AC-E1, AC-N5.

## Steps

1. Add `change_overview_path: str | None = None`, `diff_base: str | None = None` and `change_guidance: str | None = None` to `AgentRunRequest` in `src/wastech_orchestrator/providers/base.py`, with a comment in the style of `security_preamble`: core-built, carried here, secret-free by contract.
2. In `build_context_footer`, list `("change overview", request.change_overview_path)` immediately before `("diff", request.diff_path)`; after the list, append `Change base: <sha>` when `diff_base` is set and the `change_guidance` text when set. A request with none of the three renders today's footer byte-for-byte.
3. Add `change_overview_path` to `assert_orchestration_paths_contained` in `src/wastech_orchestrator/providers/exchange.py`.
4. Add `change_overview_path` and `diff_base` to `context_paths` in `_request_representation` (`src/wastech_orchestrator/providers/_adapter_base.py`).
5. In `src/wastech_orchestrator/routing/router.py` `_build_request`, when a `PartialChange` substitutes `diff_path`, also set the three new fields to `None`.

## Files touched

- `src/wastech_orchestrator/providers/base.py`
- `src/wastech_orchestrator/providers/exchange.py`
- `src/wastech_orchestrator/providers/_adapter_base.py`
- `src/wastech_orchestrator/routing/router.py`
- `tests/test_providers_base.py`
- `tests/providers/` (containment, `request.json`)
- `tests/routing/` (partial-diff substitution)

## Invariants in play

- Providers stay a leaf: no import of `core` or `routing` from `providers/` (`lint-imports`).
- No argv change in any adapter — the text travels in the stdin prompt only.
- The router learns no core text; it only clears fields.

## Tests

- Footer: with all three fields, exact order and text; with none, byte-identical to the current footer for every existing fixture.
- Containment: an overview path outside the exchange raises `ExchangeError`.
- `request.json` carries both new keys only when set.
- Router: a `PartialChange` yields a request with the partial `diff_path` and the three fields `None` (AC-E1).
- Claude and Codex argv snapshots unchanged for a request carrying the new fields.

## Docs to sync in this phase

None — nothing sets the fields yet.

## Acceptance for this phase

- [ ] AC-E1 holds; the no-field footer is byte-identical; adapter argv unchanged.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green.
