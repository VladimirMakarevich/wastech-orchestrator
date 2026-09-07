---
name: simplify-review
description: After implementation, verify the change is no more complex than the task actually required. Flags unrequested complexity (speculative generality, premature config/abstraction, back-compat/migration scaffolding, over-broad error handling, duplication, dead code) and KISS/DRY violations in the diff. Review-only — reports findings and simpler alternatives, does not edit. Use after finishing a task or stage, before a commit or PR, to catch over-engineering nobody asked for.
---

# simplify-review

This skill verifies, **after** the work is done, that the implementation is no more complex than the task actually required. For every piece of added complexity it asks one question: **"was this explicitly asked for?"** If the answer is no — and it isn't required by correctness, security, or the repo's mandatory rules — it is a candidate for removal.

This is **review-only**: it reports findings and simpler alternatives, it does **not** edit code. It complements two other skills, don't duplicate them:

- `/code-review` hunts for **bugs** — this skill does not.
- `/simplify` **applies** reuse/efficiency/altitude cleanups — this skill only reviews scope and over-engineering and reports.

## When to use

- After finishing an implementation, a stage, or right before a commit / PR.
- When a change feels bigger or cleverer than the request that prompted it.
- When you suspect speculative "future-proofing", extra abstraction layers, or migration/back-compat machinery crept in.

If the change is tiny and obviously scoped, don't run a ceremony — just say so.

## What counts as "explicitly requested"

The baseline you compare the diff against, in order:

1. **The user's request + this conversation** — what was actually asked, in their words.
2. **The linked task / backlog doc** (`docs/backlog/*`, the task spec) if one exists.
3. **The repo's mandatory rules and hard invariants** (`.agents/rules/`, `CLAUDE.md`) and whatever correctness or security genuinely require.

Anything in the diff that you cannot trace back to 1–3 is a candidate finding. When you can't tell whether something was requested, **flag it as an open question — never assume it was wanted**.

## How to run

1. **Get the change set.** `git diff` against the base branch (`main`) plus uncommitted changes. Review **only** changed/added code in the diff.
2. **Re-fix the intended scope.** Re-read the request and the linked task doc so you hold the requested scope clearly in mind before judging.
3. **Walk the diff** and run each added construct through the smells checklist below.
4. **For each finding,** record: location, what it is, why it looks unrequested, the simpler alternative, the principle it violates.
5. **Produce the report + verdict.** Do not edit code.

## What to look for (over-engineering smells)

- **YAGNI / speculative generality** — abstractions, interfaces, generic parameters, plugin/hook points built for a hypothetical future not in the task. A one-implementation interface is the classic tell.
- **Premature configuration** — flags, options, env vars, settings that nothing actually varies; defaults that are never overridden.
- **Back-compat / migration scaffolding** — versioning, `v1`→`v2` cutovers, migration or deprecation shims. This orchestrator is a greenfield MVP with **no deployment** — such machinery is almost always unrequested. Challenge it hard.
- **Unnecessary layers / indirection** — wrappers, factories, managers, adapters that add a hop without adding value.
- **Over-broad error handling** — `try`/`except`, retries, or fallbacks for failures that can't occur or that the task never asked to handle. (Note: infra-only fallback **is** part of this repo's design — don't flag mandated behavior; see the hard invariants.)
- **Duplication (DRY) — flag sparingly** — genuine copy-paste that will drift apart, but only when removing it doesn't add coupling or abstraction. KISS and YAGNI come first (see _How to judge_).
- **Dead / unreachable code** — added but unused, "just in case".
- **Gold-plating** — extra commands, endpoints, output formats, or features beyond the request.
- **Premature optimization** — caching, pooling, or indexing the task didn't call for.
- **Over-parametrization** — many parameters/flags where only one path is ever exercised.

## How to judge (priority: KISS and YAGNI, then DRY)

Priority order: **KISS > YAGNI > DRY**. When they pull in different directions, simplicity and not-building-it-yet win.

- **KISS** — prefer the simplest thing that satisfies the request and the rules. Complexity must **earn its place** by being required, not merely by being possible.
- **DRY — the lowest priority of the three.** Remove real, harmful duplication, but never at the cost of KISS or YAGNI: don't invent an abstraction to dedupe incidental similarity. Forcing DRY often **couples otherwise-independent code and adds indirection** — that complexity is usually worse than a little repetition. Two similar things are not always a pattern; prefer some duplication over the wrong abstraction.
- **Respect mandated complexity.** The provider abstraction, the orchestrator-only publication mandate, infra-only fallback, the security policy, the state machine — these are required by `.agents/rules/` and the hard invariants. They are **not** findings. Read it as a MANDATE, not as a mechanism: a finding that the mechanism does not hold — under `security.strict_isolation: false`, or on a host with no sandbox — is a valid finding, not a violation of this rule.

## Output

Report concisely, in the user's language:

- **Verdict** — one line: is the change as simple as the task needs, or does it carry unrequested complexity?
- **Findings** (ranked, biggest first). Each one:
  - location (`file:line`),
  - what it is,
  - why it looks unrequested — where was it asked for? If nowhere, say so,
  - the simpler alternative,
  - the principle (KISS / YAGNI / DRY).
- **Kept on purpose** — complexity you considered but that is justified (required by the task or the rules). Name it so the user knows you checked, not skipped.
- **Open questions** — anything where you genuinely can't tell if it was requested. Ask; don't assume.

Then **stop**. Do not edit code. If the user wants the simplifications applied, point them to `/simplify` or offer to apply specific findings on request.

## What not to do

- Don't hunt for bugs — that's `/code-review`.
- Don't apply edits — this skill reviews only.
- Don't flag complexity that the task, the rules, or correctness/security genuinely require.
- Don't push DRY so hard that you create premature abstractions.
- Don't pad the report. "No unrequested complexity found" is a valid and good result — say it plainly.
