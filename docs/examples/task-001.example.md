---
id: task-001
title: "Example task: add login form validation"
refined: false              # set true to skip the refinement stage (task already complete)
decompose: false            # keep the first example as a single implementation unit
agents:                     # optional per-stage override (only from agents.allowed)
  refinement: claude
  planning: claude
  implementation: claude
  review: codex
  fixing: claude
  summary: claude
contacts: ["@team-lead"]    # who to ping on Telegram when there are questions
---

## Description

Describe briefly and concretely what needs to be done. Copy this example into the runtime
`tasks/pending/` directory before running it.

## Acceptance criteria

- [ ] what should work after implementation;
- [ ] which tests to add/update.

## Constraints

- do not touch the billing module;
- no new dependencies without approval.
