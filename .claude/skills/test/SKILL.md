---
name: test
description: Run and diagnose the wastech-orchestrator pytest suite during development — full, targeted, or last-failed — to keep the suite green and find bugs while implementing. Use while writing/changing code (the dev test loop). For the full pre-commit gate (ruff + mypy + pytest) use /run-checks instead.
---

# test

Run the project's automated tests to keep a working state and surface bugs _while_ developing. This is the fast inner loop; `/run-checks` is the full quality gate (ruff + format + mypy + pytest) you run before a commit, a PR, or a phase transition. The source of truth for _what_ to test is [docs/rules/testing.md](../../../docs/rules/testing.md) (spec §14).

Argument (optional): a path, a `-k` expression, or a flag — e.g. `tests/config`, `-k footprint`, `--lf`, `--cov`. With no argument, run the whole suite.

## Steps

1. Ensure the package is installed (`pip install -e ".[dev]"`); install if imports fail.
2. Run the relevant tests with `python -m pytest`:
   - **everything:** `python -m pytest -q`
   - **a slice** (one subpackage/file): `python -m pytest tests/config -q` / `python -m pytest tests/config/test_validation.py -q`
   - **by name:** `python -m pytest -k "footprint or legacy" -q`
   - **only what failed last** (fast iteration): `python -m pytest --lf -q`
   - **stop at first failure** while bisecting: `python -m pytest -x -q`
   - **coverage of critical paths** (asked for, or to spot gaps): `python -m pytest --cov=wastech_orchestrator --cov-report=term-missing -q`
3. On a failure: show the failing test as `file:line` with a one-line cause; fix the **root cause** (in the code, or in the test if the test was wrong), then re-run just that slice, and finally the whole suite. Never weaken an assertion or `xfail`/`skip` a test just to go green.
4. When you add or change behavior, add/adjust a test at the right level (below) in the same change.
5. End with a short verdict: passed/failed counts. Only say the suite is green when `pytest` is green.

## Test levels (from testing.md — match the code under test)

- **Unit** — pure logic, no external processes: config/task/route validation, error classification, state-machine transitions, redaction, path normalization, loop/fix-cycle limits, the §19 gate, `init` idempotency, footprint rules. Most tests live here.
- **Integration** — use **fake CLI executables** via the `/fake-cli` skill, never the real Codex/Claude: a success run, each infra error (`binary_not_found`, `authentication_failed`, `rate_limited`, `timeout`, `process_crashed`, malformed output), an infra error after files changed, a successful fallback, and fallback forbidden on a quality failure.
- **End-to-end** — on a temporary Git repo: refinement skip, planning/implementation/review, fixing on failed checks, exactly one commit/push/PR, restart without duplicate publishing, ledger record per terminal transition, decomposition, quarantine of broken tasks, footprint modes.

## Rules

- Deterministic and isolated: **no network and no real CLIs** in unit/integration; mock/inject time and external processes.
- A behavior change without a test is incomplete (testing.md). A missing test for new behavior is a gap.
- Don't chase a coverage percentage for its own sake — cover the critical paths (router, fallback, state machine, security/redaction).
- A green `pytest` is mandatory before committing and before moving between implementation stages.
