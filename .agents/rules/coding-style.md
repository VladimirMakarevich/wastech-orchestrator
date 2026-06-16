# Code style rules

## Language and tooling

- **Python 3.12+**.
- Formatting and linting: **ruff** (`ruff check .`, `ruff format`).
- Types: **mypy** in strict mode for `src/`. The entire public API is annotated.
- Tests: **pytest**.

## General principles

- Small, focused functions and modules with a single responsibility.
- **Keep it simple (KISS), no unrequested complexity.** Build the simplest thing that satisfies the task and these rules; don't add abstractions, configuration, or layers for hypothetical futures.
- **Add only what the task needs (YAGNI); extensibility through simplicity.** Reuse existing building blocks before adding new ones. The most maintainable code is simple code with clean seams and honest names, not premature abstraction — add an extension point only when a concrete, known requirement needs it.
- Explicit is better than implicit: no "magic" global state.
- No side effects at module import time.
- Errors are surfaced through typed exceptions/results, not "bare" strings.

## Structure

- Package: `src/wastech_orchestrator/`, src-layout.
- One component = one module/subpackage (`providers/`, `git_manager.py`, `state_store.py`, …).
- Data contracts are `dataclasses` (or Pydantic if input validation is needed). The provider contract is a `typing.Protocol`.

## Naming

- `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- Provider and stage names come strictly from the canonical list (see [architecture.md](architecture.md)); define an enum rather than scattering string literals through the code.

## Processes and subprocess

- Run external CLIs with an **argument list** (`subprocess.run([...])`), without `shell=True` and without interpolating user-supplied strings.
- Timeouts are mandatory for all external calls.
- stdout/stderr are written to artifacts (see spec §10), not discarded.

## Logging

- Standard `logging`, structured (task_id, stage, attempt, provider).
- **Never** log secrets, tokens, or the full process environment.

## In-code documentation

- Public functions/classes have a docstring: what it does, the input/output contract, and which exceptions it raises.
- Comments belong only where they explain "why", not "what".
