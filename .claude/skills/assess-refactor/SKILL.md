---
name: assess-refactor
description: Before implementing new functionality, decide whether a focused refactor of the code the feature will touch should happen FIRST. Assesses the health of the area the feature lands in (coupling, duplication, oversized units, missing seams, weak tests, drifted names, near-violations of the architecture rules) and weighs the technical-debt cost of building on it as-is versus cleaning it up first. Analysis-only — recommends refactor-first / refactor-after / no-refactor with a bounded scope, the risk of skipping, and trade-offs; writes no code. Bounded by YAGNI: only refactor what this concrete feature touches. Use after the task is understood and before implementation.
---

# assess-refactor

This skill runs **before** implementation, once you know roughly _what_ the new feature is and _where_ it will land. Its job is to answer one question the team currently skips: **is the existing code we're about to build on healthy enough, or should we refactor it first?**

The problem it solves: when we jump straight into a new feature we never price in the technical debt we're about to take on. Sometimes the area the feature lands in is already tangled, and adding to it makes quality, maintainability, and extensibility worse — when a small, focused refactor first would have made the feature smaller, safer, and cleaner. This skill makes that call explicitly, instead of by accident.

It is **analysis-only**: it recommends a course of action, scopes it, and surfaces trade-offs. It writes **no code** and performs **no refactor**.

## Where it sits among the other skills

Know the boundaries — don't duplicate them:

- `/clarify-task` agrees on **WHAT** to build (goal, scope, boundaries) in plain language. Run it first if the goal is fuzzy.
- **`assess-refactor` (this skill)** judges the **GROUND** — is the existing code the feature will touch fit to build on, or does it need cleanup first?
- `/simplify-task` shapes the **NEW** code — the simplest sound approach for the feature, without over-building it.
- `/simplify-review` checks the **DIFF** afterward for over-engineering.

Natural order: `/clarify-task` → **`assess-refactor`** → `/simplify-task` → implement → `/simplify-review`. The two are complementary opposites: `/simplify-task` guards against over-building the _new_ thing; this skill judges whether the _old_ thing is good enough to build on.

## When to use

- After the task is understood and before you commit to an implementation approach.
- When the feature lands in code that already feels tangled, duplicated, or oversized.
- When you can't see a clean seam to attach the feature to — you'd have to reach into many places.
- When the area has weak or no test coverage and the feature will change its behavior.

When to skip — say so plainly and move on:

