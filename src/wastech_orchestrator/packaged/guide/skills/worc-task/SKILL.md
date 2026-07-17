---
name: worc-task
description: Convert a raw, free-form task into a valid wastech-orchestrator task file (YAML front matter + Description / Acceptance criteria / Constraints), staged in tasks/preparing/ and promoted to tasks/pending/. Use when you have an informal task — a paragraph, a ticket, a Slack message — and want a ready-to-run task file the orchestrator will accept.
---

# worc-task

Turn an informal task description into **one** valid wastech-orchestrator task file. The orchestrator is not a chat agent: it takes a single task file and drives it through a fixed pipeline (refinement → planning → implementation → testing → review → fixing → summary → publishing), then commits a branch and opens a PR. **One task = one branch = one PR.** Your job here is to translate the user's raw text into a file that passes the validation gate on the first try.

Speak in the user's language (default to the language they wrote in).

## When to use

- The user pasted a free-form task ("add X", "fix Y", a ticket body) and wants it as an orchestrator task.
- Use this for a **single coherent change**. If the work is several **independent** changes, make **separate task files** (each becomes its own PR). If it is **one** change you already know how to split into ordered steps that should land together as one PR, use **worc-deco-task** instead.

## How to run

1. **Read the raw text.** Draft the task from it directly. Ask **at most** a couple of clarifying questions, and only when a required field genuinely cannot be derived, or when you would otherwise have to invent acceptance criteria. Prefer to draft and let the user adjust.
2. **Derive the fields:**
   - `id` — a stable, lowercase slug of the title. **Must match** `^[a-z0-9][a-z0-9._-]{0,63}$` (starts with a letter/digit; then letters, digits, `.`, `_`, `-`; 1–64 chars). No spaces, no uppercase, no leading separator. Invalid ids are **rejected, never sanitized**.
   - `title` — a short, specific, non-empty imperative line. Used for the branch slug (`agent/<id>-<slug>`) and reports.
   - `## Description` — concrete and **non-empty**: what changes, why, and the end-state behavior.
   - `## Acceptance criteria` — a testable checklist (see below). Include it unless you deliberately want refinement to enrich the task.
   - `## Constraints` — do-not-touch areas, dependency limits, compatibility/migration limits.
3. **Set optional front-matter fields only when clearly warranted.** Default to omitting them — the defaults are almost always right.
4. **Write the file** to `tasks/preparing/<id>.md` — the staging folder the watch daemon never scans, so a half-written draft is never picked up mid-edit. When it is complete, promote it into the queue with `worc promote <id>` (or the `promote` verb inside `worc shell`), which atomically moves it into `tasks/pending/` (the canonical, git-tracked location). If you cannot find a `tasks/preparing/` directory, confirm where the task lifecycle lives or fall back to the repo root, and tell the user.
5. **Self-check** against the hard rules below before finishing.

## Write testable acceptance criteria

Each criterion should name the behavior, the input, and the expected output.

Good:

```markdown
- [ ] `GET /users?page=2` returns the second page using the existing pagination metadata.
- [ ] Invalid page values return HTTP 400 with the existing error shape.
- [ ] Add unit tests for valid and invalid page values.
```

Avoid (vague, untestable): "Make pagination better.", "Clean up the API."

If acceptance criteria are present **and** the Description is non-empty, the orchestrator skips refinement automatically. Omit them only when you want the refinement stage to enrich an under-specified task (missing criteria never rejects the task — there is no flag).

## The task contract (front-matter fields)

Only these keys are allowed. **Any other key makes the task rejected** (`unknown_top_level_field`).

| Field | Required | Type | Meaning |
| --- | --: | --- | --- |
| `id` | **yes** | string | Stable id. Must match `^[a-z0-9][a-z0-9._-]{0,63}$`. |
| `title` | **yes** | string | Short, non-empty human title. Branch slug + reports. |
| `task_type` | no | string | Flow selector. Omit ⇒ `implementation` (the default coding pipeline). Built-ins: `implementation`, `deep_research`, `security_audit`; operators add more as `<repo>/.worc/flows/<task_type>.yaml`. An unknown type (no matching flow) fails the task before any branch is created. The task only _names_ the flow — it never edits it. |
| `pr_title` | no | string \| null | PR title override, used verbatim instead of `title`. Does not change the branch name or commit messages. Omit to auto-generate. |
| `branch_mode` | no | `new` \| `existing` \| `current` | Where task git ops point. `new` (default) forks a fresh branch from base; `existing` works in `branch_ref`; `current` uses the current checkout as-is. Overrides `repo.branch_mode`. |
| `branch_ref` | no | string | Branch to check out — **required iff** `branch_mode: existing`; must already exist (never auto-created). Ignored for other modes. |
| `publish` | no | `commit` \| `push` \| `pull_request` | Downgrade-only cap on where the publish node stops (`min(flow_policy, publish)`). Omit ⇒ the flow's policy; no-op on a flow with no publish node. |
| `trust_level` | no | `strict` \| `auto` | Per-task override of the dangerous-diff approval threshold. `strict` gates every deletion/manifest edit; `auto` (default) gates only operator `protected_paths`. Never lowers the hard security ceiling. |
| `auto_merge` | no | boolean | `true` requests auto-merge of the PR (**DANGER — skips human review**); `false` always opts out; omit uses the instance default. A set per-task value wins outright. |
| `prompt_audit` | no | boolean | `true`/`false` forces prompt-audit recording for this task; omit uses the config default. |
| `decomposition` | no | boolean | `true`/`false` permits/forbids decomposition for this task (task-wins over `agents.decomposition.enabled`); omit uses the config default. Only flips the gate — the flow + planning still decide whether a split happens. Not for operator-authored `subtasks`. |
| `contacts` | no | list of strings | Plain-text mentions in Telegram notifications/HITL prompts. Cosmetic only — no access control, no chat routing. |
| `depends_on` | no | list of strings | Other **task ids** that must be **merged** before this task starts (non-blocking, merge-gated). For _separate_ tasks that build on each other. |
| `subtasks` | no | list of strings | Operator-authored decomposition (one root + ordered subtask spec files → one PR). **Use worc-deco-task to author this**, not by hand. |
| `nodes` | no | mapping | Per-node disable toggle, keyed by flow node id: `nodes.<node-id>.enabled: false`. `enabled` is the **only** valid sub-key. |

