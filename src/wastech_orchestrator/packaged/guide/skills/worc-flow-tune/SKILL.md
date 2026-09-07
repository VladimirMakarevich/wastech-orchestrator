---
name: worc-flow-tune
description: Tune an existing wastech-orchestrator flow's per-node execution knobs — which `provider`/`model`/`reasoning` a step runs on, plus `timeout_seconds`, `network_access`, `git_evidence`, `skills`/`allow_skills`, `extra_args`, and the flow's loop `budgets` — without changing the graph or the prompts. Use when a step should run on a different provider or effort; author a new flow with worc-flow, or reword a step with worc-flow-role.
---

# worc-flow-tune

Help an operator change _how_ a flow's steps execute — the provider, model, reasoning effort, timeout, network access, and loop budgets — while leaving the graph (nodes, edges, routes) and the prompts untouched. The flow node is where these are **declared**, and editing it is the durable change; a task file can only overlay `provider`/`model`/`reasoning` for a single run, per node, best-effort (`nodes.<node-id>`), and cannot touch the other knobs at all. **When a stage should run differently _every_ time, it belongs here, not in a task.** Speak in the user's language (default to the language they wrote in).

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
   - `git_evidence` — tri-state; `true` asks for the read-only git verbs so the node can inspect delivery history. Honored only while the operator's `security.allow_git_evidence` is on (`install` writes it on; with it off the declaration is inert), and **rejected on a `workspace-write` node** — it already has an unrestricted shell. Inert as well under `security.strict_isolation: false` (advanced mode), where every node has an unscoped shell already: declaring it there buys nothing, so do not reach for it instead of `workspace-write` on that configuration.
   - `skills` — name the **target repository's own** Claude Code skills this node must invoke (`skills: [acme-tdd]`), so the step runs instructions the team already wrote instead of a re-authored copy. **Advanced mode only** (`security.strict_isolation: false`) — under strict isolation the skill tool does not exist for the session, so this is rejected rather than left inert. Each name must resolve to `<repo>/.claude/skills/<name>/SKILL.md` or the task refuses to start.
   - `allow_skills` — tri-state, and the default is **off**: a node that declares neither key is launched with the CLI's skills switch turned off, so a skill the flow never asked for cannot fire on its own description. `true` turns them on without requiring a particular one (advanced mode only); `false` refuses them and is legal at every value of `strict_isolation`. Cannot be combined with `skills`.
   - `extra_args` — raw CLI flags for this node (subject to the forbidden-args scan).
   - `best_effort` — tolerate an infrastructure failure and continue (e.g. a summary node).
3. For loop iteration caps, edit the flow-level `budgets:` mapping (each named `fail`/`rework` loop; the engine clamps to `min(flow, config cap)`), not a node field.
4. Confirm the model/reasoning you chose is actually configured for that provider in `.worc/config.yaml agents.providers.<id>` — if the provider or its reasoning set needs adjusting, that is a config change (use **worc-config**), not a flow edit.
5. Validate: run `worc validate-flow <name>`. The config-aware layer checks that every `provider` is in `agents.allowed`, that `reasoning` is valid for the resolved provider, that no Codex `workspace-write` node also has network on the shipped default (that check is skipped under `security.strict_isolation: false`, where the mode grants both anyway), and that any node-declared `skills` / explicit `allow_skills: true` are legal for this isolation setting and that every named skill exists in the target repository. `worc preflight` does **not** validate flows.

## Applying the change to a task already in flight

A brand-new task always picks up the edited flow. To apply your edit to a **specific parked/failed task** without re-paying for completed upstream work, resume it with `worc rerun <id> --continue` (add `--from <node>` to re-enter at a chosen step, e.g. `--from review`): an operator `--continue` **adopts** the current on-disk flow — it re-freezes the control plane from your edited files and resumes from the checkpoint under the new knobs. `--dry-run` prints a `note:` when it detects the change. (Only automatic daemon crash-recovery keeps the task's original frozen flow; an operator `--continue` is trusted to adopt.) Editing a task file is **not** adopted this way — the task packet is frozen per task, so it needs a fresh run. (`AGENTS.md`/`CLAUDE.md` are different: the agent reads them live, so an edit between runs is picked up automatically on the next run; a task may also edit them during a run — that is ordinary work, reported to the operator, not blocked.)

Editing a flow **while a run is in flight** is a third case, and the one most likely to surprise you: it does not park the run and it does not take effect. Every node of a run reads the control plane the task froze at its start, so your edit reaches nothing in that run — the orchestrator notices the divergence, prints one warning naming the file, and carries on over the frozen copy. The divergence never stops the task, because the check cannot tell your edit from an agent rewriting a role prompt, and on a repository where flows get tuned several times a day that verdict would come down to whether you saved the file a minute before the freeze or a minute after. So: to fix the run you are watching, let it stop (or stop it) and resume with `--continue`, which adopts. Editing under it changes the next run only.

## Heuristics

- Raise `reasoning` (and reach for a heavier `model`) only where the stage's difficulty warrants the extra token cost; leave routine stages at their defaults.
- Change one knob at a time and re-validate — most run-time surprises are an invalid `reasoning` value for the resolved provider or a `model` id that provider does not serve.
- Prefer leaving a node at its inherited default over pinning a provider: a pinned `provider` also constrains lineage resumption (an `editing_lineage` cannot resume across providers).

## What not to do

- Don't put `provider`/`model`/`reasoning` at a **task file's top level** — that is rejected outright (`unknown_top_level_field`). A per-node overlay under `nodes.<node-id>` _is_ accepted, but it is one run only and best-effort (an unsupported value is warned and silently skipped), so it is for a one-off experiment — never the place to record a durable decision. Tune the flow node for that.
- Don't name a `provider` that is not in `agents.allowed`, or a `reasoning` value invalid for that provider — validation fails.
- Don't give a Codex `workspace-write` node `network_access: true` — it is rejected; split external fetches into a `read-only` node. That refusal belongs to strict isolation: under `security.strict_isolation: false` (advanced mode) the validator accepts the combination, because the mode has already granted every node both the write and the network.
- Don't invent a `model` id; it is passed through unverified and will only fail at run time. Keep to models the provider actually serves.
- Don't reach for `skills`/`allow_skills: true` on a strictly isolated configuration — it fails validation rather than going quietly inert, because a silently skipped step would let a run report success without having done the operator's tested procedure. `allow_skills: false` is fine there: a flow may always narrow.
- Don't read `allow_skills: false` as "this node cannot see the repository's skills". It removes the tool, not the files — a node with a shell can still read a `SKILL.md` and follow it as ordinary text.
- Don't promise a Codex node will invoke a named skill. It gets the same prompt block, but `codex exec` has no flag that names one, so only the off-switch is real there.
- Don't add full-access `extra_args` — the forbidden-args scan rejects them at any value of `strict_isolation`.
