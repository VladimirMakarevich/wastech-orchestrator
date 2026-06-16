---
name: fake-cli
description: Scaffold deterministic fake CLI executables and pytest fixtures that stand in for the real Codex/Claude binaries in provider integration tests. Use when adding integration coverage for a provider adapter, the router, or the pipeline (phases 2–5) — success and every infrastructure failure scenario.
---

# fake-cli

Create stub "CLI" executables and the pytest plumbing that point a provider's `command` at them, so integration tests exercise the adapters/router/pipeline without the real Codex or Claude — no network, fully deterministic ([testing.md](../../../.agents/rules/testing.md)).

## Scenarios to cover

Map each to a normalized `ErrorClass` (spec §7.1) so the adapter's classification is tested:

- **success** — exit 0, well-formed structured output (JSONL for Codex, `stream-json` for Claude);
- **binary_not_found** — point `command` at a non-existent path;
- **authentication_failed**, **rate_limited**, **provider_unavailable** — exit non-zero with the provider's stderr signature for that class;
- **timeout** — sleep past `timeout_seconds` (use a small timeout in the test);
- **process_crashed** — abort / non-zero crash signal;
- **invalid_output** — emit malformed/partial structured output;
- **infra error after files changed** — write/modify a file in `cwd`, _then_ fail with an infra class (for the §7.4 partial-change + fallback path, used from phase 4).

## Steps

1. **Stub body — one Python script, OS-independent.** Put the logic in a `fake_agent.py` that reads the desired scenario (and any canned stdout/exit code) from an **environment variable** or an argument the test sets, then writes the canned stdout/stderr, optionally touches a file or sleeps, and exits with the chosen code. Keep it deterministic — no real time/random; bound any sleep.
2. **Launcher named like the CLI.** The provider builds `[command, "exec"|"-p", ...]`, so `command` must be a runnable executable named `codex`/`claude`:
   - **Windows (this repo's platform):** write `codex.cmd` / `claude.cmd` containing `@python "%~dp0fake_agent.py" %*`.
   - **POSIX:** write `codex` / `claude` with a `#!/usr/bin/env python3` shebang (or a `sh` wrapper) and set the executable bit. Generate the right launcher for the current OS in a fixture; place it in a `tmp_path` dir and pass that path as the provider's `command` (or prepend the dir to `PATH`).
3. **Pytest fixtures (`tests/conftest.py`, `tests/fakes/`).** A fixture builds the launcher+stub for a requested scenario and returns the `command` path. **Parametrize by provider** (`codex`, `claude`) so the same scenario matrix runs against both adapters and proves interchangeability.
4. **Assert** the adapter returns the right `RunStatus`/`ErrorClass`, writes the §10 artifacts (request/stdout/stderr/events/result), and — from phase 4 — that the router falls back only on infra classes and surfaces quality failures without a switch.

## Rules

- Deterministic and isolated: **no network, no real CLI**, time/sleep bounded or injected.
- Cross-platform: the fixture must work on Windows and POSIX (generate the matching launcher).
- The stub must be able to emit the provider-shaped event stream (JSONL vs. `stream-json`).
- Never bake a real secret into a stub or its output; reuse the redaction tests' fake tokens.

## Definition of Done

- A reusable, provider-parametrized fixture produces a runnable fake CLI per scenario.
- Every scenario above is covered for both providers and asserts the expected `ErrorClass`/artifacts.
- The suite passes on Windows; `/run-checks` green.
