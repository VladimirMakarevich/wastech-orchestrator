# P1.4a — read-only git evidence for an audit node

Priority: **P2** (execution position: **next, ahead of P2.8** — operator decision 2026-07-26) Status: **implemented** Date: 2026-07-26 Source: [p1-4](p1-4-audit-coverage-gate.md) change 3, [postmortem.md](postmortem.md) DR-7 sub-defect 1

Spun out of [P1.4](p1-4-audit-coverage-gate.md), which shipped changes 1 and 2 and deferred change 3. This document exists because the deferred piece is not a flag: it changes what `read-only` **means**, and four separate places in the orchestrator currently rely on the old meaning.

## Implemented

Open items, watch items and deliberate non-goals left behind by this work are carried in the campaign's [follow_ups.md](follow_ups.md) — the `.git`-drift reading below is the one that wants an operator yes/no.

All five changes, plus the declaration on the three `deep_research` analysis nodes this document was written about. The grant ships **inert**: `security.allow_git_evidence` defaults to `false`, so a declaring flow validates, loads and runs exactly as it did before.

### The probe first: `--allowedTools` patterns are enforced in the allow direction

The one thing this design rested on and nobody had checked. Four probes against a real `claude` 2.1.217 in a throwaway git repo, all under the argv the adapter builds (`--permission-mode dontAsk`, `--setting-sources ""`, `--strict-mcp-config`):

1. **A `Bash` call matching no pattern is auto-denied.** `--tools Read,Glob,Grep,Bash --allowedTools "Read,Glob,Grep,Bash(git log:*)"`, asked to run `echo PWNED > pwned.txt`: the tool result came back `is_error`, tagged `"non_execution_kind": "permission-rule"`, the terminal event listed the call under `permission_denials`, and no file appeared. This is the behavior the grant needs, so the Claude half stands as designed rather than falling back to "sandbox only".
2. **A matching pattern is auto-approved.** `git log --oneline -1` ran clean with `permission_denials: []` — the capability is real, not merely un-denied.
3. **A bare tool name in `--allowedTools` overrides its own narrower pattern.** With `--allowedTools "Read,Glob,Grep,Bash,Bash(git log:*)"` the same `echo PWNED > …` **succeeded** and the file was created. This one contradicts §1 of this document and is the reason the implementation deviates from it — see below.
4. **A compound command riding a matching prefix is denied.** `git log --oneline -1 && echo PWNED_D > pwned_d.txt` was refused outright, so the matcher is not a naive prefix test.

### Deviation from §1: the two lists must be disjoint per tool, not concatenated

§1 says `build_claude_argv` joins `plan.tools` into `--tools` and `plan.tools + plan.allow_patterns` into `--allowedTools`. Probe 3 shows that would ship a fully open shell: the bare `Bash` in the auto-approve list wins over `Bash(git log:*)` sitting beside it, and every command would be approved. Implemented instead as `ClaudeToolPlan.allowed_tools`, which drops any tool name that a pattern scopes and appends the patterns: `--tools` gets `Read,Glob,Grep,Bash`, `--allowedTools` gets `Read,Glob,Grep,Bash(git log:*),…`. A plan that scopes nothing produces the identical joined string on both flags, so today's argv is byte-for-byte unchanged — pinned by a test on both profiles.

### The rest, as specified

