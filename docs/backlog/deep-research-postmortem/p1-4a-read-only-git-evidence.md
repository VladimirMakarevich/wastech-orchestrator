# P1.4a — read-only git evidence for an audit node

Priority: **P2** (execution position: **next, ahead of P2.8** — operator decision 2026-07-26) Status: **accepted** Date: 2026-07-26 Source: [p1-4](p1-4-audit-coverage-gate.md) change 3, [postmortem.md](postmortem.md) DR-7 sub-defect 1

Spun out of [P1.4](p1-4-audit-coverage-gate.md), which shipped changes 1 and 2 and deferred change 3. This document exists because the deferred piece is not a flag: it changes what `read-only` **means**, and four separate places in the orchestrator currently rely on the old meaning.

## Problem

An audit node's role prompt calls delivery history prime evidence — a change that did less than it claims is exactly the defect class a plan-versus-implementation audit exists to find. On the run this came from, the agent tried (its own narration: _"Let me check the git history to see which P9/P10 remediation tasks have actually landed"_), had no shell, substituted a Markdown `Grep`, and never examined a commit.

[P1.4](p1-4-audit-coverage-gate.md) closed the _contradiction_ by making the prompt capability-conditional: history is prime evidence where it is reachable with the tools you were actually granted, and with no shell you say so rather than presenting a changelog grep as history. That is honest, and it is also an admission that the capability is missing on one of the two providers.

The asymmetry is the sharpest statement of the problem. The **same flow** on the **same node**:

- under **Codex**, `read-only` is a sandbox mode that already permits command execution, so `git log` works today;
- under **Claude**, `read-only` is the absence of `Bash` from the tool set, so it cannot run any command at all.

So the flow's evidence quality depends on which provider the operator routed the node to, and neither the flow nor the prompt can express that.

## The rule this sits under

[P1.4](p1-4-audit-coverage-gate.md)'s implementation note claimed `security.md` requires an operator escape hatch for this capability. It does not — that file is four lines and mandates no such thing (P1.4's "Implemented" section carries the full correction). What it does say is that security mechanisms must not unnecessarily limit functionality, that restrictions come only from significant risks and only as the least restrictive solution that gives the necessary protection, and: **"do not silently trade functionality away for isolation."**

Read plainly, that argues _for_ this grant. Withholding read-only history inspection from an audit node — while the same node under the other provider already has it — is the shape of over-restriction the rule warns about. The reason this is a separate document is therefore **not** that the capability is unwelcome; it is that implementing it correctly touches the isolation model in four places, and that work deserves its own review rather than riding along with a graph split.

## Evidence — why this is not a flag

Every claim below was re-verified against the code at the commit that shipped P1.4.

### 1. Claude's `read-only` prevents writes by _tool absence_, and three guards ride on that

[`providers/claude.py:105`](../../../src/wastech_orchestrator/providers/claude.py) maps the profile to `("dontAsk", ("Read", "Glob", "Grep"))`. `Edit`, `Write` and `Bash` are simply not in the set — that **is** the write prevention. Consequently, adding `Bash` to a `read-only` node does not merely add git; it removes the only thing stopping `echo x > file`. Three further guards assume it cannot happen:

- [`nodes/agent.py:458`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) — `_apply_post_edit_guard` early-returns unless the node is workspace-write. A read-only node therefore gets **no** diff capture, **no** `output_policy` containment check (`_apply_output_containment_guard`), and **no** dangerous-diff gate. A shell write by a read-only node would be invisible to all three.
- [`nodes/agent.py:390-397`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) — the WRI-009 git-control fingerprint is captured only for a workspace-write attempt, and `write_guard` is left `None`, with the assumption stated in the comment: _"a read-only attempt carries no write tools, so `write_guard` stays `None`."_ A read-only node with a shell could touch `.git` and nothing would compare before against after.
- [`providers/claude.py:196`](../../../src/wastech_orchestrator/providers/claude.py) — `resolve_claude_tools` decides `needs_sandbox` inside `if profile == "workspace-write":`. A read-only node keeping `Bash` gets **no** `--settings` sandbox file, i.e. an unsandboxed shell on precisely the hosts where the adapter refuses to run an unsandboxed workspace-write shell. The same branch owns the `CAPABILITY_UNAVAILABLE` refusal on a Linux/WSL2 host missing `bwrap`/`socat`, so that refusal would not fire either.

