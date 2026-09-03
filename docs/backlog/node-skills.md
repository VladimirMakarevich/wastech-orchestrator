# Node-declared skills (`skills:` / `allow_skills:`) — run the operator's own harness skills from a flow, or refuse them

Status: **proposed** Date: 2026-09-04 Owner: Vladimir Makarevich

An operator who arrives with a working Claude Code harness already owns the knowledge this orchestrator asks them to re-author. They have a planning skill, an implementation skill, a testing skill — each proven against their own repository and maintained by their own team. Today the only way to get that knowledge into a flow is to copy its text into a role prompt, where the copy starts drifting from the original the moment it is made. This item lets a node point at the skill instead: the graph, the gates, the isolation and the publication stay the orchestrator's, and what a node _does_ inside its turn becomes the operator's own, already-tested instruction set.

It has two directions, and they are not symmetric. **Requiring** a skill (`skills:`) is a widening and is **advanced mode only** (`security.strict_isolation: false`) — not as policy, but because under strict isolation the `Skill` tool does not exist for the session at all. **Refusing** every skill (`allow_skills: false`) is a narrowing, is legal at every value of that switch, and is backed by a real per-attempt CLI off-switch on both providers rather than by prompt text.

## Problem

A deterministic flow is the reason to adopt this orchestrator, and a role prompt is the reason to hesitate: adopting the flow means re-writing, in our vocabulary, instructions the operator has already written and tested in theirs. Two copies of the same conventions then age apart, and only one of them is the one their humans use. Nothing in the product closes that gap — there is no way for a node to say "do this step the way this repository already does it".

## Current behavior (verified)

- **Repository skills already load.** Read-isolation is off out of the box (`security.disable_read_isolation: true`, [`config/schema.py`](../../src/wastech_orchestrator/config/schema.py)), so the Claude adapter emits `--setting-sources project` and the target's `.claude/skills/**` is discovered natively ([`providers/claude.py:978`](../../src/wastech_orchestrator/providers/claude.py)). The operator's **user-level** `~/.claude/skills` is deliberately not loaded: the adapter selects `project`, not the CLI's `user,project,local` default.
- **Under strict isolation they cannot be invoked.** `--tools` is a hard existence gate and carries only the profile baseline — `Read, Glob, Grep`, plus `Edit, Write, Bash` for `workspace-write` ([`providers/claude.py:134`](../../src/wastech_orchestrator/providers/claude.py)). `Skill` is not in it, so it does not exist for the session.
- **In advanced mode `--tools` is not emitted at all** ([`providers/claude.py:1006`](../../src/wastech_orchestrator/providers/claude.py)), so every built-in tool exists, `Skill` included, and no deny list names it ([`providers/claude.py:197`](../../src/wastech_orchestrator/providers/claude.py)). This is the whole reason the feature is mode-gated.
- **`Skill` is the real tool name**, read out of the pinned binary (`2.1.234`, [`providers/claude.py:220`](../../src/wastech_orchestrator/providers/claude.py)) rather than from memory; `SlashCommand` is not in that registry. The CLI's own help confirms the surface: `--disable-slash-commands` is documented as "Disable all skills", and skills "resolve via `/skill-name`". That flag is already reserved against `extra_args`, so a flow cannot switch skills off either.
- **`Skill` is not auto-approved.** It is absent from `--allowedTools` ([`providers/claude.py:1011`](../../src/wastech_orchestrator/providers/claude.py)), and how `acceptEdits` treats an existing-but-not-auto-approved tool in a headless run is not derivable from this code. The same uncertainty is already recorded in the advanced-mode restriction audit (`full-tool-access/audit-agent-restrictions-advanced-mode.md`, item 9). It is settled by a probe, not by reading.
- **Codex has skills but no selector.** `codex-cli 0.152.1` reports `skill_search` as stable/enabled and carries `skip_host_skill_discovery`; `codex exec --help` offers no flag that names a skill. In advanced mode nothing is disabled and the project is trusted, so host discovery applies — but there is no argv through which we can require one.
- **Both CLIs can be told to run no skills at all, per attempt.** Claude has `--disable-slash-commands`, documented by the CLI itself as "Disable all skills"; it is already in the adapter's reserved-flag set ([`providers/claude.py:640`](../../src/wastech_orchestrator/providers/claude.py)), so it is the adapter's to emit and an operator cannot reach it through `extra_args`. Codex has `--disable skill_search`; the name validates against the live binary (`codex sandbox --disable skill_search` runs, `--disable bogus_feature_xyz` fails with "Unknown feature flag"), and `--disable` is likewise reserved to the adapter.
- **A flow may always narrow.** The config-aware validation layer already states the rule it enforces — "Security can only ever _narrow_ here" ([`core/flow/validator.py`](../../src/wastech_orchestrator/core/flow/validator.py)) — which is why an off-switch needs no mode gate while a requirement does.
- **A skills layer existed here and was removed.** Its model was the opposite of this one: an inventory discovered by `git ls-files`, frozen into a package, handed to the agent as `- skill (read-only reference; advisory, do not execute)`, with the `Skill` tool deliberately unused for provider parity. It was deleted as unused product surface (see the "Agent instruction stubs in target repo" row in [README.md](README.md)). Nothing of it is revived here.

