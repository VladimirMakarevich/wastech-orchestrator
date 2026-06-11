---
id: task-001
title: "Example task: add login form validation"
agents:                     # optional per-stage override (only from agents.allowed)
  planning: claude
  implementation: claude
  review: codex
  fixing: claude
contacts: ["@team-lead"]    # who to ping on Telegram when there are questions
---

## Description

Describe briefly and concretely what needs to be done. This file is parsed by the orchestrator:
front matter → normalized task manifest, body → context for the agent.

## Acceptance criteria

- [ ] what should work after implementation;
- [ ] which tests to add/update.

## Constraints

- do not touch the billing module;
- no new dependencies without approval.
