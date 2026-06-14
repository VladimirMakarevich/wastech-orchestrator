---
id: task-webhook-retry-budget
title: "Add a bounded retry budget to webhook delivery"
pr_title: "feat(webhooks): bounded retry budget for delivery"  # overrides the auto-generated PR title (omit to auto-generate from title)
refined: false              # true = skip the refinement stage (criteria below already make it complete)
decompose: false            # true = force split into subtasks / false = disable / omit = config default
auto_merge: false           # true = auto-merge (DANGER: skips human review; only if config git.auto_merge_allow_per_task) / false = opt out / omit = config default
contacts:                   # handles surfaced for human-in-the-loop prompts and approvals
  - "@team-lead"
  - "@webhooks-oncall"
agents:                     # per-stage provider override — only agent-routed stages; only providers in agents.allowed
  refinement: claude
  planning: claude
  implementation: codex
  review: claude
  fixing: codex
  summary: claude
model: claude-sonnet-4-6    # task-wide default model for agent-routed stages not overridden under `stages`
reasoning: medium           # task-wide default reasoning: low | medium | high | xhigh | max
stages:                     # per-stage overrides; precedence: stages.<stage> -> task-wide -> provider default
  refinement:
    model: claude-opus-4-8
  planning:
    model: claude-opus-4-8
    reasoning: high
  implementation:
    model: claude-sonnet-4-6
    reasoning: medium
  review:
    reasoning: high          # only reasoning overridden — model stays the task-wide default
  fixing:
    model: claude-sonnet-4-6
  testing:
    enabled: true            # testing is skippable but runs no agent -> only `enabled` is valid here (no model/reasoning); false would skip checks (rarely wanted)
  summary:
    enabled: false           # (illustrative) skip a stage; routing & `enabled` are independent. Skippable: planning, testing, review, fixing, summary.
                             # skipping `review` also needs agents.allow_review_skip; implementation/refinement are never skippable; publishing is not per-task.
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
