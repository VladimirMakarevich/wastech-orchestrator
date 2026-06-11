# AGENTS.md — instructions for the coding agent (Codex) in this repository

This repository is modified by an automated orchestrator. Follow these rules:

- Make the minimal, focused change that satisfies the task; match the surrounding code style.
- Do **not** run `git commit` / `git push` or create pull requests — the orchestrator owns publishing.
- When you change behavior, add or update tests.
- Touch only the files the task requires; do not add dependencies without approval.
- If the task is ambiguous, state your assumptions in the result instead of guessing silently.
