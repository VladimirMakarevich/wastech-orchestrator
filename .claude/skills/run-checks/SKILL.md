---
name: run-checks
description: Run all quality checks for wastech-orchestrator (ruff, mypy, pytest) and report briefly. Use before a commit, before a PR, and before moving to the next implementation stage.
---

# run-checks

Run the full set of checks for the wastech-orchestrator repository.

## Steps

1. Make sure the environment is installed (`pip install -e ".[dev]"`); if dependencies are missing, install them.
2. Run the following in order and collect the result of each command:
   ```bash
   ruff check .
   ruff format --check .
   mypy src
   pytest
   ```
3. If something fails:
   - show the specific errors (file:line) and a brief cause;
   - propose or apply a minimal fix;
   - re-run only the relevant check.
4. At the end, give a short summary: what passed and what did not. Do not declare "all green" if even a single check failed.

## Rules

- Do not disable linter/type-checking rules just to get a "green" run — fix the cause.
- Do not commit while checks are red (see docs/rules/testing.md, git-workflow.md).