- **§2 re-keying.** `resolve_claude_tools` now branches on `"Bash" in tools` rather than on the profile name, and takes a `git_evidence` flag. Workspace-write is unaffected (Bash is in its baseline, so the same arm is selected); a granted read-only node now gets the OS sandbox on a sandbox host, the `LINUX_MISSING_DEPS` refusal on a Linux/WSL2 host without `bwrap`+`socat`, and the Bash drop on native Windows under `strict_isolation`. The grant is guarded on `"Bash" not in tools`, so on workspace-write it is a no-op — scoping a shell that is already unscoped would be a restriction wearing the name of a capability.
- **§3 read-only-ness.** `build_sandbox_settings` grew `deny_write_root`; the adapter passes the clone root for a read-only attempt, so the sandbox — not the allowlist — is what holds the node to reading. The git control-state fingerprint is now captured for "this attempt can execute commands" (`_can_run_commands`) rather than for workspace-write alone. `_apply_post_edit_guard` stays off.
- **§4 declaration surface.** Per-node tri-state `git_evidence` on agent and evaluator nodes (schema, snapshot loader with a shared `_parse_tristate`, validator, ceiling check) plus `security.allow_git_evidence`, default off, wired through the loader, the config writer, `config.example.yaml`, `preflight`, the run log, and the config reference. `resolve_git_evidence(node_value, allowed)` is the single place both halves are required. The verb list is P1.4's, unchanged, and `security.denied_commands` is untouched.
- **§5 documentation.** `guide/flows/reference.md` gained a "Read-only git evidence" section that states the observable contract — history readable, repository unchangeable, nothing published — and then says plainly that Claude reaches it with an allowlist plus an OS sandbox while Codex reaches it with the sandbox alone. No symmetric verb list is claimed anywhere. The rest of the shipped guide follows the field where it already tracks its siblings: the node-field tables and the `roles.md` evaluator note, the `flows/README.md` foot-guns and the `config/README.md` security bullets, and the three authoring skills (`worc-flow-tune`'s knob list, `worc-config`'s safe-defaults checklist, `worc-flow`'s do-not-over-grant rule — the last one steers an author away from the workaround this document rejected, raising an audit node to `workspace-write` just to get a shell).

### Decisions taken during implementation

- **A stray write warns; `.git` drift still parks.** Operator decision 2 says a read-only node with a shell must never park the task, and that is implemented for a **working-tree write**: `NodeOutcome.read_only_write` drives a console warning plus a ⚠️ `TRACE_READ_ONLY_WRITE` trace, the outcome stays `done`, and nothing downstream is handed the change. Git **control-state** drift is left on its existing path (`NodeManualRequired`). The two are different events: a stray file is the accident decision 2 is about, while a rewritten `.git/config` or hook is the WRI-009 security violation this document's §1 named as an unwatched hole — and §1 asks for the fingerprint so drift "is still detected", which is only meaningful if detection still acts. Downgrading it here would weaken an existing invariant for exactly the node class that just gained a shell. Flagged rather than assumed: if the intent was that even `.git` poisoning only warns, that is a one-line change at the compare site.
- **The write check is before-versus-after, not "is the tree dirty".** An absolute check would blame a granted read-only node for a diff an earlier `workspace-write` node left behind. The change set is snapshotted before the node runs and compared after, and only for a node that actually holds the grant — nothing else pays for the two `git` calls.
- **`git_evidence` on a workspace-write node is a validation error.** It would be inert there (see §2 above), and a flag that silently does nothing reads as protection. The message points at the fix.
- **`--allowedTools` joined `_REQUIRED_CLAUDE_FLAGS`.** The confinement of a granted shell rests on that flag, and the adapter already passes it unconditionally, so a CLI that dropped it was going to fail at runtime anyway — the preflight probe now catches it before a paid call. This is the "probe at preflight" §4 asks for; the _semantics_ question above is a fact about the CLI that a `--help` grep cannot answer, so it was settled by the manual probes and recorded here.
- **The three `deep_research` analysis nodes declare it.** They are the nodes this document was written about, and leaving the capability reachable-but-unused would mean hand-editing a packaged flow to get any benefit. Inert by default. Their prompts' capability-conditional "delivery evidence" wording is deliberately **left as it is** — it stays correct after the grant, since the same flow still runs without a shell whenever the switch is off, the provider is Claude on native Windows, or the node is routed somewhere without a sandbox.

### Deliberately not done

- **No `isolation_reasons` arm for the grant.** That preflight sees only provider config and cannot tell whether any node declares `git_evidence`, so keying it on the switch alone would fail preflight for runs that never use the capability. The host check that matters is per-attempt and already lives in the adapter, with the declaration in hand. Reasoning recorded in `security/isolation.py`.
- **Codex untouched.** No code change; a test pins that its profile is unchanged _and why_ — three keys, no verb dimension, workspace mounted `read`, network off, which is a stronger mutation ban than any allowlist.
- **No evaluator-side write detection.** The field is accepted on evaluator nodes and reaches the request, but the before/after tree comparison lives in the agent runner only, as §3 describes. An evaluator with a granted shell is protected by the same sandbox; it just would not produce the warning if that sandbox failed.
- **The `None` / `False` distinction on the node field carries no behavior.** Both mean "did not ask". The tri-state is the shape operator decision 1 called for and mirrors `network_access`; a flow-wide default would be the thing that gives `False` its own meaning, and no flow needs one.

Doc impact for the `main` refresh: a new `security.*` key and a new per-node flow field (`configuration.md`, the flow-authoring page), and the read-only permission profile no longer implies "no shell" (`worc_architecture.md`, `glossary.md`).

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
