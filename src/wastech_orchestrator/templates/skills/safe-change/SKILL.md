---
name: safe-change
description: Discipline for making a minimal, safe change in a repository driven by the wastech-orchestrator. Use during planning/implementation/fixing to keep the diff scoped and to respect the orchestrator's guardrails (no commits, no publishing, no stray files).
---

# safe-change

This repository is modified by an automated orchestrator that owns the Git lifecycle. Your job is to
change the **code**; the orchestrator handles branching, staging, commit, push, and the PR. Follow
this discipline on every change so the result passes review and checks on the first pass.

## Do

- Make the **smallest change** that satisfies the task and its acceptance criteria.
- Touch **only** the files the task requires; match the surrounding code's style and conventions.
- When you change behavior, add or update tests (see the `test-discipline` skill).
- State any assumption you had to make in your final result, rather than guessing silently.

## Never

- **Never** run `git commit` / `git push` / `git merge` or create a pull request — the orchestrator
  publishes the result. Staging and committing are not your responsibility.
- Never add a dependency unless the task truly requires it; if it does, call it out explicitly in
  your result instead of adding it quietly.
- Never read or modify secrets (`.env`, `secrets/**`) or credentials.
- Never touch the orchestration/task directories if present (`tasks/`, `logs/`, `workspace/`),
  reformat unrelated code, or leave debug prints / commented-out blocks behind.
- Never weaken or work around the sandbox/permission settings you are running under.

## When the task is ambiguous

Proceed autonomously with a documented assumption (the orchestrator's refinement stage exists for
this). Record the assumption in your result; do not invent scope beyond the task.

## Result handoff

End with a short note: what you changed, which files, any assumption made, and anything the reviewer
should look at. Do **not** describe Git actions — you take none.