Body sections: `## Description` (required, non-empty) · `## Acceptance criteria` (present ⇒ refinement auto-skipped) · `## Constraints`. If there are no `##` sections at all, the whole body is treated as the description.

### `nodes` (per-node disable)

The only per-node knob is `enabled: false`, keyed by a flow **node id**. The default `implementation` flow's node ids are `planning`, `implementation`, `testing`, `review`, `fixing` (and `refinement`, which is skipped automatically when the task is complete — not via `nodes`). Disabling effects: `planning` → stub plan, single unit; `testing` → straight to review, no checks; `review` → **commit with no agent review gate (high-risk)**; `fixing` → a failure spins the fix loop to its cap, then `manual_action_required`.

```yaml
nodes:
  testing:
    enabled: false # only for a repo with no meaningful test suite
```

## Hard rules — never emit a task that breaks these

The validation gate rejects a task **before** any branch or agent runs. To always pass:

1. The file **starts** with a `---` front-matter fence — no blank lines or text before it.
2. `id` and `title` are present; `id` matches the regex above.
3. `## Description` (or the body) is **non-empty**.
4. **Only** the allowed front-matter keys appear; types match the table.
5. `nodes` overrides carry **only** `enabled` (a boolean), keyed by a flow node id. Any other sub-key (including `model`/`reasoning`) is rejected (`invalid_node_override`).
6. **No secrets** (tokens, keys, passwords) anywhere in the file. Credentials live in the operator's environment, never in a task.
7. **Front-matter values are plain text.** No value may look like a CLI argument: a value that **starts with `-`** or contains an **argv-shaped token** (a backtick `` ` ``, `;`, `|`, `$(`, or a newline) is rejected as `injection_suspected`. This applies to **every** field, `title` and `contacts` included (the gate does not exempt display fields). Front matter is scanned defensively even though the body never builds CLI arguments — so put code, shell snippets, or punctuation-heavy phrasing in the **body**, and keep front-matter values to short plain labels (e.g. title `Fix parse() on empty input`, not `` Fix `parse()` on empty input ``).
8. Keep it reasonably sized (the gate caps file size, line count, and per-line length).

**Provider, model, and reasoning are not task fields** — they live on the flow node (the operator's flow + config). A task cannot repoint a stage's provider or set its model. Never add `provider`, `model`, `reasoning`, or `agents` keys.

## Skeleton (copy and fill in)

```markdown
---
id: task-001
title: "Add a bounded retry budget to webhook delivery"
---

## Description

Webhook delivery should stop retrying after a bounded number of failed attempts. Store the attempt count with the existing delivery record and keep the current success path unchanged.

## Acceptance criteria

- [ ] Failed webhook delivery increments an attempt counter.
- [ ] Delivery stops after 5 failed attempts.
- [ ] Successful delivery still marks the record as delivered.
- [ ] Add or update tests for retry exhaustion and success.

## Constraints

- Do not change the public webhook payload shape.
- No new runtime dependencies without approval.
```

## What not to do

- Don't invent `provider`, `model`, `reasoning`, or `agents` keys — they are flow concerns and get the task rejected.
- Don't add any front-matter key outside the allowed table.
- Don't embed secrets, and don't put CLI-flag-shaped or shell-punctuation values in **any** front-matter field (including `title`/`contacts`) — a leading `-` or a `` ` ``/`;`/`|`/`$(` gets the task rejected. Put such content in the body.
- Don't try to add, replace, or relax checks, or weaken the sandbox — those are operator config, not task fields.
- Don't cram several unrelated changes into one task — split into separate tasks. To split **one** change into ordered steps that land as a single PR, use **worc-deco-task**.
