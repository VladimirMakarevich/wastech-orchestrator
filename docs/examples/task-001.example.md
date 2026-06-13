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
contacts: ["@team-lead"]    # plain-text Telegram mentions for prompts/terminal notifications
model: null                 # optional: override model for all stages, e.g. "claude-opus-4-8"
reasoning: null             # optional: low | medium | high | xhigh (Opus 4.7+) | max
stages:                     # optional: per-stage model/reasoning (overrides the task-wide values
                            # above for that stage; both sub-fields optional and independent).
                            # Allowed: refinement, planning, implementation, review, fixing, summary.
  planning:
    model: null             # e.g. "claude-opus-4-8"
    reasoning: null         # e.g. "high"
  review:
    model: null
    reasoning: null
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