## Requirements

| # | Requirement |
| --- | --- |
| **R1** | An `agent` node MAY declare `skills: [<name>, …]`. Evaluator nodes are out of scope for v1 (open question 8). |
| **R2** | The declaration is legal **only** under `security.strict_isolation: false`. A flow declaring it against a strict config fails validation with a named violation — it is not accepted-and-inert the way `git_evidence` is, because an inert `skills` silently deletes the step's point. |
| **R3** | The names reach the agent as a **Core-built deterministic block** appended at the existing neutral prompt seam, never through the role-file renderer: the renderer stays path-only, which is a security invariant. |
| **R4** | That block states precedence explicitly: the security preamble and the role prompt win over anything a skill says, and no skill grants publication. The publication mandate is unchanged and unweakened. |
| **R5** | Names are resolved **fail-closed before any launch**: `<repo>/.claude/skills/<name>/SKILL.md` must exist. A missing skill is a configuration error, not a warning — a run that silently skipped the operator's tested step is worse than one that refused to start. |
| **R6** | The declared names are recorded durably (node run + prompt audit), so a finished run answers "which skills did this node have" from state, not from inference. |
| **R7** | The Claude adapter adds `Skill` to `--allowedTools` for an attempt that carries names, **only** in advanced mode. It changes nothing else: no `--tools`, no deny, no permission mode. Under strict isolation the adapter refuses the names outright (defence in depth over R2). |
| **R8** | Codex receives the same prompt block and no argv change. The shipped documentation says plainly that guaranteed invocation is Claude-only today. |
| **R9** | No inventory, freeze, packaging, redaction or cap layer. Skills are ordinary repository files read by the CLI itself; the documentation states that they are **not** frozen for the task and that a writing node can change one mid-run. |
| **R10** | An `agent` node MAY declare `allow_skills`, parsed **tristate** (`true` / `false` / absent) the way `network_access` and `git_evidence` already are. Absent is today's behavior: the CLI discovers the target's skills natively and the model may invoke one on its own. |
| **R10a** | An **explicit** `allow_skills: true` under `security.strict_isolation: true` is a validation error, for the same reason as R2: the operator asked for something this config cannot give. An **absent** key is not a request and is never an error — which is why the field is tristate and not a plain bool, since `true` is also the default and refusing a flow for restating a default would be noise. `false` is legal at every value of the switch. |
| **R11** | `allow_skills: false` is carried by the provider's own off-switch — Claude `--disable-slash-commands`, Codex `--disable skill_search` — not by prompt text. A one-line prompt statement MAY accompany it, and the documentation must label that line as advisory: this repository does not call friction a floor. |
| **R12** | `skills: [...]` and `allow_skills: false` on the same node is a validation error. The switch is all-or-nothing per node: neither CLI offers "these skills and no others", so no wording may imply one. |

