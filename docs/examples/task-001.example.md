---
id: task-001
title: "Example task: add login form validation"
contacts: ["@team-lead"] # plain-text Telegram mentions for prompts/terminal notifications
stages: # optional per-stage skip toggle (the only per-stage knob).
  # enabled: false: SKIP the stage (per-task). Skippable: planning, testing,
  #   review, fixing, summary. (implementation/publishing can never be skipped;
  #   refinement is skipped automatically when the task is complete.) Skipping
  #   `review` requires agents.allow_review_skip: true.
  testing:
    enabled: true # set false when the repo has no meaningful test suite
  # review:
  #   enabled: false        # DANGER: no agent review gate; requires agents.allow_review_skip: true
---

## Description

Describe briefly and concretely what needs to be done. Copy this example into the runtime `tasks/pending/` directory before running it.

## Acceptance criteria

- [ ] what should work after implementation;
- [ ] which tests to add/update.

## Constraints

- do not touch the billing module;
- no new dependencies without approval.
