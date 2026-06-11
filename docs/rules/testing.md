# Testing rules

The source of truth is [orchestrator_final_plan.md §14](../../orchestrator_final_plan.md).

## Levels

### Unit
Cover pure logic without external processes:
- configuration and task override validation;
- route resolution and the allowlist;
- each provider's command builder (without actually running the CLI);
- parsing of structured output;
- error classification (`ProviderError` → class);
- state machine transitions;
- secret redaction and path normalization;
- retry / fallback / fix-cycle limits.

### Integration
Use **fake CLI executables** (stub scripts) rather than the real Codex/Claude:
- a successful run;
- `binary_not_found`, `authentication_failed`, `rate_limited`, `timeout`, `process_crashed`, malformed output;
- an infrastructure error **after** files have been changed;
- a successful fallback;
- fallback being forbidden on a quality failure.

### End-to-end
On a temporary Git repository:
- Claude performs planning/implementation, Codex performs review;
- failed checks trigger `fixing`;
- success → exactly one commit, push, and PR;
- a restart does not duplicate publishing;
- exhausting attempts → `failed`.

## Principles

- Tests are deterministic and isolated (no network calls and no real CLIs in unit/integration).
- External processes and time are mocked/injected.
- Every behavior change is accompanied by a test.
- The goal is high coverage of critical paths (router, fallback, state machine, security/redaction), not a percentage for its own sake.
- A green `pytest` is a mandatory precondition for committing and for transitioning between implementation stages.