## Proposed minimal design

```yaml
- id: implementation
  kind: agent
  role_file: my_flow/implement.md
  permission_profile: workspace-write
  skills: [acme-implement, acme-tdd] # advanced mode only
```

And the other direction, on a node whose turn must stay exactly what the flow says:

```yaml
- id: review_gate
  kind: agent
  role_file: my_flow/gate.md
  permission_profile: read-only
  allow_skills: false # → --disable-slash-commands (Claude) / --disable skill_search (Codex)
```

Core appends one deterministic block to the effective prompt (the operator never writes it, so it cannot drift from the YAML):

```
Required skills — invoke each of these before you finish; they are this repository's own conventions, not optional:
- /acme-implement
- /acme-tdd
Where a skill conflicts with the instructions above, the instructions above win. No skill grants any right to commit, push, or open a pull request.
```

Three properties make this the small version rather than a subsystem. The seam already exists — `build_effective_prompt` composes `preamble → prompt → footer` in one place for every request kind and both providers ([`providers/base.py:295`](../../src/wastech_orchestrator/providers/base.py)), so nothing per-adapter is needed to deliver the text. The security envelope is untouched: advanced mode already grants every built-in tool, so allow-listing `Skill` widens nothing that the mode had not already opened. And the discovery problem does not exist, because the CLI does the discovery — this feature only names what to call.

## Implementation steps

