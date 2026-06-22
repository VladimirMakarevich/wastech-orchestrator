---
id: task-001
title: "Example task: add login form validation"
contacts: ["@team-lead"] # plain-text Telegram mentions for prompts/terminal notifications
nodes: # optional per-node disable toggle, keyed by flow node id (the only per-node knob).
  # enabled: false: disable the node (per-task). Any node in the task's resolved flow
  #   may be disabled (ids shown are the default `implementation` flow's). refinement is
  #   skipped automatically when the task is complete; the summary is always written.
  #   An id absent from the flow ends the task `failed` (a controlled error).
  testing:
    enabled: true # set false when the repo has no meaningful test suite
  # review:
  #   enabled: false        # DANGER: no agent review gate before commit/PR
---

## Description

Describe briefly and concretely what needs to be done. Copy this example into the runtime `tasks/pending/` directory before running it.

## Acceptance criteria

- [ ] what should work after implementation;
- [ ] which tests to add/update.

## Constraints

- do not touch the billing module;
- no new dependencies without approval.
