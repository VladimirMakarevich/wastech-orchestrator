---
id: task-XXX
title: "Short imperative title"
refined: false          # set true to skip the refinement stage if this task is already complete
decompose: false        # true forces decomposition / false disables it / omit = config default (§5.1)
agents:                 # optional per-stage override (only providers from agents.allowed)
  refinement: claude
  planning: claude
  implementation: claude
  review: codex
  fixing: claude
  summary: claude
---

## Description

Describe concretely what needs to be done. The front matter becomes the normalized task manifest;
this body is the context handed to the agent.

## Acceptance criteria

- [ ] what must work after implementation;
- [ ] which tests to add or update.

## Constraints

- modules that must not be touched;
- no new dependencies without approval.