0. **Live probe first (paid, blocking).** One advanced-mode node with a trivial repository skill, run twice: with `Skill` in `--allowedTools` and without. This settles R7 and tells us whether the feature needs the argv change or only the prompt block. Do not write code before this answers.
1. **Schema + parse.** `skills: tuple[str, ...] = ()` on `AgentNode` ([`core/flow/schema.py:52`](../../src/wastech_orchestrator/core/flow/schema.py)); parse it in `_parse_agent_node` and add the key to `_AGENT_FIELDS` ([`core/flow/snapshot.py:85`](../../src/wastech_orchestrator/core/flow/snapshot.py)) — the loader rejects unknown fields fail-closed, so the field does not exist until it is listed there. Validate each name as one portable path segment (no separators, no `..`).
2. **Validation (R2).** In `_check_config_consistency` ([`core/flow/validator.py:484`](../../src/wastech_orchestrator/core/flow/validator.py)): a node with a non-empty `skills` under `strict_isolation: true` is a violation naming the node and the key. This is the config-aware layer, which is where a mode-dependent rule belongs; the config-free ceiling check stays untouched.
3. **Prompt seam (R3, R4).** Add `required_skills: list[str]` to `AgentRunRequest` and render the block in `build_effective_prompt` ([`providers/base.py:193`, `:295`](../../src/wastech_orchestrator/providers/base.py)), beside the existing context footer. The agent runner fills the field from the node. Provider-neutral by construction: no adapter learns the syntax.
4. **Claude argv (R7).** In `build_claude_argv`, extend the `--allowedTools` value with `Skill` when the request carries names and `strict_isolation` is off; raise `CONFIGURATION_ERROR` when it carries names under strict isolation ([`providers/claude.py:1011`](../../src/wastech_orchestrator/providers/claude.py)). Nothing else in the tool plan moves.
5. **Off-switch (R10–R12).** `allow_skills: bool | None = None` on `AgentNode`, parsed through the existing `_parse_tristate` ([`core/flow/snapshot.py`](../../src/wastech_orchestrator/core/flow/snapshot.py)) and carried to `AgentRunRequest` as one resolved field. Validation adds two rules: an explicit `true` under `strict_isolation: true` (R10a), and the `skills:` + `allow_skills: false` pair (R12). When the resolved value is off, Claude appends `--disable-slash-commands` and Codex appends `--disable skill_search` to the argv it already builds for feature disables. Nothing is emitted in the other two states.
6. **Existence gate (R5).** Resolve `<repo>/.claude/skills/<name>/SKILL.md` for every declared name at flow resolution / preflight and refuse the task with a message naming the node, the skill and the expected path.
7. **Record (R6).** Persist the resolved names on the node run so the prompt audit and the run summary both show them; the rendered prompt already carries the block for free.
8. **Docs + tests.** On this branch: `guide/flows/reference.md` (a node-field row per key), `guide/flows/roles.md`, `guide/config/security.md` (the advanced-mode section, including R9's "not frozen"), the three `worc-flow*` skills that author and tune flows, and the root `README.md`. Tests: flow parse/reject, both strict-mode violations (`skills:` and an explicit `allow_skills: true`), the silence of an absent key at either mode, the `skills:` + `allow_skills: false` conflict, the prompt block, both adapters' argv arms in both directions, and the missing-skill refusal. Doc-impact note for the documentation branch: touches the flow node surface and the advanced-mode description, so `configuration.md` and `worc_architecture.md` likely need a pass.

## Open questions

1. **Does `Skill` need to be auto-approved?** Step 0 answers it. If a headless `acceptEdits` run invokes an existing non-allow-listed tool without prompting, step 4 becomes optional hardening rather than a requirement.
2. **User-level skills.** An operator's "already-configured harness" often lives in `~/.claude/skills` or in a plugin, and the adapter loads `project` only. Widening to `user,project` also imports their user-global hooks, MCP servers and plugins — a much larger surface for a small gain. v1 says: the skill must live in the target repository.
3. **What a skill may not do.** A skill is arbitrary text from the target repo that can contradict the role prompt, the output contract, or the security preamble. R4 states precedence in the prompt; whether anything should _enforce_ it (and what could) is open.
4. **Codex parity.** Prompt-only until a probe shows whether `codex exec` honours a `/name` token the way Claude does. If it does not, the shipped wording must say so rather than implying symmetry.
5. **What an absent `allow_skills` resolves to.** Skills-on keeps today's advanced-mode behavior and matches a mode whose premise is the operator's freedom; `false` would match the product's determinism promise, since a skill the flow never asked for can fire on its own description. Recommended skills-on, because silently narrowing a shipped mode contradicts what its own carriers say — but it is a real choice, not an obvious one, and it is the one thing R10 leaves open.
6. **What the off-switch does not stop.** It stops skill _invocation_. A node with a shell can still `cat` a `SKILL.md` and follow it as ordinary text, and no flag reaches that. The shipped wording must say so rather than implying the node cannot see the file.
7. **Codex under strict isolation.** Whether `--ignore-user-config` plus the untrusted project layer already stops host skill discovery is not established here; `skip_host_skill_discovery` exists as its own feature flag, which suggests it does not. Worth a no-model probe, and a reason to emit the explicit disable regardless of the mode.
8. **Evaluator nodes.** "Review with our own review skill" is an obvious next ask, and an evaluator is forced `read-only` with a typed findings contract that a skill could disturb. Deliberately deferred, not refused.

## Scope / risk

Two optional node fields, three validation rules, one prompt block, and three argv tokens (one to allow, two to refuse). No new subsystem, no state, no migration (greenfield). The risk is not correctness but **honesty about the boundary**: the feature hands a node instructions the orchestrator neither froze nor reviewed, on a provider surface that advanced mode had already opened. Nothing new is weakened — but the documentation has to say that plainly, in the same change, or the mode's carriers will read the feature as a promise of control it does not make.

## Depends on

Nothing structural. It applies only inside advanced mode, so it is naturally sequenced after that mode's own work has settled; it needs no flow-format migration, and an installed flow that omits the key behaves exactly as it does today.
