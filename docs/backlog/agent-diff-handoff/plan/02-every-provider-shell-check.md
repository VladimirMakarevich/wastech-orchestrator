# Phase 02 — Every-provider shell check

- **Status:** ☐
- **Depends on:** none
- **Delivers:** a provider-neutral answer to "will this attempt have a shell on whichever provider serves it", fail-closed toward **no**.

## Goal

Add the question D-3 needs, beside the existing one it must not replace. Moves toward AC-5 and AC-6.

## Steps

1. In `src/wastech_orchestrator/security/shell_reach.py`, add `every_provider_grants_shell(providers, configs, checks, query) -> bool`: true only when the provider list is non-empty and every provider has a config, a bound check, and a check that returns true; any missing piece returns false. Its docstring states why its default is the opposite of `any_provider_grants_shell`'s.
2. In `src/wastech_orchestrator/routing/router.py`, add `AgentRouter.route_always_grants_shell(route, *, permission_profile, git_evidence)`, building the same provider list and `ShellQuery` as `route_grants_shell` and delegating to the new function.
3. Extend `RouterPort` in `src/wastech_orchestrator/core/flow/nodes/base.py` (the core's view of the router, beside `route_grants_shell`) with the new method, and every fake router used by the node-runner tests.

## Files touched

- `src/wastech_orchestrator/security/shell_reach.py`
- `src/wastech_orchestrator/routing/router.py`
- `src/wastech_orchestrator/core/flow/nodes/base.py` (`RouterPort`)
- `tests/security/test_shell_reach.py`
- `tests/routing/` (router test for the new method)

## Invariants in play

- The core learns no provider flag: the per-provider answers stay in the adapters' `attempt_has_shell`, bound by the composition root.
- `route_grants_shell` and the detection brackets that use it are not touched.

## Tests

- All providers say yes → true; one says no → false; a missing config or a missing check → false; an empty list → false.
- The same inputs through `any_provider_grants_shell` → true where the new one is false (the contrast AC-6 names).
- Router: Codex-only route → true; Claude `read-only` without `git_evidence` under `strict_isolation: true` → false; Codex primary with Claude `read-only` fallback → false.

## Docs to sync in this phase

None — nothing calls it yet.

## Acceptance for this phase

- [ ] AC-6 holds at the function and router level.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green.