This is the substance of the item. A verb allowlist without these four is a read-only node that can write.

### 2. One joined tool string feeds both the existence gate and the auto-approve list

[`providers/claude.py:655-656`](../../../src/wastech_orchestrator/providers/claude.py):

```python
joined_tools = ",".join(plan.tools)
argv += ["--tools", joined_tools, "--allowedTools", joined_tools]
```

`--tools` is the hard existence gate and takes bare tool names; a scoped pattern belongs in `--allowedTools`. So `ClaudeToolPlan` must carry **two** sets: `Bash` in the existence gate, `Bash(git log:*)`-style entries in the auto-approve list. The pattern grammar itself is already proven in this adapter — [`_deny_tools_for`](../../../src/wastech_orchestrator/providers/claude.py) at `claude.py:295-307` renders `security.denied_commands` as exactly `Bash(<cmd>:*)` patterns into `--disallowedTools`. It is the _allow_ direction that has never been exercised.

One behavior the design rests on and that nobody here has verified against the CLI: that under `--permission-mode dontAsk` a `Bash` invocation matching **no** `--allowedTools` pattern is auto-denied rather than auto-approved. The comment at [`claude.py:95-99`](../../../src/wastech_orchestrator/providers/claude.py) states `dontAsk` "auto-denies every non-allowlisted tool with no prompt", which is the required behavior, but that sentence is about whole tools, not about patterns within one. This must be probed before the grant is trusted — the repo already has the pattern for that (`_REQUIRED_CLAUDE_FLAGS` is probed at preflight so enum/flag drift is caught before the model runs).

### 3. Codex cannot express a verb allowlist, and does not need one

[`providers/codex_profile.py:162-166`](../../../src/wastech_orchestrator/providers/codex_profile.py) emits a profile with exactly three keys — `extends`, `filesystem`, `network` — where `read-only` extends `:read-only`, the workspace is granted `"read"` ([`:133-136`](../../../src/wastech_orchestrator/providers/codex_profile.py)) and `network.enabled` is `False`. There is no command or verb dimension anywhere in it.

That is not a gap to fill: it is a **stronger** guarantee than a verb list. `git commit` fails because `.git` is not writable; `git push` fails because there is no network. The mutation ban is enforced by the sandbox rather than by enumerating verbs, which no prompt, task or flow can talk its way around.

So P1.4's premise that "the verb allowlist is what makes the two providers behave the same rather than accidentally-different" is wrong in its mechanism. The two providers can be made to agree on the _observable_ contract — read-only history is available, mutation is impossible — while getting there by different means, and the design must say so out loud instead of pretending a symmetric allowlist exists.

## Change

### 1. Split the Claude tool plan into an existence set and an allow-pattern set

`ClaudeToolPlan` grows a second field (`allow_patterns`); `build_claude_argv` joins `plan.tools` into `--tools` and `plan.tools + plan.allow_patterns` into `--allowedTools`. No behavior change for any node that adds no patterns, which is every node today.

### 2. Re-key the sandbox decision on "does this plan keep `Bash`"

Replace `if profile == "workspace-write":` in `resolve_claude_tools` with a condition on the resolved tool set containing `Bash`. For workspace-write this is equivalent (Bash is in its baseline), so the change is behavior-preserving there and correct for the new case: a read-only node with a shell gets the OS sandbox on a host that has one, and the `CAPABILITY_UNAVAILABLE` refusal on a supported host that is missing the sandbox dependencies. `build_sandbox_settings` already tolerates `write_guard=None`, so it needs no signature change — but see 3.

