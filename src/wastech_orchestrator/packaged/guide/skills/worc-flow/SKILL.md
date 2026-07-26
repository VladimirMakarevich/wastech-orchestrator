---
name: worc-flow
description: Author a new custom flow for wastech-orchestrator — a validated graph of typed nodes saved as `.worc/flows/<task_type>.yaml` with its own prompt folder, that a task selects by `task_type`. Use when you need different steps, a different output kind, or a different route than a built-in flow gives you — not to change what one step says (that is worc-flow-role) or which model a step uses (that is worc-flow-tune).
---

# worc-flow

Help an operator author a brand-new flow: the pipeline a task runs through, written as data — a validated graph of typed nodes (`agent`, `evaluator`, `checks`, `tool`, `publish`) joined by outcome-labelled edges. A task's `task_type` selects its flow. Speak in the user's language (default to the language they wrote in).

## When to use

- The operator needs a pipeline that does not exist yet: different steps, a different **output kind** (code vs a research document vs a private report), or a different route than a built-in flow (`implementation`, `deep_research`, `security_audit`, …) gives.
- **Do not** create a flow just to reword a step — edit that node's `role_file` instead (use **worc-flow-role**). **Do not** create a flow to change a step's provider/model/reasoning — tune the existing node (use **worc-flow-tune**). Author a flow only when the graph itself must change.

Before drafting anything, read the packaged flow guide — `.worc/guide/flows/README.md` (the quickstart, where flows live, and the foot-guns) and `.worc/guide/flows/reference.md` (every flow-level and node-level field with allowed values, defaults, and constraints). Do not guess a field's meaning or invent values.

## How to run

1. Inspect what already exists before asking questions. Look for the seeded built-in flows under `.worc/flows/` (they are editable copies) and pick the closest one as a starting shape rather than authoring from scratch. Confirm the desired deliverable with the operator: is it a code change, a repository document, or a private report? Where should the result be published?
2. Choose the three flow-level contracts up front (see `.worc/guide/flows/reference.md` for each variant):
   - `output_policy` — the **closed set of three** (`code_change` | `repository_document` | `private_control_workspace_report`). This is a **contract, not a description**: it fixes the write area and the required files.
   - `publishing` — where the result lands (`pull_request` | `documentation_pull_request` | `local_artifact` | `private_control_workspace_report` | `none`).
   - `permission_ceiling` — the hard cap for every node; set it as low as the flow needs (`read-only` unless a node must edit).
3. Create the dispatch file `.worc/flows/<task_type>.yaml` — **the file stem must equal `task_type`** — and put every node prompt in the sibling folder `.worc/flows/<task_type>/*.md`. `role_file` values are relative to `.worc/flows/` (e.g. `role_file: <task_type>/implement.md`), no `..`.
4. Wire the graph:
   - Give each step a typed node (`agent` / `evaluator` / `checks` / `tool` / `publish`).
   - Join them with edges carrying the source kind's allowed `outcome` labels.
   - **Bound every `fail`/`rework` loop with a `budget`.** Exactly one entry node (no incoming edges); every node must be able to reach a terminal.
   - Chain results by name: an `agent`/`tool` node's output is exposed to later nodes as `{<node_id>_path}`.
5. Write the node prompts. Defer the prompt wording and the per-node output contract to **worc-flow-role** (`.worc/guide/flows/roles.md` + `.worc/guide/flows/prompt-variables.md`).
6. If the flow has a `checks` node with `checker: command_profile`, make sure `.worc/config.yaml` actually defines matching `checks.command_sets` — otherwise there is no quality gate to run. Use **worc-config** for that.
7. Validate: run `worc validate-flow <name>` (or `--all`). It loads and validates the flow config-aware — graph integrity, the security ceiling, config consistency, and `.worc/tools/` tool names — exactly what the engine checks at dispatch. `worc preflight` does **not** validate flows.
8. Optionally track flows in git so custom flows get history and go through a PR: replace the blanket `.worc/` line `install` wrote in the repo's `.gitignore` with `.worc/*` + `!.worc/flows/` (see `.worc/guide/flows/README.md → Tracking flows in git`).

## Heuristics

- Start from the closest seeded built-in flow and trim/extend it; do not author a graph from a blank file.
- Keep the graph as small as the deliverable needs — every node is a launch, a budget, and a failure mode.
- Prefer the built-in node output contracts over a custom `output_schema`; reach for a custom schema only when you genuinely need a different shape.
- Reuse `{<node_id>_path}` to hand one node's result to the next instead of inventing new plumbing. One node exposes exactly one output — split into several nodes to publish several results.

## What not to do

- Do not pick `output_policy` by what the deliverable "sounds like". A brand-new document that is not a `docs/research/*` sources bundle (e.g. a blog post under `blog/`) is a `code_change`, **not** a `repository_document` — choosing `repository_document` confines every write to `docs/research/<task_id>/` and the flow hard-stops at `manual_action_required` on the first real write.
- Do not set a custom `output_schema` without making **every** object in it (top level and nested) `additionalProperties: false` — Codex rejects a non-strict schema with a hard 400 and the node fails on every run.
- Do not leave an `evaluator` node with a prose-only "looks good" prompt — an evaluator is **fail-closed**: it must emit the findings result or the task routes to `manual_action_required`.
- Do not raise `permission_ceiling` above what the flow needs, and do not grant `workspace-write` to a node that only reads. A read-only node that needs to read git history takes `git_evidence: true`, not `workspace-write`. A Codex `workspace-write` node with network access is rejected — split external fetches into a `read-only` node.
- Do not point a `role_file` outside the flow's own `<task_type>/` folder, and never use `..`.
- Do not leave a `fail`/`rework` loop unbounded — every loop needs a `budget` or validation fails.
