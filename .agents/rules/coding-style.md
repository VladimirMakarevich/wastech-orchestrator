# Code style rules

## Language and tooling

- **Python 3.12+**.
- Formatting and linting: **ruff** (`ruff check .`, `ruff format`).
- Types: **mypy** in strict mode for `src/`. The entire public API is annotated.
- Tests: **pytest**.

## Comments And Rationale

- Treat comments as part of the deliverable: all new code must be documented where it is introduced, not left for a later cleanup pass.
- Follow the rule `why, not what`: comments explain why the code exists, why a constraint matters, and why a specific shape was chosen, not what the syntax already says.
- Prefer self-documenting names over label-comments. A clear name is part of the documentation rule and should replace comments that merely tag a variable, branch, or helper.
- Comment the non-obvious parts: rationale, invariants, tradeoffs, external-system constraints, race conditions, portability traps, and bug-prevention context.
- Do not add comments that merely restate names, types, assignments, loops, or conditionals.
- When behavior is non-obvious, surprising, or constrained by a real limitation, capture that reason next to the relevant code path.
- Comments must be self-contained and independent. Never reference project documents, tickets, ADRs, PRs, backlog items, or other files in a comment (no "see docs/...", "per the ADR", "as described in ...") — those move, get renamed, or are deleted, leaving the comment dangling. State the actual `why` inline so the comment stands on its own without any external artifact.
- Historical narrative in comments is forbidden. Comments document the current design and intent only; do not leave "used to be", "changed from", or compatibility-tombstone commentary behind after a rewrite.
- If a block is hard to justify with a short why-comment, simplify or restructure it until the intent and rationale are clear.

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

## Cross-platform support (Windows / Linux / macOS) — mandatory

Every feature must work on **Windows, Linux, and macOS**. This is a release requirement, not an afterthought: design and test for all three as you build, never "POSIX now, Windows later". Concrete rules (each learned from a real Windows break):

- **Paths via `pathlib.Path`** — never hardcode `/` or `\`, and never assume `os.sep`. When a path is **stored, compared, displayed, or asserted as a string** (audit logs, prompts, persisted state, test assertions), normalize it with `Path.as_posix()` so it is identical on every OS. `Path("a/b")` still opens correctly on Windows, so `as_posix()` is safe for round-tripping.
- **Text files that are committed or byte-compared**: open with `newline=""` (or write bytes) so `\n` is preserved — default text mode rewrites `\n`→`\r\n` on Windows, which corrupts byte-equal comparisons and adds CRLF noise to git-tracked content.
- **Signals and process control are POSIX-shaped — do not assume them on Windows.** `signal.SIGKILL` is absent (guard with `getattr(signal, "SIGKILL", …)`); `os.kill(pid, sig)` opens the target with `OpenProcess(PROCESS_ALL_ACCESS)` and **cannot probe or signal a process the caller holds no handle to**, so cross-process control (a `stop` command signalling a separate daemon) must **not** rely on `os.kill`/signals — prefer OS-neutral coordination (a sentinel file, the daemon's self-managed PID file). A missing PID on Windows raises a bare `OSError` (winerror 87), not `ProcessLookupError`.
- **Branch platform differences explicitly** (`os.name == "nt"` / `sys.platform`) and make the seam injectable so **both** branches are unit-tested on any host (see [testing.md](testing.md)).
- **No POSIX-only filesystem assumptions**: no `/proc`, `/tmp`, `/dev/null`, `fork`, `fcntl`, executable-bit, or symlink dependencies in core paths; use the stdlib cross-platform equivalents (`tempfile`, `os.replace`, `pathlib`). Degrade gracefully where a capability is Linux-only (document the fallback).
- The argv/no-`shell=True` rule above is itself a portability rule — keep it.

## Logging

- Standard `logging`, structured (task_id, stage, attempt, provider).
- **Never** log secrets, tokens, or the full process environment.

## In-code documentation

- Public functions/classes have a docstring that explains the contract and intent: why the API exists, the guarantees and preconditions it relies on, and which exceptions it raises. Do not use docstrings to narrate the implementation step by step.
- Comments belong only where they explain "why", not "what".