### 3. Make the read-only guarantee survive the shell

The grant must not turn a read-only node into an ungoverned writer. Concretely:

- the sandbox policy for such a node denies **write to the whole workspace root**, not only the internal deny set — the shell's read-only-ness must be enforced by the sandbox, the way Codex enforces it, and not left to the goodwill of an allowlist;
- the WRI-009 git-control fingerprint is captured for it (drop the `_is_workspace_write` condition in favour of "this attempt can execute commands"), so `.git` drift is still detected;
- `_apply_post_edit_guard` stays **off** for such a node, and a diff from it is **not** fail-closed (operator decision, 2026-07-26). The sandbox denial in the first bullet is the enforcement; if a write nevertheless lands, the run reports it to the operator as a warning and continues. Rationale: the guard's whole apparatus — diff capture, `output_policy` containment, the dangerous-diff approval round-trip — exists for a node whose job is to edit, and a read-only node parking a task over a stray write would trade a real capability for a hypothetical. Use the surface that already exists for exactly this shape of signal: the console warning plus a ⚠️ Telegram trace the orchestrator emits when a non-blocking evaluator accepts with findings open.

On a host with **no** sandbox (native Windows), the choice is the same one the adapter already makes for workspace-write: drop `Bash` under `strict_isolation` and let the prompt's capability-conditional wording take over. That keeps the feature from silently becoming "unsandboxed shell on Windows".

### 4. Declaration surface

**Decided (operator, 2026-07-26): both places** — a **per-node tri-state** (e.g. `git_evidence: true | false | null`) on agent and evaluator nodes, gated by an operator **config master switch** defaulting to off.

- The per-node field follows the precedent already in the schema: `network_access` is a per-node tri-state that toggles exactly one capability dimension without touching the filesystem ceiling, and the campaign's own constraint requires every mechanism to be reachable declaratively rather than by identity. A config-only key would blanket _every_ read-only node in the run, handing a shell to `fact_verification` and `critical_review` as well, which is strictly worse.
- The config switch is what keeps the AGENTS.md invariant intact — the envelope is not weakened _through a flow node_, because with the switch off a flow's request resolves to nothing. Mirror `security.strict_isolation` / `security.disable_read_isolation` in shape and default.
- `security.denied_commands` stays the floor and is unchanged: `git commit`, `git push`, `gh pr create`, `gh pr merge` are already rendered into `--disallowedTools`, and a deny beats an allow. The read-only verb set to allow is the P1.4 list — `log`, `show`, `diff`, `blame`, `status`, `rev-list`, `rev-parse`, `ls-files`, `shortlog`, `describe`, `cat-file`, `for-each-ref`.
- The validator, the ceiling check and the preflight all learn the field, and the preflight is where the `dontAsk` pattern-denial probe from §2 belongs.

### 5. Say what each provider actually guarantees

The shipped docs must not claim a symmetric verb allowlist. For Claude: an allowlist plus an OS sandbox. For Codex: the sandbox alone, which already forbids every mutation. Both converge on one operator-visible contract — **history is readable, the repository cannot be changed, nothing is published** — and `guide/flows/reference.md` should state it in those terms.

## Alternatives rejected

