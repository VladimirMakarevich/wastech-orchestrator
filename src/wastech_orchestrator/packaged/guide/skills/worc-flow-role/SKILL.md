---
name: worc-flow-role
description: Write or revise a single flow node's role prompt (`role_file`) for wastech-orchestrator — the Markdown text that becomes that node's instructions — honoring the node's typed output contract and the `{name}` variable allowlist. Use when you want to change what a step says without changing the graph; author a whole new flow with worc-flow, or repoint a step's provider/model with worc-flow-tune.
---

# worc-flow-role

Help an operator write or revise one node's role prompt. A `role_file` is a plain Markdown file — no front matter, no schema — that becomes a node's instructions. Changing what a step _says_ never needs a new flow: you edit that node's `role_file` in the delivered copy under `.worc/flows/<task_type>/<name>.md`. Speak in the user's language (default to the language they wrote in).

## When to use

- The operator wants a step to behave differently in _wording/emphasis_ — a sharper review lens, a more specific implementation brief, a different research angle — but the graph (nodes, edges, output kind, route) stays the same.
- For a new step, a new route, or a different output kind → **worc-flow**. To change which provider/model/reasoning a step uses → **worc-flow-tune**. Neither of those is a prompt edit.

Before editing, read the packaged role guide `.worc/guide/flows/roles.md` (what a role file is, the built-in evaluator roles, and the per-node output contract) and `.worc/guide/flows/prompt-variables.md` (the `{name}` allowlist and the `{?name}…{/name}` syntax). Do not invent variables or output shapes.

## How to run

1. Locate the node and its prompt. In `.worc/flows/<task_type>.yaml` find the node, read its `role_file:` value (relative to `.worc/flows/`), and open that file under `.worc/flows/<task_type>/`. Confirm the node's `kind` (`agent` vs `evaluator`) and whether it has `hitl:` or is the planning node named by `decomposition.proposed_by` — that decides the contract.
2. Honor the node's typed output contract (the core re-validates it — a malformed result fails the node; you cannot loosen this from a prompt):
   - **plain `agent`** — returns its final message (also exposed downstream as `{<id>_path}`). If the node declares `output_file:` in the flow, the **file** it writes is what travels as `{<id>_path}` instead — so the prompt must name that same filename and require the file to stand alone, because no closing message goes with it.
   - **`agent` with `hitl:`** — returns `content` plus an optional question/approval object; ask a question only for a material ambiguity repository evidence cannot resolve.
   - **planning agent** (`decomposition.proposed_by`) — `content` + optional `human_input` + `decompose` + `subtasks`.
   - **`evaluator`** — must emit `{ findings: [ { severity, path, what, fix } ] }`. It is **fail-closed**: a prose-only "looks good" routes the task to `manual_action_required`. Return an **empty `findings` array** when clean, not prose. For an evaluator, also mind its `role` lens (`review` / `critic` / `verifier` / `test_quality`, or the default) and spell the findings fields out explicitly.
3. Reference artifacts only by **allowlisted `{name}` path variables** (e.g. `{task_path}`, `{plan_path}`, `{diff_path}`, `{repo_path}`, and node-chaining `{<node_id>_path}`) — never inline task bodies, diffs, env, or secrets. Wrap every _optional_ variable in a `{?name}…{/name}` block so a missing value never leaves a dangling fragment.
4. Keep the prompt short, imperative, and focused on the node's single job.
5. Validate: run `worc validate-flow <name>`. Its anti-drift lint **warns** about any `{name}` no node populates (a typo like `{plna_path}` would otherwise ship as literal text — it renders verbatim, which is the safe fallback, so it is a warning not a failure). Set `prompt_audit: true` to inspect the exact rendered prompt per node under `logs/<task-id>/prompt-audit/`.

## Heuristics

- One role file = one node's single job. If you find yourself asking a prompt to do a second step's work, that is a graph change (**worc-flow**), not a prompt edit.
- Supervisor prompts are a special case: they live at `.worc/flows/roles/supervisor.md` (or a flow's `supervisor:` block) and receive **only** `{task_id}` and `{repo}`/`{repo_path}` — no node/path variables.

## What not to do

- Don't turn an evaluator into a prose review — it must emit the findings result (empty array when clean) or the task fail-closes to manual.
- Don't reference a `{name}` that no node populates for that node; don't paste task text, diffs, secrets, or environment values into the prompt — only path variables are allowed.
- Don't try to change the machine contract from wording (output schemas, follow-ups, memory delta stay in the orchestrator). A prompt can move tone and emphasis, never what the orchestrator parses.
- Don't point a `role_file` outside the flow's own `<task_type>/` folder, and never use `..`.
