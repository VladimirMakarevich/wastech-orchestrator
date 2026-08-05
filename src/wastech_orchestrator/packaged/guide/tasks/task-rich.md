---
# A "clean" task: identity/dispatch only, plus the two sanctioned exceptions — `nodes.<id>.enabled`
# (per-task node disable) and `auto_merge` (task-wins). Provider, model, and reasoning live on the
# FLOW NODE (config.yaml providers + the flow YAML), never the task. Refinement-skip is automatic (a
# complete task — description + acceptance criteria — skips it); decomposition is decided by the flow.
# `task_type` picks WHICH flow runs the task (omit ⇒ implementation); the task only names it.
id: task-webhook-retry-budget
title: "Add a bounded retry budget to webhook delivery"
task_type: implementation # selects the FLOW. Omit ⇒ implementation (default). Built-ins: implementation, deep_research, security_audit, content_chapter, content_translate, blog_article, blog_article_revise; or a custom operator flow at .worc/flows/<task_type>.yaml. The task only names the flow — never edits it.
branch_name: "feature/ABC-123-webhook-retry-budget" # full branch override; omit for repo.branch_prefix/id/title slug. Ignored when branch_mode is existing/current.
branch_mode: new # new (default; fork a fresh branch from base) | existing (work in branch_ref) | current (use the current checkout as-is). Overrides repo.branch_mode.
# branch_ref: "feature/big-feature" # REQUIRED iff branch_mode: existing (the already-existing branch to check out); omit for new/current.
publish: pull_request # downgrade-only cap on where the publish node stops: commit | push | pull_request. Effective = min(flow_policy, publish); omit ⇒ flow policy; no-op if the flow has no publish node.
trust_level: auto # per-task override of the dangerous-diff approval gate: strict (gate every deletion/manifest edit) | auto (default; gate only operator protected_paths). Never lowers the hard ceiling.
auto_merge: false # true = auto-merge (DANGER: skips human review; the task author owns this call) / false = opt out / omit = config default. The task value wins outright.
priority: high # scheduling order under `watch`: eligible tasks run high → mid → low (ties by natural filename order, p9 before p10). low|mid|high; omit/unrecognised ⇒ mid (fail-open). depends_on is always stronger.
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
