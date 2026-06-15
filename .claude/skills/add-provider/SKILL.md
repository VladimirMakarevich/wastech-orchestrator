---
name: add-provider
description: Create a new coding-agent adapter (AgentProvider) for wastech-orchestrator following the contract in providers/base.py. Use when adding a Codex/Claude Code adapter or another CLI provider.
---

# add-provider

Scaffold a new provider adapter strictly according to the contract.

## Before you start

Read:

- `src/wastech_orchestrator/providers/base.py` — the `AgentProvider` contract, the request/result structures, and the error classes;
- `orchestrator_final_plan.md` §4.3, §4.4, §7 — the adapter's responsibilities and error normalization;
- `docs/rules/architecture.md` and `docs/rules/security.md` — the invariants.

## Steps

1. Create the module `src/wastech_orchestrator/providers/<provider>.py` with a class implementing `AgentProvider`:
   - `id` = canonical identifier (`codex` / `claude`);
   - `preflight()` → `ProviderHealth` (executable, version, authentication, required capabilities; message free of secrets);
   - `run(request)` → `AgentRunResult` (or `ProviderError` with the correct `ErrorClass` on an infrastructure failure).
2. Building the CLI call:
   - an **argument list**, without `shell=True` and without interpolating user-supplied strings;
   - a mandatory timeout;
   - the sandbox/permission profile from the request, **without** any bypass options;
   - pass only allowlisted env (see security.md).
3. Normalization:
   - exit code and events → `RunStatus` / `ErrorClass`;
   - structured output (JSONL / stream-json) → `structured_output`;
   - stdout/stderr/event log → artifact paths (spec §10), redacted request artifact.
4. **Forbidden** in the adapter: fallback, changing the state machine, commit/push/PR.
5. Tests (see docs/rules/testing.md):
   - unit: command builder, output parsing, error classification;
   - integration: a fake CLI executable for the success/timeout/crash/malformed/auth-fail scenarios.
6. Run `/run-checks`.

## Definition of Done

- the class passes `isinstance(obj, AgentProvider)` (Protocol runtime-checkable);
- all infrastructure failures return the correct `ErrorClass`;
- no secrets in logs/artifacts;
- green ruff/mypy/pytest.
