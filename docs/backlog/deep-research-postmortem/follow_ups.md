# deep_research post-mortem — follow-ups

The campaign's running list of things that outlived the item that produced them: watch items, operational consequences, and deliberate non-goals. Each item's own document remains the record of what it did and why; this file exists so the residue does not have to be rediscovered by re-reading eight documents. Both entries that needed an operator answer were decided on 2026-07-27 and are recorded in their own documents — add a **Needs a decision** section back above **Watch items** if a later item raises one.

Add to it as items land. Keep an entry only while it can still cost something — delete it when it is done or has become untrue, rather than marking it resolved.

**This folder is deleted once its items land, so nothing that must outlive it may live only here.** As of 2026-07-27 everything still active has a home in the backlog root: the two unclosed gaps from the git-evidence grant in [../read-only-shell-residues.md](../read-only-shell-residues.md), the target-repo chores in `../target-resync-after-deep-research.md` (that document was removed as outdated on 2026-08-05, so the entries below are now the only copy), and the derived-docs breadcrumbs in [../main-docs-reconstruction-notes.md](../main-docs-reconstruction-notes.md). The entries below are kept as the working record until then; each says where its surviving copy is. Anything new added here must do the same.

- [Watch items](#watch-items) — nothing to do unless the world changes
- [Operational consequences](#operational-consequences) — true of every target repo, not of the code
- [Carried into `main`](#carried-into-main) — the derived-docs refresh backlog
- [Deliberate non-goals](#deliberate-non-goals) — decided against; recorded so they are re-decided, not re-discovered

## Watch items

### A granted read-only node can still redirect the push (P1.4a)

**Surviving copy: [../read-only-shell-residues.md](../read-only-shell-residues.md) §A** — this one is an unclosed gap with a designed fix, so it does not die with the folder. The three others in this section are closed by shipped documentation and need no root item; each says where.

Operator decision, 2026-07-27: Git control-state drift on a granted `read-only` node warns instead of parking. The fingerprint still fires and the warning names the drifted aspect, but nothing stops the run — so what that decision costs depends entirely on _which_ aspect drifted, and two of the three obvious ones turn out to be covered elsewhere:

- **A planted hook cannot execute.** Every orchestrator git command carries `-c core.hooksPath=<private empty dir>` ([`git_manager.py`](../../../src/wastech_orchestrator/git_manager.py), the `GIT_NULL_HOOKS_DIRNAME` rationale), so a `.git/hooks/post-commit` in the clone never runs in an orchestrator process. This was true before the decision and is unaffected by it.
- **A program-launching config driver still parks the task.** `filter.*.clean/smudge/process`, `diff.*.command/textconv`, repo-local `core.sshCommand`/`credential.helper` are refused by `_assert_no_untrusted_filters` before staging/checkout — and `commit_code` calls it, which is _after_ the node, so a mid-run plant is caught. That is a git-manager guard on the orchestrator's own command, not a node outcome, so decision 2 does not reach it.
- **Push redirection is the real residue.** `url.<attacker>.insteadOf` and `remote.*.pushurl` are not programs, so that assertion does not see them, and `push` runs `git push --set-upstream origin <branch>` without re-checking where `origin` now points. The drift _is_ detected (all repo-local keys are hashed into the fingerprint, so the warning reads `config: repo config key changed: url.….insteadof`) — but only a human reading it stops the branch from going somewhere else, and an unattended run has no human.

Deliberately not fixed, 2026-07-27: the layer beneath still holds — the provider sandbox write-denies the whole clone for this node class, so touching `.git/config` at all means that enforcement already failed. If it ever needs closing, the shape is drift, not a ban: capture `git remote get-url --push origin` (which resolves `insteadOf`/`pushurl` to the final URL) at branch prep, re-read it immediately before `push`/`push_branch_update`, and refuse on a change with the URL redacted — a pre-existing operator `pushurl` lands in the baseline and keeps working, only a mid-run rewrite refuses. About twenty lines and two tests. Revisit if a sandbox gap is found or if `deep_research` starts running unattended on a schedule.

Note the discarded middle option, so it is not re-proposed: "warn but do not publish" is not a third behavior — any refusal on the publish node _is_ `manual_action_required`, i.e. the same parking one step later.

### `--allowedTools` allow-direction semantics are pinned by a manual probe, not by the suite (P1.4a)

The Claude half of the git-evidence grant rests on a fact about `claude` 2.1.217 that no offline test can assert: under `--permission-mode dontAsk`, a `Bash` invocation matching none of the `--allowedTools` patterns is auto-**denied**. It was verified by hand (all four probes are recorded in [P1.4a](p1-4a-read-only-git-evidence.md)'s `## Implemented`), and the presence of the `--allowedTools` flag is checked at preflight — but the _semantics_ are not, because proving them needs a real model turn.

If a future CLI flips that default to auto-approve, the verb allowlist becomes decorative and the granted shell is unrestricted. The blast radius is bounded by the second layer: the OS sandbox write-denies the whole clone, so the node still cannot change the repository, and `security.denied_commands` still blocks commit/push/PR. What would be lost is the confinement to _reading git_ — the node could run arbitrary read commands.

Re-probe when pinning a new Claude CLI major, using the four probes in the item. If the behavior ever flips, the documented fallback is the one the item names: drop the verb allowlist and rely on the sandbox alone, exactly like Codex.

The reminder no longer depends on anyone reading this file: 2026-07-27 the shipped `guide/flows/reference.md` gained the verified version, the two probes to repeat, and that fallback, in the Claude bullet of its "Read-only git evidence" section — which is what an operator upgrading their CLI actually reads, whereas this entry lives on `dev`. A mechanical preflight warning (a verified-version constant compared against the detected major, gated on the switch) was weighed and declined as more machinery than a one-time re-probe deserves; it stays available if the CLI turns out to move often.

### `deep_research` can now park on a host toolchain, which it never could before (P3.10 10g)

The `document_checks` node is the flow's first `command_profile` node, and that checker is fail-closed in a way the `citation` checker is not: if a selected command's toolchain is absent on the host, or every selected check is skipped, it raises `manual_action_required` rather than failing quality — a fix loop cannot install toolchains. So a research run on a machine without the target's formatter now parks where it previously published. That is the intended behavior of a gate (the alternative is committing unchecked files, which is the defect the item exists to fix) and it matches how the `implementation` flow has always behaved. It is listed here because the flow's failure profile changed: a `deep_research` operator who has never configured `command_sets` sees no change at all, and one who configures them gains a new way for a long expensive run to stop at the last step. Note `skip_if_unavailable: true` only _half_ helps — it turns the launch failure into a loud skip, but a set that is the only one selected and then skipped leaves the gate with nothing run, which parks the task on the same path. Per-task `nodes.document_checks.enabled: false` is the clean escape.

Both of those non-obvious facts were written down on 2026-07-27 where an operator meets them rather than only here: the comment above the node in the packaged `deep_research.yaml` (read while configuring `command_sets` for it) and the `skip_if_unavailable` row of `guide/config/reference.md` (read while looking for exactly that escape hatch). The entry stays because the changed failure profile is still worth knowing before a long run, not because anything is missing.

### The git-evidence master switch does not gate Codex, and cannot (P1.4a)

`security.allow_git_evidence: false` (the default) does **not** mean "no node can read git history anywhere". It means "no node gains a capability it did not already have". A Codex `read-only` node can run `git log` today and will keep doing so with the switch off, because Codex's `read-only` sandbox permits command execution — its mutation ban comes from the workspace being mounted `read` with the network off, not from a verb list.

So the provider asymmetry [P1.4a](p1-4a-read-only-git-evidence.md)'s Problem section describes is **resolved only when the switch is on**. With it off, the same flow still has different reach depending on which provider runs the node. That is the intended behavior of an opt-in.

The misreading it invites was closed on 2026-07-27 at its source: the fact was already in `guide/flows/reference.md`, but spread across two bullets and absent from the `security.allow_git_evidence` row of `guide/config/reference.md` — the place an operator reads _about the key itself_, and where the words "master switch" do the damage. That row now says to read it as a grant switch rather than a kill switch, and names Codex's behavior with the switch off. The entry stays as a reminder of the shape, not as an outstanding doc gap.

## Operational consequences

**No surviving copy** — `../target-resync-after-deep-research.md` held these and was removed as outdated on 2026-08-05, so the three entries below are the record. They are chores in the target repo's `.worc/`, undone as of 2026-07-27; re-home them before this folder is deleted.

### Target repositories need a `.worc/flows/` refresh (P0.1, P1.4, P1.4a, P2.8, P2.9, P3.10)

A target repo that already carries `.worc/flows/deep_research.yaml` keeps running **its own** copy, so nothing the campaign changed in the packaged flow reaches it until that tree is refreshed. Everything the campaign touched in the flow has accumulated behind that one refresh:

- **[P1.4](p1-4-audit-coverage-gate.md)** — the refresh must bring the four new role files (`analysis_core.md`, `analysis_surfaces.md`, `analysis_docs_tests.md`, `coverage.md`) and drop `repository_analysis.md`, **or the flow fails to load on a missing `role_file`**. This is the only entry here that can break a run rather than merely withhold an improvement.
- **[P0.1](p0-1-evaluator-gate-severity.md)** — `gate_severity` on the packaged evaluators.
- **[P1.4a](p1-4a-read-only-git-evidence.md)** — `git_evidence: true` on the three analysis nodes. Withholding it is harmless: the nodes keep behaving as they do today.
- **[P2.8](p2-8-node-output-handoff.md)** — `output_file: report.md` on `synthesis`, plus the rewritten `verifier.md` / `critic.md`. These two go together: without the flow field, `{synthesis_path}` resolves to the node's closing message, so refreshing only the prompts would point both evaluators at a summary instead of the deliverable. Refreshing only the flow field is harmless.
- **[P2.9](p2-9-deliverable-containment.md)** — the rewritten `architecture_design.md` / `synthesis.md`. Withholding them keeps shipping the intermediate blueprint in the pull request.
- **[P3.10](p3-10-flow-and-config-hygiene.md)** — `refinement` without its `when:`, and the new `document_checks` node with its two edges. The gate is inert until the target also defines a `checks.command_sets` entry whose `paths` match the committed documents (below).

Worth doing as one refresh with one verification pass rather than six. The command, since "refresh that tree" invites a hand copy and a hand copy is exactly how the P1.4 ordering gets reversed:

```bash
worc install --reconfigure     # in the target repo
worc validate-flow deep_research
```

`--reconfigure` snapshots the existing `.worc/flows/` to a timestamped sibling (under the gitignored `.worc/`, so it never shows in `git status`) and then re-copies the packaged tree with `overwrite=True`, so all six changes land together and a YAML can never arrive without its role files. Two things it also does, worth knowing before running it:

- **it backs up and regenerates `config.yaml`** — `checks.command_sets`, Telegram wiring and any tuned `agents.retry.*` have to be re-applied from the backup afterwards. That coupling is the whole reason this refresh keeps not happening; a targeted `worc upgrade-flows` is proposed in [../upgrade-flows.md](../upgrade-flows.md);
- **it removes nothing** — copying only overwrites, so `deep_research/repository_analysis.md` stays behind as an orphan. Harmless (after the YAML refresh nothing references it), just untidy.

### The document gate needs a matching command set in the target's config (P3.10 10g)

[P3.10](p3-10-flow-and-config-hygiene.md)'s `document_checks` node runs the operator's own `checks.command_sets`, diff-selected. A repository with no set matching the committed documents selects nothing and passes vacuously — so the node is present and does nothing until the target adds one (for the repo that produced this campaign: a `docs` set with `paths: ["**/*.md"]` running its Markdown format check). Two things to get right in that set: name a _checking_ command, because one that rewrites files trips the core's green-but-dirtying guard and parks the task; and remember a catch-all set with no `paths` runs on **any** non-empty diff, so a research run would pull the whole code gate in behind it.

The second one was a hole in the shipped advice, not just in this target's config, and was closed on 2026-07-27: `guide/config/reference.md` recommends "single-root repo: one catch-all set (no `paths`)", which was written when `command_profile` only ever ran on a code diff. That row now carries the consequence for a document-producing flow and the two ways out (scope the catch-all to code, or keep it and add a documents set).

### Two target-config edits and one task-file habit the campaign did not make (P3.10 10a, 10e, 10f)

None of these live in this repository, and all three are one-liners in the target's `.worc/`:

- `config.example.yaml` at `schema_version: 24` against a packaged `31` — re-copy from the packaged file. Note this is the stale _example_; if the real `config.yaml` is what is behind, the command is `worc upgrade-config` (adds new keys from the template, strips removed ones) — copying the example over a live config would take your own settings with it.
- `agents.retry.max_blocked_s: 3600.0` against a current default of `21600.0` — a mid-run rate limit would fail the task ~5 h early. The 6 h default is chosen to outlast a provider's ~5 h usage window so a rate-limited task waits out the reset and resumes; at 3600 an expensive run that hits a subscription limit is lost instead.
- Stop setting `nodes.refinement.enabled: false` in the task file: with the `when:` predicate gone, that switch is now the _only_ thing keeping the scoping pass from running, and the pass is where an audit-shaped question gets its per-subsystem sub-questions.

There was a third config bullet — `codex.model: gpt-5.4` against a packaged `gpt-5.5` — and it was **wrong**: the packaged value is `gpt-5.4`, changed from `gpt-5.5` in `5b36af0` on 2026-07-11, before this campaign began. Verified and withdrawn 2026-07-27 in [P3.10](p3-10-flow-and-config-hygiene.md) 10f and in [postmortem.md](postmortem.md)'s finding table; the target is correct on that key. Recorded rather than quietly deleted because the same habit that produced it — checking a default from memory instead of the file — had also left `config/schema.py`'s v20 changelog entry asserting `max_blocked_s=3600.0` long after the live default became `21600.0`. That line was fixed in the same pass.

The ≈ −$0.7 reasoning trim is in the same category: the packaged flow pins no `reasoning`, so the trim is a target-side edit — and only the `architecture_design` half of it, since [P3.10](p3-10-flow-and-config-hygiene.md)'s `## Implemented` declines the `fact_verification` half.

## Carried into `main`

**Surviving copy: [../main-docs-reconstruction-notes.md](../main-docs-reconstruction-notes.md)** — the reconstruction task runs later than this folder's deletion, so that root item is the real home now and any new note belongs there. Kept below for the folder's remaining life.

The `docs/` tree is reconstructed on `main` from the merged `dev` diff as a separate task. Doc-impact notes the campaign has accumulated for it, so the reconstruction has breadcrumbs rather than a bare diff:

- **[P1.4](p1-4-audit-coverage-gate.md)** — the node-output channel now spans evaluators (`configuration.md`, the flow-authoring page's prompt-variable table); `deep_research`'s graph gained three nodes and a gate (`worc_architecture.md`, `cookbook.md`).
- **[P1.4a](p1-4a-read-only-git-evidence.md)** — a new `security.*` key and a new per-node flow field (`configuration.md`, the flow-authoring page); and the `read-only` permission profile no longer implies "no shell" (`worc_architecture.md`, `glossary.md`).
- **[P2.8](p2-8-node-output-handoff.md)** — a new per-node flow field `output_file` (`configuration.md`, the flow-authoring page's node-field table); the node-output channel can now carry a produced document rather than the node's message (`worc_architecture.md`'s handoff description, `glossary.md` if it defines the channel); `citation.json` gained `manifest_path` (`configuration.md`'s checker section).
- **[P2.9](p2-9-deliverable-containment.md)** — no engine change, but the `repository_document` story changes in prose: the structuring node writes nothing and the deliverable directory holds only the deliverable (`cookbook.md`, `worc_architecture.md`).
- **[P3.10](p3-10-flow-and-config-hygiene.md)** — `deep_research`'s graph gained a `command_profile` gate and lost `refinement`'s predicate (`worc_architecture.md`, `cookbook.md`); and the two `when:` facts are now documented as what they actually resolve (`configuration.md` — this is the one an operator is most likely to misread).
- **This walkthrough, 2026-07-27** — one behavior change and four shipped-doc clarifications, all of which the derived tree currently contradicts:
  - **WRI-009 no longer always parks.** Git control-state drift on a `read-only` node holding the git-evidence grant now warns and continues; every other profile still parks. `worc_architecture.md`'s WRI-009 description states the terminal outcome unconditionally, and `glossary.md` should not list control-state drift as an unqualified `manual_action_required` trigger. The signal is `NodeOutcome.read_only_git_drift` (carrying the redacted aspect summary, not a bool) and a third synthetic trace label `TRACE_READ_ONLY_GIT_DRIFT` joined `TRACE_REWORK_EXHAUSTED` / `TRACE_READ_ONLY_WRITE` — if `configuration.md` or the operations page enumerates the ⚠️ trace labels, it is now short one.
  - **`security.allow_git_evidence` is a grant switch, not a kill switch** — with it off, a Codex `read-only` node still reads git history, so the provider asymmetry persists (`configuration.md`'s security section).
  - **`skip_if_unavailable` is not an escape hatch** — skipping the only selected set parks the task exactly as the launch failure would; disabling the node per task is the escape (`configuration.md`'s checks section).
  - **The single-root "one catch-all set" recommendation is incomplete** once a flow produces documents: the catch-all fires on a Markdown-only diff too (`configuration.md`, and `cookbook.md` wherever it shows a first `command_sets`).
  - **The `--allowedTools` deny-direction fact is now written down for operators** — verified `claude` version, the two probes to repeat on a new major, and the sandbox-only fallback (the flow-authoring page's git-evidence section, mirroring `guide/flows/reference.md`).

## Deliberate non-goals

Decided against, not overlooked.

### P1.4a — read-only git evidence

- **No `isolation_reasons` arm for the grant.** That preflight receives only the provider config and cannot see whether any node declares `git_evidence`, so keying it on the switch alone would fail preflight for runs that never use the capability. The host check that matters is per-attempt and already lives in the adapter, with the declaration in hand — a granted shell on a host that cannot sandbox it raises `CAPABILITY_UNAVAILABLE` there. Reasoning is in the module docstring of [`security/isolation.py`](../../../src/wastech_orchestrator/security/isolation.py). Revisit only if operators start hitting the per-attempt refusal late in a run and want it earlier.
- **No evaluator-side detection, of either signal.** The field is accepted on evaluator nodes and reaches the request, but the before/after comparison lives in the agent runner only, as §3 describes. Originally that meant the working-tree write; since 2026-07-27 the same bracket also carries the Git control-state comparison, so **both** signals are missing for an evaluator, not one. Such a node is protected by the same sandbox; it simply would not report if that sandbox failed. Worth adding if a flow ever grants an evaluator the verbs in practice — tracked, with the push-redirection residue, in [../read-only-shell-residues.md](../read-only-shell-residues.md).
- **`None` and `False` on the node field carry the same behavior.** Both mean "did not ask". The tri-state is the shape operator decision 1 called for and mirrors `network_access`; what would give `False` its own meaning is a flow-wide default, and no flow needs one.
- **Codex adapter untouched.** A test pins that its profile is unchanged _and why_ — three keys, no command dimension, workspace `read`, network off, which is a stronger mutation ban than any allowlist can be.

### P2.8 — node output handoff

- **Piece 3 (the footer slot) is not scheduled.** The item itself asks for it to be argued separately, and its two halves have different costs: an upstream-output slot changes the shape of every rendered prompt in every flow, and the size-bounded `{<node_id>_content}` companion is the only part of the campaign that touches the documented "never inline content" doctrine. Neither is needed for the acceptance criteria — the packaged prompts name `{<node_id>_path}`, and after piece 1 that path is the artifact. Revisit if operators start hand-writing the same upstream reference into every role file.
- **No `{report_dir}` prompt variable.** It was the obvious way to let the verifier name the deliverable directory without the `docs/research/{task_id}` convention, and it would have cleaned `synthesis.md` too. Rejected as a new core variable for one sentence: the deliverable reaches the evaluators on the node-output channel, and the manifest's location is now published in `citation.json`. Reconsider only if a role prompt needs the _directory_ for something other than reaching a file a node produced — **and that condition is already met** by [../configurable-report-dir.md](../configurable-report-dir.md), where the variable is not a convenience but the seam without which the containment guard hard-stops the task. The cross-reference is recorded in that item too, since this folder is deleted before it is built.
- **The produced file is copied, not referenced.** `{<node_id>_path}` still points into the redacted exchange, so an evaluator grades a copy of the deliverable rather than the file the publish node will commit. That is deliberate (WRI-001 — a live repository path never enters a prompt) and the copy is byte-identical unless redaction fires, which for a research report means a secret got written into the deliverable. Worth knowing when reading a finding that quotes a line number.
