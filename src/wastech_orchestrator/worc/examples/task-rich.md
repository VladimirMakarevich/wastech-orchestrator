---
# A "clean" task: identity/dispatch only, plus the two sanctioned exceptions — `nodes.<id>.enabled`
# (per-task node disable) and `auto_merge` (task-wins). Provider, model, and reasoning live on the
# FLOW NODE (config.yaml providers + the flow YAML), never the task. Refinement-skip is automatic (a
# complete task — description + acceptance criteria — skips it); decomposition is decided by the flow.
id: task-webhook-retry-budget
title: "Add a bounded retry budget to webhook delivery"
pr_title: "feat(webhooks): bounded retry budget for delivery" # overrides the auto-generated PR title (omit to auto-generate from title)
auto_merge: false # true = auto-merge (DANGER: skips human review; the task author owns this call) / false = opt out / omit = config default. The task value wins outright.
contacts: # handles surfaced for human-in-the-loop prompts and approvals
  - "@team-lead"
  - "@webhooks-oncall"
nodes: # keys are flow node ids; the ONLY per-node knob is `enabled` (the disable toggle).
  testing:
    enabled: true # explicit default (it runs). false would skip the check gate (rarely wanted).
  fixing:
    enabled: false # (illustrative) disable the fixing node — the fix loop runs to its cap, then manual.
    # Any node in the task's resolved flow may be disabled; which ones are safe to disable is the
    # operator's flow-authoring call. An id absent from the flow ends the task `failed` (controlled).
---

## Description

Webhook delivery currently retries forever on failure. Add a bounded retry budget: stop retrying after a fixed number of failed attempts and mark the delivery as exhausted. Store the attempt count on the existing delivery record and leave the successful-delivery path unchanged.

## Acceptance criteria

- [ ] A failed webhook delivery increments an attempt counter on the delivery record.
- [ ] Delivery stops retrying after 5 failed attempts and the record is marked `exhausted`.
- [ ] A successful delivery still marks the record as `delivered` and does not increment the counter.
- [ ] Add or update tests for retry exhaustion and the success path.

## Constraints

- Do not change the public webhook payload shape.
- Do not add a new queue or storage backend; reuse the existing delivery record.
- No new runtime dependencies without approval.