- Tiny, isolated change in healthy code.
- Greenfield code with no existing structure to refactor (there's nothing to clean up yet).
- The feature touches code that's messy but **stable and out of its path** — leave it alone (see _How to judge_).

## Inputs

- **The agreed scope** — the feature from the request + conversation + any linked task/backlog doc.
- **The blast radius** — the files, modules, and seams the feature will actually touch or extend. Find this first; everything else is judged against it.
- **The codebase** — existing patterns, `.agents/rules/` (especially the hard invariants in [architecture.md](../../../.agents/rules/architecture.md)), and the tests covering the area ([testing.md](../../../.agents/rules/testing.md)).

## How to run

1. **Restate the feature** in a line, and **map its blast radius** — which existing units (functions, modules, seams) it will touch, extend, or attach to. Refactor judgement applies **only inside this radius**.
2. **Assess the health of that radius** against the smells below. Read the code; don't guess.
3. **Check the test safety net.** A refactor without tests is risky. Note whether the area has coverage, or whether cheap characterization tests would be needed first.
4. **Weigh the debt.** For the touched code, compare: cost of building on it as-is (now + the interest you'll pay later) versus cost of a bounded refactor first. Be honest about both.
5. **Decide and scope.** Land on one verdict (below), bound the refactor to what the feature needs, and name the risk of _not_ doing it.

## What to look for (debt smells — only in the blast radius)

- **No clean seam where the feature attaches** — to add it you'd have to reach into many places or thread state through unrelated code. This is the strongest "refactor first" signal: create the seam, then build on it.
- **Duplication the feature would extend** — adding the feature means a 2nd/3rd near-copy. If this is the case that trips the rule of three, unify _first_ so the feature has one place to land.
- **Oversized / overloaded units** — a function or module already doing too much that the feature would pile more onto. Splitting first keeps the feature's change small and reviewable.
- **High coupling / leaky boundaries** — the feature would worsen an existing tangle, or smear logic across layers that should stay separate.
- **Near-violations of the hard invariants** — e.g. provider/CLI knowledge creeping toward core, fallback logic in the wrong layer, the state machine reached around. The feature must not cement these; a refactor that restores the boundary may be required, not optional.
- **Weak or missing tests** in the area the feature will change — no safety net to refactor or extend against.
- **Drifted names / abstractions** — names or types that no longer match what the code does; the feature would build on a misleading model and spread the confusion.

## How to judge (refactor must earn its place — YAGNI guards against gold-plating)

A refactor-first recommendation is justified **only** when all of these hold:

- It is **in the path** of this feature — the feature touches the code being refactored.
- Doing it first makes the feature **smaller, safer, or clearer** — a concrete, nameable benefit, not "it'd be nicer".
- The cost of refactoring now is **less** than building on bad ground plus cleaning it later (paying the debt interest).
- There is a test safety net, or one can be added cheaply before the refactor.

Refuse the refactor — recommend building directly — when:

- The messy code is **out of the feature's path**. Cleaning it is speculative scope creep → YAGNI. "While we're here" is not a reason.
- It's **behavior-changing dressed up as refactor**. A refactor is behavior-preserving by definition; behavior changes belong to the feature or a separate task.
- The mess is real but **stable**, and the feature doesn't touch it. "If it ain't broke, and we're not touching it, leave it."
- The refactor would **balloon the change and blur review** more than it saves.

Default bias: this repo is a greenfield MVP that challenges over-engineering hard (KISS > YAGNI > DRY). When the call is genuinely close, prefer **no refactor** and let the rule of three decide later — but say so explicitly, with the debt you're consciously accepting.

## If you recommend a refactor — keep it clean

- **Behavior-preserving and separate.** The refactor changes structure, not behavior. Keep it in its own commit / PR, ahead of the feature, so each diff reviews cleanly (see [git-workflow.md](../../../.agents/rules/git-workflow.md)).
- **Tests come with it.** Refactor under existing tests, or add characterization tests first; the feature then extends them (see [testing.md](../../../.agents/rules/testing.md)).
- **Respect the invariants and docs.** The refactor still obeys `.agents/rules/`; if it changes behavior/architecture/config, the docs sync in the same change (`/sync-docs`).

## Output

Concisely, in the user's language:

- **Verdict** — one of: **Refactor first**, **Refactor after / alongside**, or **No refactor — build directly**. One line, with the core reason.
- **Blast radius** — the units the feature touches, and a quick health read on each.
- **If refactor first/after** — the **bounded scope** (what to change, what to leave), the **concrete benefit** to the feature, and the **risk of skipping it** (the debt you'd accept). Keep the feature and the refactor as separate steps.
- **Debt consciously accepted** — if you recommend building directly on imperfect code, name the debt and why it's the right call now (and what later signal would trigger the cleanup).
- **Trade-offs / decisions for the user** — where "refactor now vs later" is genuinely the user's call (cost, timing, risk appetite), ask briefly with options.
- **Open questions** — anything unclear. If it's a WHAT question, route back to `/clarify-task`.

Then **stop**. Write no code; hand the decision to `/simplify-task` and implementation.

## What not to do

- Don't write or apply the refactor — this skill only assesses and recommends.
- Don't recommend cleaning code the feature doesn't touch — that's scope creep, not debt management.
- Don't smuggle behavior changes into a "refactor"; refactors preserve behavior.
- Don't recommend refactoring without a test safety net (or a cheap plan to add one).
- Don't turn every feature into a refactoring project. Most of the time the right answer is a small refactor or none — say it plainly.
- Don't re-open settled scope (that's `/clarify-task`) or judge the new feature's complexity (that's `/simplify-task`).
