---
# A "clean" task: identity/dispatch only, plus the sanctioned exceptions — the per-node `nodes.<id>`
# block (the `enabled` disable toggle + the best-effort model/reasoning/provider overrides) and the
# task-wins gates (`auto_merge`, `prompt_audit`, `decomposition`). The FLOW NODE still DECLARES
# provider/model/reasoning (config.yaml providers + the flow YAML); a task can only overlay them for
# one run. Refinement-skip is automatic (a complete task — description + acceptance criteria — skips
# it); whether a split happens is still the flow + planning's call, `decomposition:` only flips the gate.
# `task_type` picks WHICH flow runs the task (omit ⇒ implementation); the task only names it.
# Every field below is optional except `id` + `title`; this file shows them all, it is not a template.
id: task-webhook-retry-budget
title: "Add a bounded retry budget to webhook delivery"
task_type: implementation # selects the FLOW. Omit ⇒ implementation (default). Built-ins seeded into .worc/flows/ by `install`: implementation, deep_research, security_audit, blog_article, blog_article_revise, content_chapter, content_translate; or your own .worc/flows/<task_type>.yaml (that folder is the ONLY place flows resolve from). The task only names the flow — never edits it.
branch_name: "feature/ABC-123-webhook-retry-budget" # full branch override; omit for <repo.branch_prefix>/<epoch>-<id>-<slug(title)> (e.g. worc/1765432100-task-webhook-retry-budget-add-a-bounded). Over 50 chars ⇒ warned and the auto name is used; equal to repo.base_branch ⇒ rejected. Ignored when branch_mode is existing/current.
branch_mode: new # new (default; fork a fresh branch from base) | existing (work in branch_ref) | current (use the current checkout as-is). Overrides repo.branch_mode.
# branch_ref: "feature/big-feature" # REQUIRED iff branch_mode: existing (the already-existing branch to check out); omit for new/current.
publish: pull_request # downgrade-only cap on where the publish node stops: commit | push | pull_request. Effective = min(flow_policy, publish); omit ⇒ flow policy; no-op if the flow has no publish node.
trust_level: auto # per-task override of the dangerous-diff approval gate: strict (gate every deletion/manifest edit) | auto (default; gate only operator protected_paths). Never lowers the hard ceiling.
auto_merge: false # true = auto-merge (DANGER: skips human review; the task author owns this call) / false = opt out / omit = config default. The task value wins outright.
prompt_audit: true # true/false forces per-node prompt recording under logs/<task-id>/prompt-audit/ for THIS task; omit = the global config.prompt_audit. Task value wins.
decomposition: false # true/false permits/forbids a split for THIS task (wins over agents.decomposition.enabled); omit = config default. Only flips the gate — the flow + planning still decide whether a split happens.
priority: high # scheduling order under `watch`: eligible tasks run high → mid → low (ties by natural filename order, p9 before p10). low|mid|high; omit/unrecognised ⇒ mid (fail-open). depends_on is always stronger.
queue: "default" # routes the task to the worc instance whose orchestrator.queue selector EQUALS this string (no balancing). Omit ⇒ "default". Fail-closed: a non-string or empty value REJECTS the task.
contacts: # handles surfaced for human-in-the-loop prompts and approvals
  - "@team-lead"
  - "@webhooks-oncall"
# depends_on: ["task-webhook-model"] # other TASK ids that must be MERGED before this one may start
#   (non-blocking: the scheduler runs other eligible tasks meanwhile). For SEPARATE tasks that build
#   on each other — not for splitting one task. Listing this task's own id rejects it.
# subtasks: ["subtasks/01-....md", "subtasks/02-....md"] # operator-authored decomposition: ordered
#   spec files run sequentially on ONE branch into ONE PR. Author it with the worc-deco-task skill —
#   the paths, count (2..max_subtasks), and linear ordering are validated at the pre-branch preflight.
nodes: # keys are flow node ids. Valid sub-keys: `enabled` + the best-effort model/reasoning/provider.
  testing:
    enabled: true # explicit default (it runs). false would skip the check gate (rarely wanted).
  fixing:
    enabled: false # (illustrative) disable the fixing node — the fix loop runs to its cap, then manual.
    # Any node in the task's resolved flow may be disabled; which ones are safe to disable is the
    # operator's flow-authoring call. An id absent from the flow — or one whose skip would leave the
    # graph with no forward edge — ends the task `failed` at flow resolution (controlled, no branch).
  review:
    provider: claude # THIS RUN ONLY; the flow node's own declaration is untouched.
    reasoning: high # claude: low|medium|high|xhigh|max — codex also has `minimal`.
    # Best-effort, deliberately: the gate checks only "non-empty string", and a value the resolved
    # flow/config cannot honor (provider outside agents.allowed, unsupported reasoning) is warned +
    # skipped at run time, falling back to the flow's value — the task is never aborted for it.
    # `model:` is passed through unchecked. Resolution: task override → flow node → provider config.
    # Needed on EVERY run? Edit the flow YAML instead — that is the durable fix, this is a one-off.
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