- **A third `PermissionProfile` value** (`read-only-shell`). Conceptually the cleanest home — it _is_ a permission level — but `PermissionProfile` is a two-value enum threaded through the ceiling comparison (`is_same_or_stricter`), the output policy, the validator, both adapters, the config, and the shipped docs. It also forces every existing flow author to reason about a third level to gain one capability. Rejected as disproportionate; revisit if a second shell-shaped read-only capability ever appears.
- **Config-only, no per-node field.** Smallest surface, but it grants the shell to every read-only node in the run including the evaluators. Rejected on blast radius.
- **`permission_profile: workspace-write` on the analysis nodes.** Gets a shell today with no code change, and the `repository_document` output policy would confine writes to the report directory. Rejected: it grants `Edit`/`Write` to nodes whose whole point is that they cannot change what they are auditing, and it inverts P1.4's design.
- **An orchestrator-produced history artifact** (the Core runs `git log` and publishes it as a context file). No new provider capability at all, and the orchestrator already owns git. Rejected as a fixed-shape answer to an open-ended question: the useful query is "what did the change that closed this milestone actually touch", which cannot be pre-rendered without guessing. Worth reconsidering only if §1's guard work turns out larger than it looks.

## Acceptance

- An audit node declaring the capability, in a run whose operator enabled it, can execute the allowlisted read-only git verbs under both providers, and the deliverable can cite a commit.
- Every mutating verb fails under both providers, including on a host with no OS sandbox, and the failure is a provider-level refusal rather than a prompt-level convention.
- A read-only node with a shell **cannot** write to the workspace: the write is denied by the sandbox. If one lands anyway, the operator is warned (console + ⚠️ trace) and the run continues — it never parks the task.
- With the config switch off (the default) a declaring flow behaves exactly as it does today — same argv, same tool set.
- The `--allowedTools` pattern semantics are proven by a preflight probe, not assumed.

## Test

Unit on `resolve_claude_tools`: the existence set and the allow-pattern set for each profile × capability × declaration combination, including the native-Windows drop and the `LINUX_MISSING_DEPS` refusal now firing for a read-only-with-shell plan. Unit on the argv builder: `--tools` carries bare names only, `--allowedTools` carries the patterns, `--disallowedTools` still carries `denied_commands` (deny beats allow). Unit on the Codex profile: unchanged by this feature, with a test that pins _why_ (workspace `read` + `network.enabled: false` is the mutation ban). Fake-CLI integration per the `/fake-cli` skill for the end-to-end argv under both providers. A validator test that a declaring node with the config switch off is **accepted and inert** (per §4's decision, the switch is the grant, so a declaration alone is not an error). A test that a write from a read-only-with-shell node yields the operator warning and a `done` outcome, never `manual_action_required` (§3's decision).

## Scope / risk

Both provider adapters, the flow schema, the validator, the preflight, the config schema, and the shipped operator docs. This is the largest single change in the campaign and the only one that touches the isolation model, which is why it is not bundled with anything else.

Main risk: §1's four assumptions are the kind that are load-bearing without being written down anywhere central. The mitigation is that each one is named above with its file and line, and that the default-off switch means a mistake ships inert.

## Decisions (operator, 2026-07-26)

All three questions this document opened are answered; nothing is left blocking implementation.

1. **Declaration shape — both places.** Per-node tri-state plus the operator config master switch, default off (§4).
2. **A stray write is not fail-closed.** The sandbox denial is the enforcement; a write that lands anyway is an operator warning, not a parked task (§3). `_apply_post_edit_guard` stays off for a read-only node.
3. **Priority raised above [P2.8](p2-8-node-output-handoff.md).** The missing history evidence is judged to have contributed to the two release-blocking false negatives, so this is the next thing after the current pull request lands rather than a P2 tail item. The `Priority:` header stays `P2` as a severity label; the execution position is what changed.

One item is still unverified rather than undecided: whether `--permission-mode dontAsk` auto-denies a `Bash` invocation matching no `--allowedTools` pattern (§2). That is a fact about the CLI, not a choice — it is probed at preflight, and if the probe shows patterns are not enforced in the allow direction, the Claude half falls back to "sandbox only, no verb allowlist", exactly like Codex.

## Depends on

Nothing hard. Independent of the rest of the campaign; [P1.4](p1-4-audit-coverage-gate.md) has already made the prompts honest either way, so this can land whenever it is reviewed.
