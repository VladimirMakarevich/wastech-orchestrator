---
id: task-webhook-retry-budget
title: "Add a bounded retry budget to webhook delivery"
refined: false              # let refinement enrich if needed; criteria below already make it complete
decompose: false            # one coherent change — keep it a single unit
agents:                     # per-stage provider override (only providers the operator allows)
  planning: claude
  implementation: claude
  review: codex
contacts:
  - "@team-lead"
model: claude-sonnet-4-6    # task-wide default model for stages not overridden below
reasoning: low
stages:                     # per-stage model/reasoning overrides (most-specific wins)
  planning:
    model: claude-opus-4-8
    reasoning: high
  review:
    reasoning: high         # only reasoning overridden — model stays the task-wide one
---

## Description

Webhook delivery currently retries forever on failure. Add a bounded retry budget: stop retrying
after a fixed number of failed attempts and mark the delivery as exhausted. Store the attempt count
on the existing delivery record and leave the successful-delivery path unchanged.

## Acceptance criteria

- [ ] A failed webhook delivery increments an attempt counter on the delivery record.
- [ ] Delivery stops retrying after 5 failed attempts and the record is marked `exhausted`.
- [ ] A successful delivery still marks the record as `delivered` and does not increment the counter.
- [ ] Add or update tests for retry exhaustion and the success path.

## Constraints

- Do not change the public webhook payload shape.
- Do not add a new queue or storage backend; reuse the existing delivery record.
- No new runtime dependencies without approval.
