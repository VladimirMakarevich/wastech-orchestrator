---
name: simplify-task
description: Before writing code, at the requirements/design stage, find the simplest sound approach for the agreed task. Scrutinizes the proposed technical plan against the codebase for unrequested complexity (speculative generality, premature abstraction/config, extra layers) and shapes it for code quality and maintainability via clean seams — extensibility through simplicity, not speculation (YAGNI). Analysis-only — recommends an approach and trade-offs, writes no code. Use after the task is understood and before implementation. Run /clarify-task first if the goal or scope is still unclear.
---

# simplify-task

This skill runs **before** implementation, at the requirements / design stage, once the task is understood. Its job: land on the **simplest sound approach** for the agreed task — cut complexity nobody asked for _before_ it gets written, and shape the design so the resulting code stays high-quality, maintainable, and extensible.

It is the front-end twin of `/simplify-review` (which checks the same things _after_ the diff exists). Know the boundaries:

- `/clarify-task` agrees with the user on **WHAT** to do — goal, scope, boundaries — in plain language. Run it first if the goal or scope is still fuzzy.
- `simplify-task` takes the agreed scope and scrutinizes **HOW** — the technical approach — against the codebase, in engineering terms.
- It writes **no code**: it recommends an approach, names the complexity to avoid, and surfaces trade-offs.

## When to use

- After the task is understood and before you start implementing.
- When you (or a plan) already have an approach and want to check it isn't over-built.
- When the task tempts you toward "future-proof" abstractions, configuration, or layers.

For a tiny, obvious change — skip it and just implement.

## Inputs

- **The agreed requirements** — request + conversation + the linked task/backlog doc.
- **The proposed approach/plan, if one exists.** If none exists yet, derive the simplest sound one from the requirements + codebase.
- **The codebase** — existing patterns, the Functional Map (`docs/functional/`), `.agents/rules/`, the hard invariants. Reuse what's there before adding anything new.

## How to run

1. **Restate the agreed scope** in a line or two so the target is fixed.
2. **Survey the codebase** for existing building blocks that already cover part of the task — prefer reuse over new structure.
3. **Sketch the simplest approach** that satisfies the requirements and the rules.
4. **Run the plan through the smells checklist** below — for each piece of structure ask: "does the agreed task need this _now_?"
5. **Find the few real seams** where known, concrete extension is likely, and plan to keep those clean — without building the extension now.
6. **Produce the recommendation + trade-offs.** Write no code.

## What to look for (complexity to avoid up front)

Same smells as `/simplify-review`, applied to the _plan_ instead of the diff:

- **YAGNI / speculative generality** — interfaces, generics, plugin/hook points for futures not in the task. One implementation needs no abstraction.
- **Premature configuration** — flags, options, settings that nothing will actually vary.
- **Back-compat / migration machinery** — versioning, `v1`→`v2`, migration or deprecation shims. Greenfield MVP, no deployment — don't plan it unless explicitly asked.
- **Extra layers / indirection** — managers, factories, wrappers that don't earn their hop.
- **Over-broad error handling** — planning to handle failures the task doesn't own. (Infra-only fallback is mandated design, not gold-plating.)
- **Bigger data model / API than needed** — fields, parameters, endpoints, return shapes beyond the requirement.
- **Premature optimization** — caching, pooling, or indexing the task didn't call for.

## Maintainability & extensibility — through simplicity, not speculation

You asked for maintainability and extensibility; here is how to get them **without** adding the very complexity `/simplify-review` will later flag:

- The most maintainable and extensible code is usually the **simplest** code with **clear seams and honest names** — not code pre-loaded with abstraction for hypothetical futures.
- Add an extension point only when a **concrete, known** requirement needs it. Otherwise keep it simple and refactor when the _second real_ case arrives (rule of three).
- "We might need it later" is **not** a reason to add it now.
- Favour small focused functions, narrow interfaces, single responsibility, and the existing repo patterns. These raise quality _and_ extensibility at zero speculative cost.
- Respect mandated structure: the provider abstraction, orchestrator-only commit/push/PR, infra-only fallback, the security policy, the state machine — these are required by `.agents/rules/` and are part of the simplest _correct_ design, not over-engineering.

## Output

Concisely, in the user's language:

- **Recommended approach** — the simplest sound plan, in a few steps; what to reuse from the codebase.
- **Complexity to avoid** — what the plan was (or could be) tempted to add but shouldn't. Each: what it is, why it isn't needed now, the principle (KISS / YAGNI).
- **Seams to keep clean** — the few places where known, concrete extension is likely, and how to leave them clean _without_ building them now.
- **Trade-offs / decisions for the user** — where simplicity vs. flexibility (or scope) is genuinely the user's call, ask briefly with options.
- **Open questions** — anything still unclear about scope. If it's a WHAT question, route back to `/clarify-task`.

Then **stop**. Don't write code — hand the agreed simplest approach to implementation.

## What not to do

- Don't redesign the product or re-open settled scope — that's `/clarify-task`.
- Don't write or scaffold code — this is the design stage.
- Don't add abstraction, config, or layers "to be safe"; default to the simplest thing that works.
- Don't strip complexity that the rules, correctness, or security require.
- Don't confuse extensibility with speculation — clean seams yes, premature frameworks no.
