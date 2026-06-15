---
name: clarify-task
description: Before starting work, ask the user clarifying questions to surface contradictions, cut overkill, find the task's true goal and boundaries, and reach a shared understanding. Use at the start of any non-trivial task, or whenever the request is vague, contradictory, or too broad.
---

# clarify-task

This skill helps you **not rush into doing**, but first understand the task together with the user.
The goal: both the agent and the user see the task the same way, in the same words, and drop the
unnecessary parts up front.

Speak in **simple, plain language, in the user's language** (default to the language they wrote in),
with minimal technical detail. Imagine explaining to a smart person who is not a programmer.

## When to use

- The request is vague, broad, or stated only "in words".
- You sense a contradiction, or you're being asked for more than is needed.
- The cost of a mistake is high (hard to redo, affects many parts).
- Simply before starting any non-obvious piece of work.

If the task is small and unambiguous — don't ask questions for the sake of it, just do it.

## How to run the conversation

- Ask **2–8 questions at a time**, don't dump a long list.
- Where possible, offer **answer options** (use the question-with-options tool) so the user can pick
  rather than compose from scratch.
- After each answer, react briefly: "did I understand correctly that…".
- No jargon. If a term is unavoidable, explain it in one simple sentence right away.
- No more than 3 rounds of questions. The goal is clarity, not an interrogation.

## What to find out (in order)

1. **Restate in your own words.** Say back what they're asking for, in your own words. Ask: "did I get
   this right?" This catches mismatched understanding immediately.

2. **The true goal.** Not "what to build" but "why". What problem does this solve? Who is better off,
   and how exactly? Ask "why" as many times as needed to reach the real reason.

3. **Definition of success.** How will we know the task is done well? What should become visible or
   measurable? If you can't name a criterion, the goal isn't clear yet.

4. **Contradictions.** Find where requirements fight each other (fast and cheap but perfect; simple
   and flexible at once). Name the contradiction out loud and ask which matters more.

5. **Boundaries (what is NOT included).** Spell out clearly what the task does **not** cover. This is
   often more important than the list of what it does.

6. **Overkill.** For each non-obvious item ask: "is this really needed now?" Separate "must" from
   "nice to have". Offer a simpler path if one exists, and show what the complex one costs.

7. **Shared words.** If the user and you mean different things by a word, lock in one shared meaning.
   From then on, use only that.

## How to finish

Pull together a short **summary in plain language** (5–8 lines):

- **Goal:** why this is needed.
- **What we do:** the essentials, in two or three points.
- **What we do NOT do:** what was explicitly dropped.
- **How we'll know it's done:** the success criterion.
- **Agreed wording:** disputed terms and their shared meaning.
- **Open questions:** anything still unresolved.

End with a direct question: **"Shall we proceed like this?"** Do not start work until the user
confirms the summary.

## What not to do

- Don't invent answers for the user or silently extend the task.
- Don't hide a contradiction to "keep it simple" — surface it instead.
- Don't dive into implementation details here — this is about agreeing on the essence, not the code.
- Don't ask questions endlessly: once it's clear, move to the summary.
