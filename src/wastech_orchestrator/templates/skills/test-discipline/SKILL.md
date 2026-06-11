---
name: test-discipline
description: Add and run tests for a change in a repository driven by the wastech-orchestrator, using the project's own framework and the orchestrator's configured check commands. Use whenever you change behavior, so the testing stage passes without a fix loop.
---

# test-discipline

The orchestrator runs a separate **testing** stage with the project's configured check commands and
sends any failure back to you in a **fixing** stage. Get the tests right the first time to avoid that
loop.

## Discover, don't impose

- Detect the project's existing test framework and layout (e.g. the test directory, the runner, the
  naming convention) and follow it. Do **not** introduce a new framework or restructure tests.
- The commands that gate your change are the orchestrator's configured checks (typically a test and
  a lint command). Mirror them locally before you finish.

## Steps

1. For every behavior you add or change, add or update a test next to the project's existing ones —
   cover the happy path and the meaningful edge cases, not just the obvious case.
2. If you change existing behavior, update the tests that encoded the old behavior (and explain why
   in your result).
3. Run the project's checks locally (the same test + lint commands the orchestrator will run) and
   make them pass. Fix failures **you** introduced; don't paper over a pre-existing failure — report
   it instead.
4. Keep tests deterministic and isolated — no network, no reliance on wall-clock time or ordering.

## Never

- Never delete or weaken a test just to make the suite green; fix the cause.
- Never disable a lint/type rule to pass; address the underlying issue.
- Never commit — the orchestrator stages and commits the result (see the `safe-change` skill).

## Result handoff

State which tests you added/changed and confirm the project's checks pass locally. If a check fails
for a reason outside your change, say so explicitly so the orchestrator can route it correctly.
