---
name: worc-flow-tune
description: Tune an existing wastech-orchestrator flow's per-node execution knobs — which `provider`/`model`/`reasoning` a step runs on, plus `timeout_seconds`, `network_access`, `git_evidence`, `extra_args`, and the flow's loop `budgets` — without changing the graph or the prompts. Use when a step should run on a different provider or effort; author a new flow with worc-flow, or reword a step with worc-flow-role.
---

# worc-flow-tune

Help an operator change _how_ a flow's steps execute — the provider, model, reasoning effort, timeout, network access, and loop budgets — while leaving the graph (nodes, edges, routes) and the prompts untouched. These live on the flow node, never on the task: **a task file can never repoint a stage's provider or set its model.** Speak in the user's language (default to the language they wrote in).

## When to use

- The operator wants a step to run on a different `provider` (`codex`/`claude`), a stronger/cheaper `model`, more/less `reasoning` effort, a longer `timeout_seconds`, network on/off, or wants to change a loop's iteration `budget` — and the steps themselves stay the same.
- For a new step, route, or output kind → **worc-flow**. To change what a step _says_ → **worc-flow-role**. This skill only turns the per-node execution dials on an existing `.worc/flows/<task_type>.yaml`.

Before editing, read the packaged flow reference `.worc/guide/flows/reference.md` (the node field tables and the validation layers) and the annotated built-in `.worc/flows/implementation.yaml` — its commented-out per-node `provider` / `model` / `reasoning` slots are the worked example of exactly these knobs.

## How to run

1. Locate the node in `.worc/flows/<task_type>.yaml` (these are the editable copies `install` seeded; the packaged copies in the wheel are never read at run time).
2. Set the per-node knobs you need (all default to `null` = inherit):
   - `provider` — `codex` \| `claude`; `null` uses the global primary. **Must be listed in `agents.allowed`.**
   - `model` — overrides the provider's default; **passed through unverified**, so a wrong id fails only at run time — do not invent model ids.
   - `reasoning` — overrides effort; **must be valid for the resolved provider** (Claude and Codex effort sets differ) — this one _is_ validated.
   - `timeout_seconds` — per-attempt CLI wall-clock ceiling.
   - `network_access` — tri-state per-node override of the flow's `network_policy` (`true`/`false`/omit).
   - `git_evidence` — tri-state; `true` asks for the read-only git verbs so the node can inspect delivery history. Honored only while the operator's `security.allow_git_evidence` is on (default off, so the declaration is otherwise inert), and **rejected on a `workspace-write` node** — it already has an unrestricted shell.
   - `extra_args` — raw CLI flags for this node (subject to the forbidden-args scan).
   - `best_effort` — tolerate an infrastructure failure and continue (e.g. a summary node).
3. For loop iteration caps, edit the flow-level `budgets:` mapping (each named `fail`/`rework` loop; the engine clamps to `min(flow, config cap)`), not a node field.
4. Confirm the model/reasoning you chose is actually configured for that provider in `.worc/config.yaml agents.providers.<id>` — if the provider or its reasoning set needs adjusting, that is a config change (use **worc-config**), not a flow edit.
5. Validate: run `worc validate-flow <name>`. The config-aware layer checks that every `provider` is in `agents.allowed`, that `reasoning` is valid for the resolved provider, and that no Codex `workspace-write` node also has network. `worc preflight` does **not** validate flows.

## Applying the change to a task already in flight

A brand-new task always picks up the edited flow. To apply your edit to a **specific parked/failed task** without re-paying for completed upstream work, resume it with `worc rerun <id> --continue` (add `--from <node>` to re-enter at a chosen step, e.g. `--from review`): an operator `--continue` **adopts** the current on-disk flow — it re-freezes the control plane from your edited files and resumes from the checkpoint under the new knobs. `--dry-run` prints a `note:` when it detects the change. (Only automatic daemon crash-recovery keeps the task's original frozen flow; an operator `--continue` is trusted to adopt.) Editing a task file or a `SKILL.md` is **not** adopted this way — those are frozen per task, so they need a fresh run. (`AGENTS.md`/`CLAUDE.md` are different: the agent reads them live, so an edit between runs is picked up automatically on the next run; a task may also edit them during a run — that is ordinary work, reported to the operator, not blocked.)

## Heuristics

- Raise `reasoning` (and reach for a heavier `model`) only where the stage's difficulty warrants the extra token cost; leave routine stages at their defaults.
- Change one knob at a time and re-validate — most run-time surprises are an invalid `reasoning` value for the resolved provider or a `model` id that provider does not serve.
- Prefer leaving a node at its inherited default over pinning a provider: a pinned `provider` also constrains lineage resumption (an `editing_lineage` cannot resume across providers).

## What not to do

- Don't set `provider`/`model`/`reasoning` in a **task file** — they are flow-node concerns and get the task rejected. Tune the flow node instead.
- Don't name a `provider` that is not in `agents.allowed`, or a `reasoning` value invalid for that provider — validation fails.
- Don't give a Codex `workspace-write` node `network_access: true` — it is rejected; split external fetches into a `read-only` node.
- Don't invent a `model` id; it is passed through unverified and will only fail at run time. Keep to models the provider actually serves.
- Don't add full-access `extra_args` under `strict_isolation` — the forbidden-args scan rejects them.
