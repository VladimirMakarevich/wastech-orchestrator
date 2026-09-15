---
name: grill-me-with-docs
description: Grill the user relentlessly about a plan, decision, or idea, and then turn the settled result into a task spec folder under docs/backlog/<slug>/ by handing it to the spec-task skill. Use when the user wants their thinking stress-tested AND written down — "grill me with docs", "grill me and document it", "разгриль и задокументируй", "прогриль задачу и оформи спеку". The interview never writes code; the documentation is produced by spec-task, not here.
---

# grill-me-with-docs

Two phases, in order: **grill until the design tree is settled**, then **hand the settled tree to `/spec-task`**, which owns the documents. This skill adds nothing to either method — its whole job is the seam between them, so nothing that was settled in the interview gets asked again or quietly lost on the way into the spec.

Write no code in either phase.

## Phase 1 — Grill

Invoke **`/grill-me`** ([grill-me](../grill-me/SKILL.md)) and run it to completion, exactly as written: rounds over the frontier of the design tree, numbered questions with your recommended answer, facts found by you (dispatch a sub-agent) and decisions left to the user.

While grilling, keep a running record of what gets settled — you will need it in phase 3, and it must not be reconstructed from memory at the end:

- the **decision** and the answer the user chose (including where they overrode your recommendation, and why);
- the **options rejected** and the reason;
- every **fact** you or a sub-agent found in the environment, with a `path:line` citation;
- anything the user **deferred, excluded, or refused to decide** — this is scope fence and open-question material, not something to drop.

Phase 1 ends only where `/grill-me` says it ends: the frontier is empty and the user confirms you have reached a shared understanding. Do not start writing documents early, and do not shorten the interview because a spec is coming.

## Phase 2 — Confirm the handoff

Before any file is created, put a short brief to the user and get an explicit go-ahead:

1. **What was settled** — the design tree's resolved branches, in the user's own terms, a line each.
2. **What stayed open** — deferred decisions, unresolved trade-offs, assumptions that were accepted rather than verified.
3. **The proposed slug** for `docs/backlog/<slug>/`.

If the user pushes back here, that is another round of grilling, not an edit to the brief.

## Phase 3 — Hand it to spec-task

Invoke **`/spec-task`** ([spec-task](../spec-task/SKILL.md)) with the brief from phase 2 as its `args`. From that point `/spec-task` is in charge: it owns the slug, the folder, the templates, the one-document-at-a-time flow with sign-off, the backlog registration, and the Markdown gate. Do not scaffold the folder yourself and do not restate its rules here.

What this skill contributes is the mapping — the interview's output is not raw material for a fresh discovery pass, it is already the answer:

| From the interview | Lands in |
| --- | --- |
| The original pain, in the user's framing | `problem.md` |
| Settled behavior and outcomes | `requirements.md` (FR-/NFR-) |
| Everything explicitly excluded or deferred | `out-of-scope.md` |
| The story the user told to explain the change | `happy-path.md` |
| Decisions, rejected options, and their reasons | `design.md` — "Key decisions" |
| Facts found in the environment, with citations | `design.md` — the sections they ground |
| How the user said they would know it worked | `acceptance-criteria.md` |
| Questions the user refused or deferred to decide | `questions.md`, marked blocking or not as the interview decided |

Then, throughout the spec:

- **Never re-ask a settled question.** A decision that survived grilling goes into the document as a decision. If a document forces a genuinely new question — one the tree never reached — ask it, log it in `questions.md`, and say plainly that it is new.
- **Carry the reasons, not just the conclusions.** A key decision without its rejected alternative is the half that goes stale first.
- **An accepted recommendation is still the user's decision** — record it as theirs, not as yours.
- If a settled decision turns out to violate a hard invariant once `/spec-task` checks it against the code, that is a conflict to raise with the user, not something to silently re-decide in the document.

## Done when

`/spec-task` reports its own definition of done: the folder exists under `docs/backlog/<slug>/`, every document is filled and signed off, no blocking question remains, the plan is ordered, the folder is linked from the backlog index, and `python tools/mdlint.py` is green. Nothing from phase 1 is missing from it.