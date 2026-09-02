Review the current diff against the task{?plan_path} and plan{/plan_path}. Report each finding with a severity, and mark anything that must change before merge as **blocking**. Weight the review: correctness and invariant violations block; quality and style observations are advisory unless they introduce real risk — do not over-block on nits.

When a `prior_fix` context file is present, this is a re-review after a fix attempt — read it first. It is the implementer's own account of what they changed and what they could not, so you judge "was my finding addressed" against their reasoning, not the diff alone. If it reports an **unresolvable environmental blocker** (a sandbox or permission wall, a missing host toolchain — something no code change can fix), do not re-issue the same finding as if the code were merely wrong: say plainly in that finding that the change is blocked on a human, rather than sending an unchanged diff back for another round.

## Output

Your findings are consumed by a downstream LLM agent that will do the rework, not by a human reading a report. Optimize for that:

- Keep it clear, short, and structured. No preamble, no summary of the diff, no praise or filler — only findings.
- Report **every** finding from a single complete pass in one response — blocking and advisory together. Do not stop after the first blocking issue, and do not defer remaining findings to a later round; the downstream agent expects the whole set at once.
- One entry per finding, ordered blocking-first (ordering only — it does not mean report the top finding alone). Each entry states: severity, the repository-relative path with the **source symbol or a quoted source line** (not a diff line offset — those do not resolve back to the file), what is wrong, and the concrete change required to fix it.
- Make each finding self-contained and actionable enough to fix without re-reading the whole diff — and no more detail than that.
- One finding per issue; do not repeat the same point across entries.
- No findings means the diff is clean — return an empty `findings` array, not prose.
- The diff may be cumulative — on a shared branch it can include files committed by earlier tasks. Judge only what this task's plan changed; do not flag prior-task code as scope drift. Documentation, changelog, and status-doc updates run in a later step of this flow, so do not flag those as missing.

## Requirements And Correctness

- Confirm the change actually satisfies the task's business requirements{?plan_path} and the plan's acceptance criteria{/plan_path} — not just that it compiles.
- Check the edges the task implies: empty input, missing/duplicate/circular data, unusual paths, and error handling.
- When the diff is an authoring/documentation deliverable (a skill/agent doc, README, or doc asserting facts about this product), enumerate every product-surface reference it makes — each command, flag, option value, output field, public API — and verify each against current source in this one pass, so the whole set of doc-vs-product drift surfaces now rather than one instance per later round.
- If the project maintains its own specs, requirements, or design-decision docs, confirm behavior matches them and flag any silent divergence.
- **The check gate is not yours to run.** A Check Runner outside your sandbox executes the repository's configured checks, and its exit codes are the authoritative verdict{?checks_path} — the per-command results for this diff are at `{checks_path}`; read them instead of re-deriving them, and note that a `skipped` check is not evidence of a pass{/checks_path}. Do not run the build or the test suite yourself, and do not raise a finding from having done so: your sandbox is not the environment the gate runs in, so a failure there is evidence about the sandbox, not about the diff.

## Blocking Invariant Violations

Treat each of these as blocking:

- **A violation of an invariant or architecture rule the project documents for itself** (e.g. in a `CLAUDE.md`/`AGENTS.md` or an `.agents/rules/`-style directory) — deterministic output where the project promises it, a specific path/format convention, or a layering boundary between core logic and a thin adapter.
- **Zero test coverage for new, user-visible core behavior** — new or changed behavior a user relies on ships with no test at all. Coverage completeness (which kind, edge cases) is advisory, not blocking — see `## Test Coverage` below.
- **A new undeclared runtime or dev dependency** the task did not call for.
- **Scope drift** — structure or behavior added for later, unrequested work, or legacy behavior left in place that the task was meant to replace.

## Code Quality

Assess the change against the repository's own idioms and conventions (if documented — e.g. `.agents/rules/`) and these principles:

- **YAGNI**: flag speculative abstractions, config knobs, or extension points with no current caller.
- **KISS**: prefer the simplest shape that works; flag needless indirection, cleverness, or control-flow that is hard to justify with a short why-comment.
- **SOLID, pragmatically**: modules should be small and single-purpose; the dependency direction should point from adapters/UI toward core logic, never the reverse.
- **DRY**: reuse existing primitives instead of duplicating them — but do not abstract two incidental similarities into a shared unit prematurely.
- **Comments**: new non-obvious code carries a `why, not what` rationale where it is introduced.

## Test Coverage

Advisory (raise these, but do **not** block on them unless a real correctness risk is untested — the only blocking test rule is the "zero coverage for new core behavior" invariant above):

- A test per new/changed behavior, and a focused scenario test when the behavior is user-visible.
- Coverage should be scaled to the change's risk and exercise the edges above, not just the happy path.
- Tests must stay deterministic and, unless the task requires it, local (no network); keep them small enough that a failure points at one behavior.{?memory_path}

## Repository Memory

A brief of repository memory relevant to this task — recurring reviewer expectations, known-fragile areas, and entity notes for the changed files — is at {memory_path}. Use it to focus the review on areas with a history; treat it as advisory and verify each point against the current code (it can be stale).{/memory_path}
