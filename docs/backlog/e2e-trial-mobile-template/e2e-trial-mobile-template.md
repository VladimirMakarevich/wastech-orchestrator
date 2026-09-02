# E2E trial on `wastechlab-mobile-template` — findings

Status: **chain 002 complete** Date: 2026-09-01 → 2026-09-02 Owner: Vladimir Makarevich

A supervised end-to-end trial of the whole operator surface against a real target repo: **`wastechlab-mobile-template`** (Ionic 8 + Angular 21 + Capacitor 8, offline-first template), five queued tasks — one operator-authored decomposition (`001`, five subtasks, one branch/PR) and a four-task dependency chain (`002a → 002b → 002c → 002d`) — run one at a time through `worc run` with a human merge gate between them.

Four things are under test, and each finding below is attributed to exactly one lever:

1. **config skills** — did `worc-config` produce a correct, safe, complete `config.yaml` for this repo;
2. **flow / role skills** — did `worc-flow`, `worc-flow-role`, `worc-flow-tune` produce valid, coherent files that actually shape agent behavior;
3. **task-authoring skills** — did `worc-task` / `worc-deco-task` produce files the gate accepts and an agent can execute with no hidden context;
4. **execution quality** — did the pipeline deliver correct code, real specs and honest documentation.

This document records **findings only**. Nothing was repaired during the trial: no skill, flow, role prompt, `config.yaml`, task file or source file was edited to make a run succeed.

Two companions: [what was fixed](e2e-trial-mobile-template.fixes.md) records the repairs and the corrections reproducing them forced on the findings themselves, and [the status index](e2e-trial-mobile-template.status.md) is the one place that answers what is left — every finding, its state, and what blocks the ones still open.

## Environment under test (pinned)

| Thing | Value |
| --- | --- |
| Target repo | `wastechlab-mobile-template` @ `9098ccb7` at the start; `08aff09` after the trial's five merges |
| Orchestrator repo | `6ef994cf` (`main`) |
| Installed `worc` | `0.10.3a2.dev155+g3e472b699` — pipx **copy**, not an editable link |
| Providers | `codex 0.144.4` (`logged_in`), `claude 2.1.234` (`logged_in`) |
| Config | `schema_version: 39`, advanced mode ON, checks = `npm run lint` + `npm run build` |

**Build parity is verified, not assumed.** `3e472b699` is an ancestor of `6ef994cf`, and `git diff 3e472b699 6ef994cf -- src/wastech_orchestrator/core/flow/nodes/ src/wastech_orchestrator/config/ src/wastech_orchestrator/core/prompts.py src/wastech_orchestrator/core/decomposition.py` is **empty** — the audited source is byte-identical to what actually ran. The same modules are also identical on `origin/dev` (`48fe2f3`), so every `file:line` below resolves on all three.

## Findings

### F1 — the `review` evaluator is blind to the subtask spec in a decomposed run

**Severity: major.** **Lever: orchestrator source — [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py).**

The agent runner publishes the decomposition variables; the evaluator runner does not.

[`core/flow/nodes/agent.py:825-828`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py):

```python
if ctx.subtask_order is not None:
    variables["subtask_order"] = ctx.subtask_order
    variables["subtask_count"] = self._in.subtask_count
    variables["subtask_spec_path"] = self._in.subtask_spec_path
```

[`core/flow/nodes/evaluator.py:551-572`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — `_prompt_variables` — sets `task_id`, `stage`, `repo_path`, the `build_path_context` set (`repo`/`task_path`/`plan_path`/`diff_path`/`checks_path`/`review_path`), `memory_path`, and the generic `{<node_id>_path}` channel. **None of the three subtask variables.** `build_context_footer` ([`providers/base.py:272-289`](../../../src/wastech_orchestrator/providers/base.py)) does not carry them either — its field list is `task / plan / diff / checks / review / prior_fix / human_input / packet`.

`implementation.yaml` puts `review` in the sub-flow:

```yaml
decomposition:
  proposed_by: planning
  sub_flow: [implementation, testing, review, fixing]
```

So `review` runs **once per subtask** — `ctx.subtask_order` is live, and the evaluator already uses it for artifact namespacing (`evaluator.py:161,207,413,520,545`) — while judging that subtask's diff against the **root** task file and the shared plan only. It can neither enforce the subtask's own `## Acceptance criteria` nor hold its `## Out of scope for this subtask` boundary, because it never sees the file those live in. In this trial that file is the immutable materialized `NN-<slug>.md` the `implementation` node is pointed at as `{subtask_spec_path}` — the authoritative statement of what the subtask was allowed to do.

The same file already documents an identical omission that had to be fixed once before — `evaluator.py:_memory_path`:

> `review`/`fixing` are the reviewer-preference nodes in `packet.py`, so review most wants recurring reviewer expectations — but the evaluator runner never wired the packet, leaving `review.md`'s `{?memory_path}` block dead.

That is the same class of defect in the same function: a channel the agent runner has and the evaluator runner does not. Worth fixing as a pair (publish the subtask variables **and** re-check the generic channels for further drift) rather than one variable at a time.

#### Proven in production, on the first subtask of the first task

The `review` request for **subtask 1 of 5** (`stages/review/run-000005/1-codex/request.json`) carries `context_paths = {task_path, plan_path, diff_path}` and its 10,620-character prompt contains the word "subtask" **zero times**. It returned four findings, **three of them `blocking`**, and every one demands work belonging to a later subtask that had not run. Their `fix:` fields are those subtasks verbatim:

| Finding | `fix:` says | Actually is |
| --- | --- | --- |
| `blocking feedback.scss:14` | add `ion-action-sheet, ion-toast { --ion-safe-area-bottom: var(--safe-area-bottom); }` | subtask **02** step 1 |
| `blocking ionic-overrides.scss:10` | add `ion-modal ion-footer { … }` in `modals.scss` | subtask **03** step 1 |
| `blocking utilities.scss:109` | add `ion-content.default-bottom-space { --padding-bottom: 6rem; }` + the page sweep | subtask **04** steps 1-2 |
| `low ionic-overrides.scss:9` | rationale comments | subtask **02** steps 2-3 |

Subtask 01's own materialized spec ends:

> **Out of scope for this subtask.** Nothing consumes the token yet — that is subtasks 02 … 04. **Do not add an overlay, modal or page rule here.**

So the gate blocked the subtask for not doing what the subtask forbade. Meanwhile the `fixing` node's prompt for the same subtask reads _"You are fixing subtask 1 of 5; keep your change scoped to that subtask's spec: …"_ — two nodes in one run, holding contradictory instructions, purely because of the `agent.py` / `evaluator.py` asymmetry.

#### It is bounded, and that bound is worth stating precisely

`fixing` refused, changed nothing, and diagnosed the defect unaided — _"The reviewer graded subtask 01's diff against the whole task's acceptance criteria rather than the subtask's … A human should re-route these four findings to subtasks 02-04."_ Review round 2 then received `prior_fix: …/fixing/run-000006/fixing.out.md`, read that account, and **accepted with zero findings**. [`review.md:5`](../../../src/wastech_orchestrator/packaged/flows/implementation/review.md)'s `prior_fix` rule is what recovered it.

So this is **not** an unsatisfiable loop — an earlier draft of this entry called it a blocker and that was wrong. It costs **one wasted review+fixing cycle per subtask**: on subtask 1 that measured review 204.5s + fixing 303.6s ($2.41) + re-check + review 2, roughly **11-12 minutes** of pure waste, repeated for every subtask of every operator-authored decomposition — about an hour on this five-subtask task, for a defect whose fix is four lines.

The residual risk is the part that does not show up as wasted time: **recovery depends on the fixing agent being good enough to refuse.** A model that simply complied would have implemented subtasks 02-04 inside subtask 01, and nothing in the machinery would have caught it — the only node holding the boundary is the one being overruled.

#### The mechanism, confirmed quantitatively

The number of false blocking findings tracks the volume of later-subtask work still absent from the tree:

| Review | False blockers | Demanded |
| --- | --: | --- |
| subtask 1, round 1 | 3 | subtasks 02, 03, 04 |
| subtask 2, round 1 | 2 | 03, 04 (02 had landed) |
| subtask 2, round 3 | 3 | 03, 04 (04 split into two findings) |
| subtask 3, round 1 | 2 | 04 only — the `modals.scss` blocker vanished the moment subtask 03 landed |

That is the defect stated exactly: the reviewer holds the **root** task's whole-task acceptance criteria and charges every unfinished part of them against whichever subtask is under review. It is not occasional misjudgement; it is a systematic accounting error, and it decays only as the remaining subtasks land.

**F1 and F2 partially cancel, by luck.** After subtask 04 lands the only outstanding subtask is `05-docs`, and `review.md:18` tells the reviewer not to flag missing documentation — so F2 will suppress F1's last false blocker. Neither defect is excused by that. The practical consequence is that **they must be fixed together**: repair F2 alone (so the reviewer does judge docs-only deliverables) and subtask 4 immediately acquires a fresh false blocker about the unwritten documentation.

Convergence, finally, is **high-variance rather than bounded**: subtask 1 needed 2 review rounds, subtask 2 needed 5. The reviewer does talk itself out of the false blockers, but nothing bounds how long that takes except `budgets.review_fix`.

**FIXED**, together with F2 as this entry requires. See [what was fixed](e2e-trial-mobile-template.fixes.md#f1--f2--the-reviewer-under-decomposition).

### F2 — `review.md` tells the reviewer to ignore documentation on tasks whose deliverable _is_ documentation

**Severity: minor.** **Lever: role prompt — `<target>/.worc/flows/implementation/review.md`** (and the packaged [`packaged/flows/implementation/review.md`](../../../src/wastech_orchestrator/packaged/flows/implementation/review.md) it was tuned from).

`review.md:18`:

> The diff may be cumulative — on a shared branch it can include files committed by earlier tasks. Judge only what this task's plan changed; do not flag prior-task code as scope drift. **Documentation updates run in a later step of this flow, so do not flag missing doc changes.**

The last clause is correct for a code task — the `documentation` node runs after `review` and would make the finding moot. It is wrong for a **docs-only** deliverable, and this trial queues two: task `002a` (documentation only, "Do not touch code") and subtask `05-docs` of `001`. For those the docs are the entire product, and the sentence invites the reviewer to stand down on the only thing worth reviewing.

The fix is a condition, not a deletion: the instruction should hold only where the diff contains non-documentation changes. Runtime confirmation of whether the reviewer actually under-reviews `002a` is pending and will be recorded in the run log section below.

**FIXED** as a condition rather than a deletion — and recorded as unproven at runtime: the prediction that it would make the reviewer under-review `002a` was falsified. See [what was fixed](e2e-trial-mobile-template.fixes.md#f1--f2--the-reviewer-under-decomposition).

### F3 — `worc-deco-task` never says the root task reaches the edit node only as a footer path

**Severity: minor.** **Lever: skill — [`packaged/guide/skills/worc-deco-task/SKILL.md`](../../../src/wastech_orchestrator/packaged/guide/skills/worc-deco-task/SKILL.md)** (mirrored into the target's `.claude/skills/worc-deco-task/`; the two are byte-identical).

The skill frames the split as root-context plus steps:

> 1. **Separate root from steps.** The **root** holds the shared context (what the whole change is and why).

and is precise about the subtask side:

> The body is materialized **verbatim** into an immutable `NN-<slug>.md` spec that the edit steps (`implementation`, `fixing`) read as `{subtask_spec_path}`, so write it however the step needs.

What it never states is the asymmetry. The prompt renderer substitutes **paths only, never bodies** ([`core/prompts.py:1-38`](../../../src/wastech_orchestrator/core/prompts.py)), and none of the installed `implementation` role prompts reference `{task_path}` at all. The root task therefore reaches the executing node as one line of `build_context_footer`:

```
Context files (read them as needed; do not assume their contents):
- task: <path>
```

while the subtask spec is named in the prompt body and called an "immutable spec". "Shared context every subtask inherits" is the author's reasonable reading of the skill; "one optional footer path" is the mechanism. An author who believes the first will put a load-bearing constraint in the root and not restate it.

**This batch reproduces the gap exactly.** The root task `001` bans a specific utility class:

> do not ship a `.safe-padding-bottom` utility class

Subtask `04-page-bottom-spacing` is the subtask that edits `src/theme/utilities.scss` — the one file where that class would be written — and its body does not restate the ban. The guard is absent precisely where it is needed, and the reviewer that might have caught it is the one described in **F1**.

The skill should say plainly: a constraint that must bind one subtask belongs in that subtask's body, because the root arrives as an optional footer path and (today) is invisible to `review` under decomposition.

**Confirmed at runtime.** The rendered `planning` prompt (7,959 chars, `stages/planning/run-000002/rendered-prompt.md`) ends with exactly those two footer lines and contains no other reference to the task; the `{?memory_path}` block dropped as expected (memory is disabled). The exchange copy at `.worc-io/001-edge-to-edge-bottom-insets/task.md` **is** the full 98-line root task and does carry the `.safe-padding-bottom` ban at line 87 — so the constraint is _reachable_, merely weakly signposted. The finding is about signposting, not about the constraint being dropped.

**FIXED**, with one correction: the sentence this entry quotes was already replaced by the F1 fix, which added the "a binding constraint belongs in the subtask body" rule. What was missing is the _why_, and that is what landed — plus F14's clause in its post-F14 form (the field marks predecessors, it no longer decides which facts the successor gets). See [what was fixed](e2e-trial-mobile-template.fixes.md#f3--the-asymmetry-between-a-root-task-and-a-subtask-spec).

### F4 — `worc-config` enumerates two of the three install-written security keys

**Severity: minor.** **Lever: skill — [`packaged/guide/skills/worc-config/SKILL.md`](../../../src/wastech_orchestrator/packaged/guide/skills/worc-config/SKILL.md).**

`SKILL.md:34-37`:

> 4. Keep these unless the operator overrides them deliberately. **Two of them** are what `install` writes rather than what is safest — say which, so the operator is choosing rather than inheriting.
>    - …
>    - `strict_isolation`: `install` writes `false`, which **is** the advanced mode…
>    - `allow_git_evidence`: `install` writes `true`; it is inert beside `strict_isolation: false`…

`disable_read_isolation` is missing from the list, and it is the third key `install` writes into the `security` block — the installed config carries all three. It is default-unsafe in exactly the sense the step is about: [`config/schema.py`](../../../src/wastech_orchestrator/config/schema.py) defaults it to `True`, and `configuration.md` calls that

> a deliberate deployment-posture choice that departs from the project's own default-safe rule for isolation.

It is redundant _today_ (`strict_isolation: false` forces read-isolation off via `SecurityConfig.read_isolation_off`, so the explicit `true` changes nothing) — but that is the case for `allow_git_evidence` too, which the skill does cover, and for the same reason: it becomes load-bearing the moment `strict_isolation` goes back to `true`. An operator who hardens the master switch on this skill's advice still silently keeps read-isolation off.

**FIXED** — the step says "three of them" and the new bullet names the trap rather than only the key: hardening `strict_isolation` and leaving this one as installed keeps read-isolation off, so both lines are one edit. See [what was fixed](e2e-trial-mobile-template.fixes.md#f4--the-third-key-install-writes).

### F5 — task `002b` does not name the path of one of the two specs it requires

**Severity: nit.** **Lever: task file — `<target>/tasks/pending/002b-back-button-reference-guards.md`.**

Step 3 names one spec path explicitly and leaves the other implicit:

> - Create `src/app/pages/demos/forms/reactive-forms-demo.page.spec.ts`: …
> - **Create a spec for the modal guard**: registers at `backButtonPriority.overlayGuard`, …

and the acceptance criterion is only `Both new specs exist`. The colocated `*.spec.ts` convention makes `database-risk-confirmation.component.spec.ts` the obvious inference, and the sibling bullet sets the pattern — so this is a nit, not a defect. Recorded because a greppable acceptance criterion is what makes the rest of this batch auditable, and this one is not greppable.

**FIXED as guidance** — "make a criterion greppable", and "name every spec file the task requires", are rules in `worc-task` now. Nothing to repair: the task ran. See [what was fixed](e2e-trial-mobile-template.fixes.md#f5--f8--f16--three-lessons-with-no-artifact-left-to-repair).

### F6 — git control-state drift fires a false positive from the operator's own IDE

**Severity: minor** (noise on a security signal, not a breach). **Lever: orchestrator source — [`git_manager.py`](../../../src/wastech_orchestrator/git_manager.py), `_capture_local_config` / `_diff_config`.**

At the close of the `planning` node the run logged:

```
level=warning stage=planning drift="config: repo config key changed:
branch.feat/001-edge-to-edge-bottom-insets.vscode-merge-base"
msg="git control state changed during this node — continuing per policy; if you did not do this yourself,
stop the run and discard the clone before it is committed or pushed"
```

The key is VS Code's. `git config --local --get-regexp vscode` in the target repo returns one per branch — `branch.main.vscode-merge-base`, `branch.fix/ios-no-firebase-local-mode.vscode-merge-base`, and the new `branch.feat/001-edge-to-edge-bottom-insets.vscode-merge-base` — so the IDE writes one as it notices each branch. The node that supposedly drifted was `read-only` with `Write`/`Edit`/`MultiEdit`/`NotebookEdit` denied and `Bash(git commit:*)` / `Bash(git push:*)` denied in its own argv; it had no way to write a config key.

[`git_manager.py:1710-1727`](../../../src/wastech_orchestrator/git_manager.py) fingerprints **every** `--local`/`--worktree` key with no exclusions, and `_diff_config` ([`git_manager.py:1929-1936`](../../../src/wastech_orchestrator/git_manager.py)) reports any delta. The capture's docstring states its scope is

> exactly the agent-writable config surface

which holds only when the orchestrator owns the checkout. It does not here: `repo.local_path` is the operator's real working checkout — what `install` writes — and the `planning` request's `working_directory` confirms there is no clone at all. That surface is therefore also operator- and IDE-writable.

So the warning fires on **every** task, at whichever node runs while the IDE first sees the new branch, and tells the operator to abort and discard. The cost is not the noise itself but the desensitization: the `full-tool-access` backlog entry argues that on the shipped default this warn line is the _only_ trace of a real isolation failure, which "makes that trace part of the mitigation, not a nicety". A signal that cries wolf once per task is not that. The run continued correctly per policy and nothing was compromised.

**FIXED** by excluding that one key from the reporting capture — not by adopting the sibling's program-key filter, which would silence a planted `core.hooksPath` or a rewritten `pushurl` to solve this. The capture's "exactly the agent-writable config surface" claim and the rules file's "the fingerprint itself is never dropped" were both corrected with it. See [what was fixed](e2e-trial-mobile-template.fixes.md#f6--the-drift-signal-that-cried-wolf-once-per-task).

Worth noting the sibling asymmetry: `_untrusted_config_programs` ([`git_manager.py:1997-2015`](../../../src/wastech_orchestrator/git_manager.py)) — the gate that actually _refuses_ — is filtered to program-launching keys (`_FILTER_DRIVER_KEY_RE`, `_PROGRAM_CONFIG_KEYS`). Only the reporting path is unfiltered.

### F7 — the agent cannot run `npm run build` inside its sandbox; the Check Runner can

**Severity: major.** **Lever: orchestrator source — [`providers/claude.py`](../../../src/wastech_orchestrator/providers/claude.py), sandbox-policy generation.**

`fixing` reported the project's own build command aborting under it:

```
npm run build → exit 134
fatal error: all goroutines are asleep - deadlock!
goroutine 1 [chan receive]:
  github.com/evanw/esbuild/.../ThreadSafeWaitGroup.Wait
  main.runService ... esbuild/cmd/esbuild/service.go:160
```

It bisected carefully — restored the three changed files from `HEAD`, reproduced the abort, restored its change — and concluded _"It is a pre-existing host-toolchain failure"_.

**That conclusion is false, and the run's own ledger disproves it.** The Check Runner executed `npm run build` successfully **twice** in the same task:

```
21:38:09  check=build passed=true exit_code=0 duration_seconds=11.436
21:47:29  check=build passed=true exit_code=0 duration_seconds=11.533
```

with 200+ files freshly written into `www/`. The variable is not the tree — it is the **sandbox**. The node's generated `claude-sandbox-settings.json` is

```json
{"sandbox": {"enabled": true, "failIfUnavailable": true,
             "allowUnsandboxedCommands": false, "excludedCommands": [],
             "autoAllowBashIfSandboxed": true, …}}
```

so every agent `Bash` runs under Claude's macOS seatbelt sandbox, where esbuild's Go service deadlocks against its Node plugin host. `grep -rn "sandbox|seatbelt|bwrap" src/wastech_orchestrator/checks/` returns nothing — the Check Runner runs the command directly. Same command, two environments, two outcomes.

Why it matters beyond the noise:

1. `implementation.md`'s **Verify** section _mandates_ the agent run `npm run lint` **and** `npm run build` before finishing. Half of the required self-check is impossible for every `implementation`/`fixing` node in this repo.
2. It burns a fix round diagnosing a phantom.
3. It writes a false conclusion — "pre-existing host-toolchain failure" — into the durable run record, where an operator would act on it.
4. The latent risk is the interesting one: only `fixing.md`'s "do not work around a missing or incompatible host toolchain" rule stopped the agent from "repairing" a build that was never broken. Defense-in-depth held, but by rule rather than by a correct environment.

This is **not** the deliberate `test:ci` / Chrome gap: `npm run build` is _in_ the check set and is what the role prompts tell the agent to run. There is no config-level lever today — `excludedCommands` is emitted empty and nothing can populate it. That makes this a direct, evidenced argument for [`full-tool-access`](../full-tool-access/README.md) step 4 (`unsandboxed_commands`), which is still only proposed.

**FIXED as a contract, and this entry's own lever is half wrong.** Two of the three claims here hold: there is no config-level lever, and the role prompts do tell the agent to run the build. The third — that this is an evidenced argument for step 4 — does not survive being taken up. `npm run build`'s body lives in `package.json`, which the agent may write, so putting it in `unsandboxed_commands` buys agent-authored execution outside the sandbox; that is a worse trade than the `gh` case the ADR bans outright, and the ADR now carries the rule that excludes the whole script-runner class by construction. What actually caused every cost recorded above is that `implementation.md` mandated the build and `fixing.md` offered "incompatible host toolchain" as the category for a sandbox failure, while `review.md` had already been fixed to say the opposite. Both prompts now carry the reviewer's rule. The sandbox question itself is real but separate, and is now a named investigation in the backlog rather than a step nobody opened. See [what was fixed](e2e-trial-mobile-template.fixes.md#f7--the-build-the-agent-was-told-to-run-and-could-not).

### F9 — the security preamble's `.worc-io/` wording makes a reviewer refuse to review

**Severity: major.** **Lever: orchestrator source — `core/flow/security_preamble`.**

Review `run-000014` finished in 21.9s with `exit 0`, status `succeeded`, and a single finding:

```
severity: blocking | path: null
what: "Could not review the diff because the requested context files are under `.worc-io/`, and the
       orchestrator security contract in the prompt explicitly says not to read `.worc-io/`. The same
       prompt also lists those files as needed for review, so the request is internally contradictory."
fix:  "Run the review with a context path that is not under `.worc-io/`, or clarify that these exact
       `.worc-io/001-.../` files are permitted despite the later blanket prohibition."
```

The preamble does permit it. Verbatim from the rendered prompt:

```
- `.worc/` is the orchestrator's private runtime (state, logs, database, secrets, frozen bundles):
  do not read it and do not write it.
- `.worc-io/` is read-only input context: read only the paths you are given; never create, modify,
  move, or delete anything under it.
Read-isolation is relaxed for this run, so the filesystem sandbox may not block the paths above. Honor
these rules by choice: in particular do not read `.worc/`, `.env`, or any orchestrator-private file even
though you may be technically able to.
```

Three things make the misreading easy: the preceding bullet about the three-characters-different `.worc/` says "do not read it"; "read only the paths you are given" reads as a restriction rather than a grant; and the trailing sentence says the sandbox may not block "the paths above" before re-forbidding "any orchestrator-private file", which a reader can take to include `.worc-io/`.

It is **intermittent**, which is worse than deterministic: reviews `run-000005`, `run-000008` and `run-000011` in the _same task_ read the same `.worc-io/` paths without complaint. Same prompt, same provider, different outcome. The fix is to make the bullet an explicit grant and scope the trailing sentence to `.worc/`.

**FIXED**, and the diagnosis in this entry is wrong: under advanced mode the block forbids reading `.worc-io/` outright, so this was a flat contradiction rather than a misreading. See [what was fixed](e2e-trial-mobile-template.fixes.md#f9--the-security-preamble).

### F10 — the evaluator contract cannot express "I could not review"

**Severity: major.** **Lever: orchestrator source — [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)**, with a supporting line in `review.md`.

The F9 refusal above was accepted as an ordinary verdict — `succeeded`, `exit 0`, a structurally valid `findings` array — and routed to `fixing` as rework. Nothing in the contract distinguishes _"the diff is defective"_ from _"I was unable to look at the diff"_. `review.md:9` guards the adjacent failure ("**No findings means the diff is clean** — return an empty `findings` array, not prose. A prose 'looks good' hard-stops the task") but there is no guard for a structurally valid refusal, and `path` is explicitly nullable.

So an infrastructure complaint was handed to the one node that cannot act on it. `fixing` spent **474.4s and $2.87** correctly concluding there was nothing to fix:

> The reviewer never examined the change and named no file, line, or defect — it reported a contradiction in its own instructions. There is nothing in the diff for it to fix… It needs a human to hand the review stage a context path it is permitted to read.

With a less careful fixer the loop could consume `budgets.review_fix` and park the task on a defect that was never in the code. A blocking finding carrying no `path` and citing no source line is not rework; it is an infrastructure failure of the node, and the graph should treat it as one.

**ADDRESSED as a warning, not a gate** — the operator chose to announce the wasted round rather than prevent it. See [what was fixed](e2e-trial-mobile-template.fixes.md#f10--a-gating-verdict-that-names-no-path).

### F8 — task `001` paraphrases a repo rule more narrowly than the rule

**Severity: minor.** **Lever: task file — `<target>/tasks/pending/001-edge-to-edge-bottom-insets.md`.**

The task states the constraint as (lines 91-93):

> **Comments state their reason in their own words** — no links and no pointers into `docs/` or `.rules/` from shipped TypeScript or SCSS.

The rule it is paraphrasing, `.rules/coding-style.md:88-92`, is broader:

> **Comments must be self-contained.** A comment must be understandable on its own… It must not depend on any other file, document, or system continuing to exist. **Links and documentation references of any kind are forbidden anywhere in the codebase**… This covers URLs, tickets, issues, PRs, and paths or section anchors into `docs/**`, `.rules/**`, `AGENTS.md`, **or any other file in the repo**.

The agent complied with the narrower paraphrase and shipped a comment referencing `runtime.scss` and `see feedback.scss`. **The reviewer caught it** — a `medium` finding citing `.rules/coding-style.md` — because `review.md:1` tells the evaluator to read the rule for the area the diff touches rather than trust the task. That is the flow design working as intended, and it is worth recording as a positive alongside the defect.

Two honest qualifications. The same finding's second clause (that the comment misdescribes page breathing space) conflates design decisions **D-5** and **D-6** and is weak — the comment matched D-5. And the repo is not consistent about its own rule: `src/theme/dark-mode.scss:26`, untouched by this run, carries a shipped comment referencing `variables.scss`. So this is a drafting slip in a codebase that is itself loose here, not carelessness — hence `minor`.

The transferable lesson for task authoring: **cite a repo rule by name and let the agent read it** rather than restating it, because a restatement can only lose fidelity.

**FIXED as guidance** — that lesson is a rule in `worc-task` now, extended to the spec files a task depends on (F5). Nothing to repair: the task ran. See [what was fixed](e2e-trial-mobile-template.fixes.md#f5--f8--f16--three-lessons-with-no-artifact-left-to-repair).

## Not defects — verified and cleared

Recorded so a later reader does not re-open them.

- **The audit trail itself is complete, well-structured and clean.** 21 `prompt-audit` records, one per node run, named `<run>-<node>[-sub<NN>].json`, each carrying the full rendered prompt plus route, provider, per-attempt status and timings; `state.db` carries 60 artifacts, 14 check runs, 10 evaluations, 23 node runs, 20 provider attempts and 5 subtasks; `publish_operations` records each subtask commit with a fingerprint, the resulting SHA and `pushed_sha: None` (nothing published yet — correct). A scan of every prompt-audit record for `sk-`, `ghp_`, `bot<digits>:`, `PRIVATE KEY` and `TELEGRAM_BOT_TOKEN=` returned **no matches**; redaction holds. F13 above is a fidelity gap in one field of an otherwise strong surface.

- **`allow_git_evidence: true` is inert here and that is documented, not hidden.** No node in the `implementation` flow declares `git_evidence` (only `deep_research.yaml:81,101,121` does), and under advanced mode the grant has no capability left to add. `worc preflight` prints `git-evidence: ON (security.allow_git_evidence=true) — inert under strict_isolation=false` and the run log repeats it. The product says the true thing out loud.
- **Dropping `npm run test:ci` from the check set is the correct call, not a dodge.** `skip_if_unavailable` is a **per-set** flag keyed on the _toolchain binary_ being absent ([`config/schema.py:380-394`](../../../src/wastech_orchestrator/config/schema.py)); `npm` is present and Chrome is not, so the set would fail rather than skip. Declaring the command with `skip_if_unavailable: true` would have been worse than omitting it with the comment the config actually carries, which also gives the restore recipe.
- **`implementation.yaml` is honest about its own voided key.** Lines 158-161 state that `network_access: false` on the `documentation` node is neither a hard guarantee nor defense-in-depth under advanced mode — "every node is online there whatever this key says" — and advise pinning `provider: codex`. The operator did not take that advice, so the doc node is online; that is a config-level choice the flow warned about, not a flow defect.
- **`hitl:` on an agent node is a permission, not a gate.** `HitlPolicy` ([`core/flow/schema.py:45-48`](../../../src/wastech_orchestrator/core/flow/schema.py)) lets the agent _optionally_ emit a `human_input` signal in its typed output; it is not a forced round-trip. Only the bare `hitl` **node kind** pauses unconditionally. So `planning`'s `allow_approval: true` does not mean five Telegram approvals across five tasks.
- **The operator decomposition path does not consult `agents.decomposition.enabled`.** That key defaults to `False` ([`config/loader.py:515`](../../../src/wastech_orchestrator/config/loader.py)) and the config omits it, but `_validate_operator_subtasks` gates on `task.subtasks` alone ([`core/orchestrator.py:787`](../../../src/wastech_orchestrator/core/orchestrator.py)) and only requires the flow to carry a `decomposition:` block. `001` decomposes correctly; the run confirmed `subtask=1/5`.
- **`--allowedTools` in the argv is not a contradiction of advanced mode.** The observed `planning` argv carries `--allowedTools Read,Glob,Grep,Bash,PowerShell,TodoWrite,BashOutput,KillShell,WebFetch,WebSearch` while `SecurityConfig.strict_isolation` says "no tool allowlist reaches the agent CLI". The adapter is precise where the schema docstring is loose: `--tools` is the _hard existence gate_ and is correctly **not** emitted ([`providers/claude.py:1004-1015`](../../../src/wastech_orchestrator/providers/claude.py)), while `--allowedTools` is only the auto-approve baseline, "and the boundary has moved to `--disallowedTools`" ([`claude.py:318`](../../../src/wastech_orchestrator/providers/claude.py)). Filed here because a shallower audit reports this as a breach. The only residue is a **nit**: the `config/schema.py` wording invites exactly that misreading.

## Cosmetic drift (no lever worth spending)

- The target's installed `.worc/config.example.yaml:93` says `model: "gpt-5.4"` where the packaged copy says `"gpt-5.5"` — an artifact of having been installed at an older version. Everything else in the file is identical.
- `.worc/config.yaml:4-7` describes the `.worc/` home as gitignored "(this config included)", but `.gitignore:76-78` deliberately re-includes `!.worc/config.yaml` with its own rationale ("Track the orchestrator config so its changes are reviewable in history"). The generated header text is stale against the generated ignore rules.

### F11 — the supervisor's observe turn is blind to the node whose behavior it is explaining

**Severity: major.** **Lever: orchestrator source — [`core/supervisor.py`](../../../src/wastech_orchestrator/core/supervisor.py), `observe()` / `_step_prompt`.**

After review returned rework on subtask 2 for the fourth time, the supervisor wrote a genuinely sharp note. It named the loop shape cycle by cycle, and concluded:

> That trajectory — real work, then cosmetic work, then nothing — is the signature of a step that has exhausted its budget or is failing silently, not one converging on a fix. **It will not converge on its own. Rework cycles spent from here are wasted unless someone changes the inputs.**

and observed, correctly and usefully, that _"lint and build have presumably stayed green across all four cycles precisely because nothing changed; green here carries no information."_

Then it got the cause exactly backwards:

> Between cycle three and cycle four, **the implementer produced nothing at all** … the signature of a step that … is **failing silently**. **Recommended human action:** check whether the implementation node is **erroring or timing out** before it writes.

`fixing` had not failed. It ran 474.4s, exited 0, cost $2.87, and wrote a detailed report explaining exactly why it changed nothing. Worse, the supervisor wrote _"I verified all three findings; all are accurate"_ — endorsing the **false** blockers, never noticing that subtask 02's spec forbids touching `modals.scss` and `utilities.scss`. It reads "Fourth consecutive cycle untouched" as failure when it is compliance. An operator following its recommendation would have gone hunting a timeout that does not exist.

The cause is a wiring gap, and it is one line — [`core/supervisor.py:506`](../../../src/wastech_orchestrator/core/supervisor.py):

```python
prompt = self._step_prompt(task_id, node_id, outcome_kind, final_message, findings)
```

The observe turn receives only the **observed** node's own `final_message` plus its `findings`, and its `_run` call passes no `supervisor_packet_path` — unlike the finalize turn ([`supervisor.py:670,688`](../../../src/wastech_orchestrator/core/supervisor.py)), which _is_ grounded in the packet. So when observing an **evaluator** step, the supervisor sees the reviewer's message and the reviewer's findings and is structurally blind to what `fixing` said in the round before — precisely the evidence needed to judge whether a rework loop is productive.

**FIXED**, and not by handing the observe turn the packet: that would publish a full packet per observation to deliver one field of it. The mechanism this entry stops one step short of is the **cadence** — under `events` a `fixing` round that ends `done` is not a deviation, so no turn is ever spent on it and its report never enters the warm session, which is why the observation had only the evaluator's side of the loop. The preceding steps' own reports now reach the prompt directly. The endorsement of the false blockers is **not** fixed and is tracked as the same starvation as F1 on another surface. See [what was fixed](e2e-trial-mobile-template.fixes.md#f11--the-observation-that-judged-a-loop-from-one-side-of-it).

Size is not the obstacle: `_STEP_MESSAGE_MAX` is 500 and the fixing report's whole rationale sits in its first 500 characters, so the packet's `steps[].message` channel ([`supervisor_packet.py:318-319`](../../../src/wastech_orchestrator/core/supervisor_packet.py)) would have carried it — the observe turn simply is not given the packet. The method's own docstring already records a sibling starvation: _"without them the observation is a bare outcome label with nothing to react to, which is why the observer made no tool calls on any evaluator step of the run this came from."_

### F12 — `worc status` names the wrong node inside a decompose region

**Severity: minor** (operator surface only). **Lever: orchestrator source — the `current_node` bookkeeping the status renderer reads.**

Twice, about a minute apart, immediately after review accepted subtask 2:

```
node=documentation   subtask=3/5   fix_iterations=4
```

`documentation` is not in `decomposition.sub_flow` (`implementation.yaml:207`) and the flow states it "runs once per task (after the last subtask)" (`implementation.yaml:143-145`). It did not run. The node that actually started, at `22:30:52`, was `implementation` for subtask 3. The status surface appears to name `review`'s successor in the **main** graph without accounting for the decompose region looping back.

Functionally nothing is wrong — `subtasks/index.json` correctly showed orders 1 and 2 `committed` and 3-5 `pending`, and the graph routed correctly. But an operator watching `worc status` would believe a five-subtask task had reached its documentation stage while it was in fact starting subtask 3. The lever is named tentatively: the symptom is precisely located, the exact write site is not isolated.

**FIXED**, and the write site is `engine.py`'s region exit — where the write is deliberate, because the same line is what lets a resumed run continue past `planning`. The repair is in the driver: between subtasks the checkpoint is re-pointed at the region entry, and only the last subtask keeps the post-region node. See [what was fixed](e2e-trial-mobile-template.fixes.md#f12--the-checkpoint-that-named-the-stage-after-the-one-still-running).

### F13 — `prompt-audit` records the per-node override, not the effective model/reasoning

**Severity: minor.** **Lever: orchestrator source — the prompt-audit writer.**

`prompt_audit: true` exists so a run can be reconstructed. Across all 21 records of this task the `reasoning` field is `None` for every node but one supervisor turn, and `model` is `None` for the supervisor:

```
fixing / implementation / planning   claude   model=claude-opus-5   reasoning=None
review                               codex    model=gpt-5.5         reasoning=None
supervisor                           claude   model=None            reasoning=None | low
```

None of those nodes ran at a provider default. The same runs' `request.json` and argv show `planning` at `reasoning: "xhigh"` / `--effort xhigh`, and `review` at `-c model_reasoning_effort="xhigh"` inherited from `agents.providers.codex.reasoning`. So the **effective** value lives in `request.json` while the artifact actually named "prompt-audit" carries only the flow-node override, and the two disagree.

An operator auditing "did the reviewer really run at `xhigh`?" reads `None` and cannot answer from the audit record. Recording the resolved value — or both, as `configured` and `effective` — closes it.

**FIXED** as both values, and per attempt rather than per stage — resolving once against the route's primary would have reported `gpt-5.5` for this very node's Claude fallback. See [what was fixed](e2e-trial-mobile-template.fixes.md#f13--the-audit-record-that-could-not-answer-the-question-it-exists-for).

### F14 — the subtask handoff's factual floor is built from `depends_on`, not from what actually landed

**Severity: minor.** **Lever: orchestrator source — [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py), the handoff floor assembly.**

```python
if not unit.depends_on:
    return None                    # orchestrator.py:3178
...
for dep in unit.depends_on:        # orchestrator.py:3183
```

In an operator-authored decomposition every subtask commits to the **same branch, sequentially**, so every earlier subtask is a predecessor in fact. The floor names only the _declared_ ones. Subtask 04 declares `depends_on: ["inset-source-and-token"]`, so its brief's factual section named subtask 01 alone while 02 (`38b97c3`) and 03 (`61648c6`) were committed and were the closer precedents. The supervisor's interpretive half caught the gap and said so in the artifact:

> **Three predecessors are committed on this branch, not one.** The handoff names only subtask 01; `38b97c3` (subtask 02) and `61648c6` (subtask 03) also landed and are closer precedents for your work.

Worse edge case: a subtask with **no** `depends_on` gets `None` — no handoff at all — even with three subtasks already committed to its branch. As with F1, the compensation came from model quality, not mechanism.

This also adds a clause to **F3**: `worc-deco-task` describes `depends_on` purely as ordering ("a list of **slugs of EARLIER subtasks only**", "dependencies are linear and backward-only") and never says it _also_ decides what the next subtask is told. An author who declares only the true logical dependency — exactly what the skill's wording invites — silently narrows the brief.

**FIXED** — the floor is built from the subtask rows that carry a commit, oldest first, and `depends_on` now only _marks_ the predecessors it names instead of selecting them. The clause this entry adds to **F3** is not fixed and stays open with F3. See [what was fixed](e2e-trial-mobile-template.fixes.md#f14--the-handoff-floor-built-from-a-declaration-instead-of-from-the-branch).

### F15 — a correct finding with an invented authority

**Severity: minor.** **Lever: role prompt — `review.md`.**

Reviewing subtask 04, the evaluator filed one blocking finding:

> **Phase 04 explicitly includes the user-login sandbox** in the page-bottom-space sweep, but its `<ion-content class="ion-padding">` was left without `default-bottom-space`.

The finding is **right**, and it is the first real delivery defect the review layer caught in this run. The citation is **fabricated**: `plan/04-page-bottom-spacing.md` mentions the user-login sandbox zero times and says the opposite — "Leave full-bleed screens alone — onboarding, auth". The claim is true of the _plan_ (`plan.md:184`), not of Phase 04.

Findings feed a fixing agent, and `fixing.md`'s guard ("treat the `fix:` hint as a lead, not ground truth … re-open the source and confirm the corrected claim there") is precisely what absorbed this — the fixer cited `plan.md` instead and moved on. But a misattributed citation sends the fixer to the wrong document first, and an operator reading the finding would believe Phase 04 says something it does not. A finding that cites a document should quote it and name which artifact it is quoting.

**FIXED** — that sentence is in `review.md`'s finding-shape bullet, bought from two restatements rather than by raising the size ratchet. The file now sits at 1,795 of 1,800 with the same rules and one more. See [what was fixed](e2e-trial-mobile-template.fixes.md#f15--the-citation-and-the-32-characters-it-had-to-be-bought-with).

### F16 — subtask 04 restates a property rule as a path list, and the path list over-excludes

**Severity: minor.** **Lever: task file — `<target>/tasks/pending/subtasks/04-page-bottom-spacing.md`.**

Step 3 reads:

> **Leave full-bleed screens alone** — `src/app/pages/onboarding/`, `src/app/pages/auth/`, and anything that deliberately draws to the edge or fills the viewport without scrolling.

The headline is a **property**; the enumeration is two **paths**. `auth/components/user-login-sandbox/` sits under one of those paths and is not full-bleed — it has an `<ion-header>` toolbar and an `<ion-content class="ion-padding">` of cards. The literal reading therefore drops a page the rule intends to include, and the implementation agent took the literal reading.

`planning` did not: `plan.md:184` lists the page for the sweep with a reason — _"normal header + scrolling content ending in two buttons — a sandbox screen, not a full-bleed auth screen"_ — and `plan.md:190` names the genuinely full-bleed auth screens as `auth/components/{login,signup,forgot-password}`. `fixing` then read the exclusion by property too and added the class.

Same class as **F8** — a paraphrase that loses fidelity — but this one cost a delivery gap rather than a style violation.

**FIXED as guidance** — "a property is not a path list: state the property, mark paths as examples" is a rule in `worc-task`, with a pointer from `worc-deco-task` where it bit. See [what was fixed](e2e-trial-mobile-template.fixes.md#f5--f8--f16--three-lessons-with-no-artifact-left-to-repair).

### F17 — a task cannot contribute to its own commit message (second instance of the same gap)

**Severity: minor.** **Lever: orchestrator source — subtask commit-message assembly; or task-authoring guidance in `worc-task` / `worc-deco-task`.**

Subtask 04's acceptance criterion: _"The pages that got the class and the pages deliberately left alone are both named, with reasons, in the summary **and the commit message**."_ The run summary carries both lists. The commit message does not — `git log -1 --format=%B 9b69db0` is one line, and all five subtask commits are title-only, because the message is generated from the subtask title with no channel for an agent to add to it.

This is the same structural gap as task `001`'s "put the `grep` output in the PR description" criterion: a task can ask for content in a publication surface that no node is able to write. **Two instances in one task.** Either give an agent's summary a way in, or have the task-authoring skills warn that these surfaces are not addressable.

**FIXED as a task-file channel, and one of the two instances above is wrong:** the PR description **is** writable — it is the run summary — so only the commit message had no channel. A `commit_type` front-matter key now sets the Conventional-Commits type for all three commits a task lands (this closes F18's remaining half), the scope stays the task id, and both authoring skills say which publication surfaces a criterion may ask for. See [what was fixed](e2e-trial-mobile-template.fixes.md#f17--the-commit-message-a-task-could-not-reach-and-the-half-of-it-that-was-never-true).

### F18 — `worc merge-task` cannot control the squash commit message

**Severity: minor.** **Lever: orchestrator source — [`git_manager.py`](../../../src/wastech_orchestrator/git_manager.py), `merge_pull_request`.**

```python
args = ["pr", "merge", pr_url, f"--{strategy.value}"]  # git_manager.py:3021
```

No `--subject`, no `--body`. With `git.auto_merge_strategy: squash` the squash commit's content is therefore whatever the **repository** setting dictates. On this repo:

```
squash_merge_commit_title:   "COMMIT_OR_PR_TITLE"
squash_merge_commit_message: "COMMIT_MESSAGES"
```

so GitHub concatenates every commit message into the squash body. An operator squash-merging through `worc merge-task` inherits that silently and learns the outcome only afterwards — and on a repo whose `.rules/git-workflow.md` forbids agent-attribution trailers "in merge commits, squash commits, and PR titles and bodies", that is exactly how a forbidden trailer reaches `main`.

`merge-task --dry-run` is otherwise good — it printed status, branch, base, PR, PR state and "-> update branch w/ base, then merge via 'squash'". It did not print the message policy, which was the one thing that mattered. Reporting that in the dry-run would be the cheapest fix.

### F19 — `worc top --help` documents an argv form that argparse rejects

Severity: nit. Lever: CLI help text (`cli.py`, `top` parser `--log-file` help). `worc top --log-file PATH` is described as "the daemon log file to tail (the path passed to 'watch --log-file')". `worc watch` has no `--log-file` option: its parser carries only `--poll-seconds` and `--queue`. `--log-file` is a **parent-parser** flag, so the working form is `worc --log-file PATH watch`. Verified: `worc watch --log-file /tmp/x.log --poll-seconds 0` prints the top-level usage error; `worc --log-file /tmp/x.log watch --help` parses. Notable because the codebase already knows this: `cli_shell.start_watch`'s docstring says the parent flags go "**before** the `watch` subcommand (they are parent-parser options — appending them after `watch` is why the old auto-spawn died on an argparse error)". The lesson was learned in the spawn path and not carried into the help text an operator reads. `worc shell --log-file` is correct — it is a real option on `shell`, which spawns the daemon itself.

**FIXED** — the help names `worc --log-file PATH watch`, and its test pins both argv forms rather than the prose, since a help string is a claim about the parser. See [what was fixed](e2e-trial-mobile-template.fixes.md#f19--the-help-text-for-a-flag-that-is-not-there).

### F20 — every `worc run` task reads as "parked (no daemon)", and `rerun --continue` will start a second engine on it

Severity: **major**. Lever: orchestrator source — `cli.py` `_display_status` / `cmd_rerun`'s guard, `core/orchestrator.py` `RERUN_ELIGIBLE_STATUSES` + `plan_rerun`.

Observed live, 00:38, while `002a` was executing its `planning` node (heartbeats every 30s in the run log):

```
$ worc status
task_id=002a-back-button-ladder-docs
status=parked (no daemon)
node=planning
$ worc list
active:
  parked (no daemon)     002a-back-button-ladder-docs  ...
```

The chain, all verified in source:

1. `cmd_run` (cli.py:1846+) writes **no** PID file and installs no stop wiring — only `cmd_watch`'s daemon branch does (cli.py:1762-1810).
2. `_display_status` (cli.py:1301-1312) renders any RUNNING row as `parked (no daemon)` whenever no _watch-daemon_ PID file is alive. `worc run` therefore reports "parked" for its entire duration, in `status`, `list` and `top` alike — the docstring's claim is that such a row is "parked at its checkpoint, awaiting resume — **not executing**", which is false for the whole `run` path.
3. `cmd_rerun`'s only concurrency guard is that same watch-daemon PID (cli.py:2022-2028) — it passes.
4. `RERUN_ELIGIBLE_STATUSES` = {FAILED, MANUAL_ACTION_REQUIRED, **RUNNING**} (core/orchestrator.py:284-286), and `plan_rerun`'s own comment (orchestrator.py:1147-1150) states the assumption outright: "a `running` row reaching here is daemon-less (the 'parked (no daemon)' state)". The active-slot check at :1156 excludes the task's own id, so it does not self-block.

So `worc rerun <id> --continue` against a task that is live under `worc run` passes **every** refusal and drives a second engine over the same branch in the same clone. The mislabel is not cosmetic: it is the exact prompt that sends an operator to that command.

`run` is a documented first-class entry point — it is how this whole trial and all of task 001 were executed — so the state is routine, not exotic.

NOT tested live, deliberately: proving step 4 needs a real `rerun --continue` against the running 002a, which risks corrupting the trial's own branch. Proven statically instead; a safe live proof would need a throwaway repo.

Cheapest fix: have `cmd_run` write the same PID file (or a run-scoped liveness marker) so the liveness probe covers both executors; failing that, stop equating "RUNNING + no daemon" with stale.

**FIXED** — with the marker, not the shared PID file: writing the daemon's own `orchestrator.pid` would let `worc stop` believe it can ask a `run` to finish, and `run` has no stop wiring, so the ladder would escalate to a kill on a healthy process. Two surfaces this entry does not name were fixed with it: `stop`'s parked-slot note (which hands the operator `rerun --continue` in a sentence) and `run`'s own missing refusal against a second `run`. See [what was fixed](e2e-trial-mobile-template.fixes.md#f20--the-executor-that-recorded-nothing-about-itself).

### F21 — `review` is never given the check results on a pass, so it re-runs the gate inside the sandbox and blocks on F7's phantom

Severity: **major** (it composes F7 + F10 into a review->fixing loop that can park a task). Lever: orchestrator source — `core/flow/nodes/checks.py` `_publish_first_failure_log`; with a supporting line owed in `review.md`.

The command-profile checks node sets `{checks_path}` **only on failure** (checks.py:121-135 — "Publish the redacted first-failure log and set `{checks_path}` before the fail edge"). On a pass it stays `None`, so nothing about the checks reaches the next node.

Confirmed empirically. `stages/review/run-000045/1-codex/request.json` `context_paths`:

```
task_path, plan_path, diff_path, review_artifacts_path      <- no checks_path
```

while `state.db` had already recorded `npm run lint` and `npm run build` green for that very tree.

The codebase already argues the other way for the _other_ profile. `_publish_citation_report` (checks.py:171-186) sets `{checks_path}` "on **BOTH** outcomes", and its docstring gives the reason:

> "Leaving it to the command-profile failure path alone would deliver the report to nobody exactly when the check passed." The command profile — the one `implementation.yaml` uses — was never given the same treatment.

Why it bites here rather than being merely tidy: task 002a's acceptance criteria end with "`npm run lint` and `npm run build` pass". The reviewer is asked to judge that criterion, is handed no evidence of it, and under advanced mode has a Bash tool. So it verifies the only way left to it — by running the build itself, inside the agent sandbox, where **F7** kills it. Review round 2:

```
severity: blocking | path: null
what: "`npm run build` does not currently satisfy the task gate: it exits 139 after printing
       `Building...`. This appears environment-bound rather than caused by the markdown-only diff,
       but the acceptance criterion is still blocked on this host."
fix:  "Human/operator needs to repair or rerun the build toolchain on a working host; no repo source
       change is indicated by this failure."
```

Note the signal differs by provider — codex/gpt-5.5 gets exit **139** (SIGSEGV), claude/opus gets exit **134** (SIGABRT) — which is itself evidence the cause is the sandbox, not the tree.

That finding is `blocking`, carries `path: null`, and its own `fix:` states no repo change is indicated — **F10** exactly, second instance in this task. It routes to `fixing`, which cannot act. Task 001 never showed this because its reviewer did not run the build; 002a's does, and the result is a loop over a failure that does not exist, bounded only by `budgets.review_fix: 15`.

`review.md` reinforces it by omission: line 64 already carves out `test:ci` ("Karma cannot execute in this environment ... Do not raise a finding that the implementer failed to run the test suite") — the exact carve-out the check gate itself lacks. Nothing tells the reviewer that the Check Runner owns lint/build, that its verdict is authoritative, or that the reviewer's own sandboxed attempt is not evidence.

Cheapest fix: publish `{checks_path}` on a pass too (the citation profile's own argument), and add one line to `review.md` pointing the reviewer at it instead of the shell.

**FIXED**, both halves. See [what was fixed](e2e-trial-mobile-template.fixes.md#f21--the-check-verdict-on-a-pass).

## Reproducibility on chain 002 (task `002a`)

The second task re-ran the same pipeline on a different shape of deliverable — documentation only, no decomposition. Every defect that could apply did apply, and two of them composed into something task 001 never showed.

### F6 (IDE-driven git control-state drift) — REPRODUCED, first node of 002a

00:40:59, closing the `planning` node, verbatim shape of the 001 occurrence:

```
level=warning stage=planning drift="config: repo config key changed:
branch.feat/002a-back-button-ladder-docs.vscode-merge-base"
msg="git control state changed during this node — continuing per policy; if you did not do this
yourself, stop the run and discard the clone before it is committed or pushed"
```

Second task, second branch, same key family, same node position. Confirms F6's prediction that this fires **once per task** at whichever node runs while the IDE first notices the new branch. Run continued correctly per policy.

### F9 — REPRODUCED on 002a, and no longer "intermittent" in the reassuring direction

Review round 1 (`stages/review/run-000042`, codex, 32.7s) returned exactly one blocking finding:

```
severity: blocking | path: null
what: "I cannot perform the requested diff review under the provided constraints because the task,
       plan, and diff are only available under `.worc-io/`, while the same instructions explicitly
       say not to read `.worc-io/`. Git state is also prohibited, so there is no allowed source for
       the current diff."
fix:  "Provide the task, plan, and diff content directly in the prompt, or explicitly allow reading
       only the three listed `.worc-io/002a-back-button-ladder-docs/*` files for this review."
```

On task 001 this hit 1 review in 6. On 002a it hit the **first** review of the task. The sample is still small, but the defect is clearly not rare, and it now has a second, independent occurrence with the same misreading: the reviewer folds `.worc-io/` into the `.worc/` prohibition.

### F10 — REPRODUCED, and it is the dominant cost of 002a

The refusal was accepted as an ordinary `rework` verdict and routed to `fixing`, which spent **426.4s** establishing that there was nothing to fix ("Its own `fix:` hint asks for orchestrator-side action ... not a repo edit"). Same shape as 001's 474.4s round. The evaluator contract still cannot say "I could not review", so an infrastructure fault is still spent as a fix round.

### F7 — REPRODUCED, with harder proof and a new escalation

`fixing` again reported `npm run build` dying under it (exit 134, SIGABRT), and again wrote a confident false conclusion into the durable record:

> "**aborts on this host, environment-bound, not caused by this change** ... It reproduced identically across five runs, including with an 8 GB heap and single-threaded settings ... **a human needs to look at this host's Node/build toolchain.**"

`state.db` `check_runs` for this task refutes it outright — the Check Runner ran the same command successfully **twice**, the second time minutes _after_ that text was written:

```
npm run lint   exit 0 passed 2026-09-01T22:53:32Z
npm run build  exit 0 passed 2026-09-01T22:53:42Z
npm run lint   exit 0 passed 2026-09-01T23:01:55Z
npm run build  exit 0 passed 2026-09-01T23:02:03Z   <- after fixing's "aborts on this host"
```

The diagnosis is _more_ elaborate than 001's (five runs, an 8 GB heap, a macOS crash report, `tsc --noEmit` as a control) — the agent spends more effort the more carefully it investigates, because every observation inside the sandbox is consistent.

**New this run — the phantom caused destructive action in the operator's live checkout.** The same report states:

> "While isolating the build abort **I removed `www/`**, which held output from an earlier build."

`www/` is git-ignored with no tracked files, and the next Check Runner build repopulated it (199 files present now), so nothing was lost. But F7's cost is no longer only a wasted round plus a false record: chasing the phantom moved an agent to delete a directory in `repo.local_path`, which is the operator's real working checkout, not a clone. That is the concrete argument that F7 is a sandbox _correctness_ bug, not noise — and it raises F7 above a pure-waste finding.

## What the review-path defects cost, measured

The defects above cost nothing in delivered quality — every tripwire on the shipped code passed. What they cost is time and money, quietly, under every budget cap and with no warning emitted.

Node timeline for task `001` through subtask 2 (times local; `sec` and `cost` from each node's `result.json`):

| started | node | run | provider | sec | cost | findings |
| --- | --- | --- | --- | --: | --: | --- |
| 21:20:53 | `planning` | 000002 | claude | 515.7 | $5.10 | — |
| 21:29:31 | `implementation` | 000003 | claude | 494.7 | $4.26 | subtask 1, productive |
| 21:38:10 | `review` | 000005 | codex | 204.5 | — | **3 false blocking** (F1) |
| 21:41:35 | `supervisor` | 000005 | claude | 27.7 | $0.49 | — |
| 21:42:04 | `fixing` | 000006 | claude | 303.6 | $2.41 | refused — **wasted** |
| 21:47:30 | `review` | 000008 | codex | 164.1 | — | 0 — accepted via `prior_fix` |
| 21:50:16 | `supervisor` | 990002 | claude | 74.0 | $0.75 | — |
| 21:51:30 | `implementation` | 000009 | claude | 343.4 | $2.80 | subtask 2, productive |
| 21:57:38 | `review` | 000011 | codex | 258.9 | — | 2 false + **1 real** (F8) |
| 22:01:58 | `supervisor` | 000011 | claude | 30.6 | $0.78 | — |
| 22:02:30 | `fixing` | 000012 | claude | 221.9 | $1.87 | fixed the real one, productive |
| 22:06:34 | `review` | 000014 | codex | 21.9 | — | **1 false blocking** (F9 refusal) |
| 22:06:57 | `supervisor` | 000014 | claude | 29.3 | $0.83 | — |
| 22:07:27 | `fixing` | 000015 | claude | 474.4 | $2.87 | refused — **wasted** |
| 22:15:46 | `review` | 000017 | codex | 204.1 | — | **3 false blocking** |
| 22:19:11 | `supervisor` | 000017 | claude | 27.0 | $0.83 | the misdiagnosing note (F11) |
| 22:19:40 | `fixing` | 000018 | claude | 369.1 | $2.72 | refused — **wasted** |
| 22:26:11 | `review` | 000020 | codex | 207.6 | — | 0 — accepted, subtask 2 committed |

**$25.72 for two of five subtasks.** Split:

- **productive — $14.03**: `planning` $5.10, the two `implementation` runs $7.06, the one `fixing` that fixed
  a real finding $1.87;
- **wasted — $11.68 (45%)**: three `fixing` runs that correctly refused false findings ($2.41 + $2.87 +
  $2.72) and five `supervisor` turns ($3.68), the last of which actively misdirected.

And that understates it: **every `codex` node reports `cost: None`** — its `normalized_usage` carries token counts with no price — so the six review turns (1,061s of `gpt-5.5` at `xhigh`, four of them producing false blockers) are absent from the total. `worc`'s per-task cost is in practice the Claude half of the bill. A small finding of its own; lever, the codex adapter's usage normalization.

### The cost asymmetry runs the wrong way

Subtask 04's review produced the run's first **real** delivery finding. Fixing it took `fixing` **130.2s and $1.23** — the cheapest round of the whole task. Refusing the four false ones took:

| Round           |   Wall-clock |      Cost | Verdict                 |
| --------------- | -----------: | --------: | ----------------------- |
| `fixing` 000006 |       303.6s |     $2.41 | refused (F1)            |
| `fixing` 000015 |       474.4s |     $2.87 | refused (F9/F10)        |
| `fixing` 000018 |       369.1s |     $2.72 | refused (F1)            |
| `fixing` 000024 |       217.5s |     $1.72 | refused (F1)            |
| **total**       | **1,364.6s** | **$9.72** |                         |
| `fixing` 000030 |       130.2s |     $1.23 | **fixed a real defect** |

**7.9× the cost and 10.5× the wall-clock to reject false findings versus to fix a true one.** The reason is structural: a true finding is just work, while a false one obliges the fixer to research it, disprove it, and write a defensible negative. The system is cheapest when it is right and most expensive when it is wrong, which is exactly backwards — and it is the strongest single argument for putting F1, F9 and F10 ahead of everything else.

## Session 2 — chain 002 under `worc watch`, and the first test of auto mode

Findings from the supervised continuation on 2026-09-02: tasks `002a`-`002d`, the watch daemon, and the auto-mode surface that had never been exercised before.

### F24 — the publish path assumes `.worc/` is ignored; the installer's own `.gitignore` re-includes `config.yaml`

Severity: minor. Lever: orchestrator source — `git_manager.py` staging (and/or the installed `.gitignore` rationale).

`git_manager.py:2450-2456` states the assumption in its own docstring:

> "it stages the whole tree (`git add -A`; **`.worc/` stays ignored**)"

but the installed `.gitignore:74-78` deliberately does the opposite for one file:

```
# Track the orchestrator config so its changes are reviewable in history. It holds env-var
# NAMES only (never values) — the secrets live in the gitignored .worc/.env.
!.worc/config.yaml
```

and `git ls-files --error-unmatch .worc/config.yaml` confirms **TRACKED**.

Consequence: **an operator config change made while a task is in flight is swept into that task's commit and PR.** I reproduced it by accident — my own `auto_mode.enabled: false -> true` edit (made between 002a and 002b) shows up in 002b's working tree:

```
$ git diff --stat HEAD
 .worc/config.yaml   | 2 +-        <- orchestrator runtime config
 docs/architecture.md | 6 +-       <- the actual task
 ...
```

The previous session filed the config-header/`.gitignore` mismatch under "cosmetic drift (no lever worth spending)". It is not purely cosmetic: it has a publishing consequence, and the two halves of the product disagree in code, not just in prose. Either exclude `.worc/**` from the candidate pathspec explicitly, or drop the docstring's claim and say that a tracked config change rides along.

Natural experiment this creates, worth watching: `review.md` tells the evaluator to "judge only what this task's plan changed; do not flag prior-task code as scope drift". `.worc/config.yaml` is neither the task's nor a prior task's — it is the operator's. Whether review flags it is a free test of the scope-drift instruction. Recorded here so the outcome is judged against a prediction made before it was known.

Disclosure for the trial's own honesty: this file's change is **mine**, not the pipeline's. If it lands in 002b's PR I will say so in the PR body rather than quietly stripping it, since the repo's own `.gitignore` rationale is that config changes should be reviewable in history.

### F24 — CORRECTED after testing it

My first statement of this finding predicted that a tracked `.worc/config.yaml` change would land in the task's PR. **It did not.** PR #5's two commits contain no `.worc/` path at all (`git diff --name-only origin/main...origin/feat/002b-back-button-guards | grep '^\.worc/'` -> empty). Correcting the finding rather than leaving the prediction standing:

What is actually true:

1. **The premise is false in two docstrings.** `git_manager.py:2185` — "`.worc/` is gitignored, so `git add` skips it without a guard" — and `:2453` — "(`git add -A`; `.worc/` stays ignored)". The installed `.gitignore:74-78` re-includes `!.worc/config.yaml` deliberately, and `git ls-files` confirms it is TRACKED. So the stated reason is wrong on any installed repo.
2. **The code-commit path is safe anyway, for a different reason than the docstring gives.** `_scoped_pathspec` (git_manager.py:2182-2205) stages an **explicit list of changed code paths**, never `git add .`/`-A`. `.worc/config.yaml` is not in that list, so it cannot enter the code commit. The safety is real; the explanation attached to it is not.
3. **The residual exposure is the base-merge path**, which genuinely does use `git add -A` (`finalize_base_merge`, :2453) and justifies it with the false premise. A modified tracked `.worc/config.yaml` would be swept into that merge commit. **Untested here** — no base merge occurred in this run — so this is a code-read risk, not an observation.
4. **The review diff does include it**: `current.diff` opens with the `.worc/config.yaml` hunk, so the reviewer spends attention on a file that can never be part of the change. Harmless in practice (the agents identified and ignored it correctly, see above), but it is an inconsistency between the two diff-producing paths — publish excludes `.worc/`, review does not.

Severity drops to **nit** for what was observed, with a minor untested risk on the base-merge path. Lever: correct the two docstrings, and decide whether `current.diff` should exclude `.worc/` the way the commit pathspec effectively does.

### F25 — in auto mode the human merge gate cannot be operated with worc's own commands

Severity: **major** (workflow-level; it is the friction the whole auto-mode feature runs into). Lever: orchestrator source — `cli.py` `cmd_merge_task`'s daemon guard placement.

```
$ worc merge-task 002b-back-button-reference-guards --dry-run
merge-task: the watch daemon is running (pid 47040); stop it first with 'wastech-orchestrator stop'
exit 1
```

The guard sits at `cli.py:2335-2341`, **before** `--dry-run` is considered. Its stated reason — "merge-task updates the branch + runs gh/merge in the shared clone ... The merge flow + git ops need the idle slot" — is right for a real merge and wrong for a dry run, which mutates nothing.

The composition is what matters. With `auto_mode.enabled: true` and `git.auto_merge: false` (the configuration an operator picks when they want a human merge gate), a `depends_on` chain needs a merge between every pair of tasks; the merge needs the daemon stopped; and stopping the daemon is precisely what auto mode exists to avoid. So the supported path per link is **stop -> merge-task -> watch again**, three commands and a lost idle slot, or else merge outside worc entirely with `gh` — which is what this trial did, and what the previous session did for 001.

Two fixes, independent and both cheap:

- Let `--dry-run` past the guard (it is read-only, and inspecting the plan is exactly what an operator wants while the daemon runs).
- Give the console the verb it already implies: `worc shell`'s help lists `merge-task <id>` as "merge a reviewed PR (refuses while the daemon is up)" — the console is _attached to_ the daemon and could stop it, merge, and restart it as one operation, which is the actual operator intent.

**FIXED** — the first bullet, for `merge-task` and for `finalize` and `rerun`, which have the identical guard-above-the-flag shape this entry does not mention. The second bullet (a console verb that stops, merges and restarts) is **declined**, and each dry run now names the executor holding the clone rather than passing silently. See [what was fixed](e2e-trial-mobile-template.fixes.md#f25--a-plan-is-not-a-mutation).

Reported as major not because any single command is broken, but because it is the seam where auto mode's value proposition ("the chain advances without me") meets the safety choice most operators will make (`auto_merge: false`), and the two do not compose.

### F26 — turning on `confirm_next_task` can make the daemon unstoppable for up to `ask_timeout_s`

Severity: **major** (it degrades the stop ladder, which is a safety surface). Lever: orchestrator source — `notify/telegram.py`'s wait loop and/or `cli.py` `_confirm_next_task`.

The chain, all read from source:

1. `_confirm_next_task` (cli.py:1607-1614) passes `config.telegram.ask_timeout_s` straight into `notifier.ask_human`. This config carries `ask_timeout_s: 28800` — eight hours, a sensible value for a HITL question, inherited here without a second thought.
2. The Telegram wait loop (`notify/telegram.py:760-775`) polls `get_updates` until `deadline_monotonic` with **no cancellation hook**: `grep -c "is_cancelled" notify/telegram.py` returns **0**.
3. `_confirm_next_task` is called from inside `watch_once` (cli.py:1714), while `watch_loop` checks its stop channels only **around** ticks and during the poll sleep — never inside one.
4. `cmd_watch` does inject an `is_cancelled` predicate, but into the **FlowEngine**, which the claim gate sits outside of.

So a daemon that fires the gate and gets no reply is wedged inside a single tick for up to 8 hours. `worc stop` writes its sentinel, waits `--timeout` (default 30s), gets no confirmation, and escalates to a **tree kill** — the ladder's hardest rung reached not because anything is wrong but because the daemon is politely waiting for a Telegram reply. `worc restart` inherits the same.

Both ends of the tuning range are awkward, which is what makes it a design finding rather than a config mistake:

- **long `ask_timeout_s`** (the shipped-style value): one prompt, but a daemon that can only be killed;
- **short `ask_timeout_s`**: responsive, but the gate re-asks **every tick** — `break` only ends the current cycle, and nothing records that this task was already declined — so an operator who says no once is asked again 60 seconds later, forever, with no backoff and no "asked N times" state.

Fixes, either or both: give the ask loop the same `is_cancelled` predicate the FlowEngine gets; and give the claim gate its own timeout key rather than borrowing the HITL one, plus a per-task "declined, do not re-ask until X" memory.

**FIXED**, all three fixes this entry proposes rather than "either or both" — they turned out to be independent, and the interruptible wait alone would have left the gate re-asking every tick. One correction: the predicate was never missing from the product, only from this constructor; reverting the single `composition.py` line that now passes it is what makes the new wiring test red. See [what was fixed](e2e-trial-mobile-template.fixes.md#f26--the-claim-gate-the-stop-ladder-could-not-reach).

**This shaped the trial's own test plan**: I lowered `ask_timeout_s` before letting the gate fire, precisely to avoid wedging the daemon for eight hours overnight.

### F27 — codex `process_crashed` at ~33% on this host, with a diagnosable signature

Severity: minor for the orchestrator (it handled both perfectly), major for an operator running codex as primary. Lever: environment / codex CLI version pin — plus a docs line so it is recognised.

`provider_attempts` across chain 002:

| provider | succeeded | process_crashed |
| -------- | --------: | --------------: |
| claude   |        19 |               0 |
| codex    |         4 |           **2** |

Both failures are `review` attempt 1 (002b 23:52, 002c 00:30) and both carry the same stderr:

```
ERROR codex_models_manager::cache: failed to load models cache:
      missing field `base_instructions` at line 97 column 5
ERROR codex_models_manager::manager: failed to renew cache TTL:  (same, repeated ~8x)
```

i.e. codex 0.144.4's own model-cache schema is out of step with the cache file on disk. Not an orchestrator defect — and the orchestrator's handling is a **positive**: classified as `process_crashed`, routed to the `claude` fallback, flow continued, and on both occasions the fallback produced the better review. Recorded because (a) the rate rose through the trial (0/3 on 002a, then 1/2, then 1/1), and (b) an operator seeing `exit 1` with no final message has no way to know it is a provider-side cache problem unless someone tells them this signature.

**FIXED in the environment, and the rate in the heading above is already wrong:** the ~33% was a one-way transition, not flakiness. The cache on disk today carries no `base_instructions` anywhere and closes its first model object at exactly `line 97 column 5` — the coordinates in the stderr above — so the field is gone from the server payload rather than intermittently absent, and 0.144.4 fails every run now, not one in three. Upgraded 0.144.4 -> **0.152.1** and re-verified; the README's supported floor named the broken version and now names the verified one. See [what was fixed](e2e-trial-mobile-template.fixes.md#f27--the-provider-cli-that-could-not-read-its-own-cache).

### F28 — the "best-effort" terminal Telegram notification has no timeout, and wedged the daemon live

Severity: **major**. Lever: orchestrator source — `notify/telegram.py` `_safe_send` / `_run_sync`. **Observed, not inferred.**

Timeline:

```
02:37:34.608  "terminal exchange sealed"        <- last log line of any kind
   (silence)
02:41        `worc stop`  -> "watcher 59782 did not confirm shutdown in 30s; graceful stop is
                             still pending (kept its PID file)"
02:44        sample 59782 -> main thread ends in select_kqueue_control_impl -> kevent
                             (an asyncio selector), plus an idle `asyncio_0` worker thread.
                             No child processes. Stop sentinel present and ignored throughout.
02:47        `worc stop --force-full` -> "hard-stopped (killed its process group)"  -> reaped
```

~10 minutes wedged, immune to the cooperative stop.

The code path: `Orchestrator._notify_terminal` (`core/orchestrator.py:4885-4900`) is documented as

> "Best-effort terminal notification. **Never raises and never alters the outcome.**"

and it delivers on that with a `try/except`. But `_safe_send` (`notify/telegram.py:281-287`) wraps only exceptions:

```python
try:
    self._client.send_message(chat_id=..., text=...)  # no timeout argument
except Exception as exc:
    self._warn(f"{op} send failed", ...)
```

and the call runs through `_run_sync` (`telegram.py:585-598`) = `asyncio.run(factory())` with **no deadline anywhere**. So "never raises" is not "never hangs": a stalled Telegram send blocks the tick indefinitely, and because `watch_loop` checks its stop channels only _around_ ticks, the daemon stops answering `stop` for as long as the network does.

**This is the same family as F26 and it makes the family's real shape clear:** _any_ Telegram call made from inside a tick is unbounded and uninterruptible, and the stop ladder cannot reach it. F26 is the `ask_human` instance (bounded by `ask_timeout_s`, which is 8h here); F28 is the fire-and-forget instance (bounded by nothing at all). This config also sets `telegram.trace: true`, which calls `send_trace` after **every node** (`core/orchestrator.py:3687`) — so the exposure is not one call per task, it is one per node.

Fixes: pass an explicit timeout into `send_message` and wrap `_run_sync` in `asyncio.wait_for`; and give the notifier the `is_cancelled` predicate `cmd_watch` already builds for the FlowEngine, so the stop ladder can reach a call in flight.

**FIXED** (the bound; the cancellation half rides with F26), and one half of the fix proposed above is a no-op: `python-telegram-bot`'s own request defaults already bound every HTTP phase at 5s, so an explicit per-call timeout restates a default — what was missing is a bound on the _caller_. See [what was fixed](e2e-trial-mobile-template.fixes.md#f28--the-unbounded-telegram-call).

**Positive, in the same breath:** `stop --force-full` did exactly what it advertises — "The rung for a wedged or suspended watcher a soft stop cannot reach" — killed the process group, cleared the PID file, and reported "it resumes from its checkpoint on next start". The ladder's design anticipated this state; nothing was lost (002c was already terminal, PR #6 created).

### F18 — now demonstrated with the artifact, and the damage is not the trailer

The previous session reasoned about this from `git_manager.py:3021`. I ran it. With the daemon stopped, `worc merge-task 002c... -y` merged PR #6 and produced this on `main` (`58f16c2`):

```
SUBJECT: Root-page exit confirmation for the hardware back button (#6)
BODY:    * feat(002c-back-button-exit-confirm): Root-page exit confirmation for the hardware back button
         * chore(orchestrator): audit trail for 002c-back-button-exit-confirm
```

against the repo's policy (`gh api repos/... -> {"title":"COMMIT_OR_PR_TITLE", "message":"COMMIT_MESSAGES"}`).

Two concrete consequences, neither of which is the agent-trailer risk the original finding led with:

1. **The subject lost its Conventional Commits type.** The branch commit was `feat(002c-back-button-exit-confirm): ...`; the squash subject is bare prose. `.rules/git-workflow.md:7` requires "**Conventional Commits** ... `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`". So the tool's own merge path violates the target repo's first git rule. My four manual merges (`docs:`, `chore:`, `feat:`, `chore:`) all comply, because `--subject` let me set them.
2. **The orchestrator's internal bookkeeping leaked into `main`'s history** — `chore(orchestrator): audit trail for ...` is a private commit on the task branch that nobody merging a feature wants in the permanent record.

The trailer risk did **not** materialise here, only because the orchestrator's own commits are clean (verified). That is luck of configuration, not a guarantee: the same mechanism would carry any trailer a future commit contained.

`merge-task --dry-run` remains the cheapest fix and still does not report the one thing that mattered: it printed status / branch / base / pr / pr state / "-> update branch w/ base, then merge via 'squash'" and said nothing about what the squash message would be.

**FIXED**, including the dry-run report this entry calls the cheapest fix — and with one correction to the diagnosis: the lost Conventional Commits type was not merely an unset flag. Without `--subject` the repository's `COMMIT_OR_PR_TITLE` setting takes the **pull-request title**, which the orchestrator itself sets to the bare task title, so the subject was assembled from an orchestrator input by a GitHub setting. Both commits a task produces now come from one place. See [what was fixed](e2e-trial-mobile-template.fixes.md#f18--the-merge-message-the-tool-did-not-write).

**Cost accepted deliberately:** I used `merge-task` here _knowing_ F18, to convert a code-read into an observation. The price is one non-conventional subject line on `main` (`58f16c2`). I am not rewriting published history to tidy it; it stands as the evidence.

## Auto-mode probes — results so far

### Queue partitioning: PASS, at zero agent cost

With three eligible `queue=default` tasks pending:

```
$ worc watch --queue nosuchqueue --poll-seconds 0
watch: nothing to do (slot free, no pending tasks)      exit 0
```

The instance saw none of the real tasks. `scan_pending_sorted` (cli.py:1464-1480) filters on plain string equality before ranking, and it is the single source shared by `watch_once`, `worc list` and `worc top`, so the shown order cannot drift from the claim order. Partitioning works as documented.

**Hazard worth reporting:** `worc list` has **no** `--queue` flag (only `top` and `shell` do), and it renders `config.orchestrator.queue` only (cli.py:4423). So a task whose `queue` is misspelled is invisible in the default operator listing, with no warning that a pending file was skipped — it just is not there. The daemon is equally silent: a foreign-queue file is filtered out before the loop, so it produces no "waiting"/"skipped" line either.

### Daemon start: works, picks up in 9s

`worc --log-file .worc/logs/daemon.log watch --poll-seconds 60` 01:25:48 launched -> 01:25:57 `002b-back-button-reference-guards` claimed -> 01:25:59 `planning`. PID file `.worc/orchestrator.pid` = `{"pid": 47040, "start_time": null}`.

**F20's control case, proven A/B.** The same engine state that read `parked (no daemon)` under `worc run` now reads correctly under the daemon:

```
$ worc status      status=running   node=planning
$ worc list        active:  running   002b-back-button-reference-guards
```

Identical DB status (`RUNNING`); the only difference is the PID file. That is F20 isolated to its cause, with the daemon as the control.

### `worc top` — works; LOG panel default is inconsistent with `shell`

Renders ACTIVE / QUEUE (in true claim order) / RECENT / LOG, refreshes on `--poll-seconds`, degrades sanely without a TTY. But:

```
$ worc top                                  ->  LOG (no --log-file) / (no output)
$ worc top --log-file .worc/logs/daemon.log ->  LOG (.worc/logs/daemon.log) / <live lines>
```

`worc shell` defaults this path to `.worc/logs/daemon.log` on purpose — `cli_shell.daemon_log_path` even documents why: "Defaulting to `.worc/logs/daemon.log` (not `None`) means the tail always has a path". `worc top` does not, so the operator's live monitor shows an empty LOG panel even when the daemon is writing to exactly that conventional location. Same family as **F19**, same two commands: the log-file story is right in `shell` and wrong in `top`.

### `worc shell` — passive attach works as documented

```
shell: attached to running daemon (pid 47040) — tailing .worc/logs/daemon.log
```

`help` and `ps` rendered; `quit` left the daemon and its in-flight task running (pid 47040 still alive afterwards). It did **not** spawn a second daemon on entry — entry is passive, as designed. The `[shell]` extra is present (prompt_toolkit 3.0.53 in the pipx venv); a non-TTY stdin only warns. Worth recording as a safety positive: `help` states that `merge-task`, `finalize` and `rerun` "refuse while the daemon is up", so the console cannot race the engine it supervises.

### Minor: the daemon's startup banner is lost to stdout buffering when redirected

`cmd_watch` prints "watch: polling every 60s for git-pushed tasks (Ctrl-C or 'stop' to exit)" before the loop, but with stdout to a pipe/file Python block-buffers it, while the log stream (unbuffered) flows past it. Redirect the daemon to a file and the first thing an operator sees is a `read-isolation OFF` warning, not the banner that says the daemon came up and at what cadence.

## THE FLAGSHIP AUTO-MODE RESULT — the daemon reports a blocked chain clearly

002b reached `done` at 02:08:34 with PR #5 created (`git.auto_merge: false`, so nothing merged). One second later, in the same tick, the daemon continued its loop and said exactly why it stops:

```
02:08:35 level=info msg="task 002c-back-button-exit-confirm waiting:
                         dependency '002b-back-button-reference-guards' PR is OPEN (unmerged)"
02:08:35 level=info msg="task 002d-back-button-audit waiting:
                         dependency '002c-back-button-exit-confirm' is pending (not yet run)"
```

and repeated both every tick (02:09:38, 02:10:42, 02:11:45 — a clean 60s cadence matching `--poll-seconds 60`).

**Verdict: it announces the block, it does not spin silently.** The two messages are distinguishable and actionable — one names an unmerged PR, the other names an unrun predecessor, so an operator can tell "merge #5" from "nothing to do yet". They appear in both the `--log-file` sink and stdout.

Two caveats worth stating with it:

- They are `_LOG.info`. This config sets `logging.level: debug`, so they show. An operator running at `warning` would see **nothing at all** — the daemon prints no per-tick summary (`_summarize_watch` runs only on the single-pass branch), so at `warning` a fully blocked chain is indistinguishable from a healthy idle one.
- There is no escalation: no Telegram notice, no change of daemon state, no "blocked for N ticks" aggregation. A chain blocked overnight produces N identical pairs of INFO lines and nothing else. Cheap improvement: emit the waiting set once on transition, then only on change.

## `review -> documentation` on accept: low findings get a second, correctly-scoped pass

Review accepted with two `low` findings and the flow routed to `documentation` (no `fixing` round). The documentation node then triaged them **by remit**, which I did not expect:

- Fixed the doc one, naming both sites: "`docs/ionic-angular-best-practices.md:72` and `docs/architecture.md:127` both claimed every ladder reference ships a colocated spec. There is no `back-button.service.spec.ts` (that's 002c) — I scoped the claim to `inlineOverlay`/`overlayGuard`/`page` in both places." Verified in the merged branch: the phrase is now "References, one per level:" and "spec'd next to its source" is gone.
- **Explicitly declined the code one, and said so**: "The one open review finding I did **not** address is a code matter, not a doc one: `_confirmInFlight` ... has no spec exercising it, so deleting the flag would leave the suite green. That's a follow-up for a code step." That is the right behavior on both counts, and the explicit refusal-with-reason is what makes the residual auditable instead of lost. **Positive**, and an argument that `low` findings do not need a `fixing` round to be worth producing. Residual carried into the merge: the `_confirmInFlight` guard still has no test.

## My own error, recorded against me

While checking whether those findings were applied I ran `git checkout origin/feat/... -- .` on `main`, which staged the whole branch over the working tree **and** reverted my own `.worc/config.yaml` auto-mode edit. No orchestrator run was active (the daemon was idle between ticks) and nothing was committed or pushed from that state. Recovered with `git reset --hard origin/main` (tree verified empty afterwards) and re-applied the config edit. The right read-only tool was `git show <ref>:<path>`, which is what I used afterwards. Recorded because the trial's rule is that the target repo is observed, not perturbed, and I briefly perturbed it.

### Chaining across a merge: PASS, one tick

| time | event |
| --- | --- |
| 02:08:34 | 002b `done`, PR #5 opened, `auto_merge: false` so nothing merged |
| 02:08:35 -> 02:12:47 | five ticks, each logging 002c "PR is OPEN (unmerged)" and 002d "pending (not yet run)" |
| ~02:13:30 | I squash-merge PR #5 -> `main` `5e6b624` (clean, no agent trailers) |
| 02:13:51 | **next tick**: `refresh_repo` ff-pulls the merge, `dependency_eligibility` flips to ELIGIBLE, 002c claimed — `validated -> preparing` |
| 02:13:53 | 002c `running`, `planning` started |

~20 seconds from merge to pickup, no operator action beyond the merge. This is the auto-mode promise working end to end on a real dependency chain: block detected, block announced per tick, block cleared, next task claimed automatically.

Worth noting what makes it work: the per-tick `refresh_repo` is what notices the merge (nothing watches GitHub), and `_dependency_merged` re-probes PR state each time rather than caching a verdict.

### `worc restart --force` mid-task: PASS, and it proves the documented asymmetry

```
02:16:03  restart --force --timeout 600 --poll-seconds 60 requested
          -> .worc/orchestrator.stop sentinel written; old daemon (47040) keeps running
02:20:44  planning completes normally, 410.3s, exit 0        <- work preserved, not killed
02:20:44  level=info node_id=implementation error_class=cancelled blocked_until=None
          msg="task parked (resumable)"                       <- parked AT the next node boundary
          "watch: stopped"  exit 0
02:20:51  new daemon (pid 59782) starts and resumes node_id=implementation
```

**Seven seconds of handover, no work lost, and no confirmation prompt** — even though the new daemon read `confirm_next_task: true` (verified in the file it loaded). `grep -iE "ask_human|human_input|telegram|approval|next-task gate"` over the daemon log for the handover window returns **nothing**.

That is exactly what `AutoModeConfig`'s comment promises — "Gates new claims only — resuming an in-flight task on daemon restart is never gated" — confirmed by direct probe rather than by reading it. The cooperative soft stop also behaved as documented: it did **not** interrupt the running `planning` node, it waited out its 410s and parked on the boundary. Note this needs a `--timeout` larger than the remaining node time: the default 30s would have escalated to a tree kill 30 seconds in, discarding ~380s of `planning` work. Worth saying out loud in the docs, since "soft stop" and a 30-second default are in tension for a flow whose nodes routinely run 5-12 minutes.

### Confirms the stdout-buffering nit

The old daemon's banner — "watch: polling every 60s for git-pushed tasks (Ctrl-C or 'stop' to exit)" — appears in its captured stdout **after** every log line, immediately before "watch: stopped". It was block-buffered from 01:25 until process exit at 02:20. So under redirection the operator gets the daemon's cadence banner only when the daemon dies.

## What `analyze-task-run` caught, and what it structurally cannot

Ran it on 002a from the orchestrator repo (report: `analyze-task-run-002a.md`). It is a **good** tool: from artifacts alone it independently reconstructed the run, and reached F21, F9, F10, F7, F11 and the audit-fidelity gaps, each with the right lever. Two things it did better than my first pass: it noticed the ledger holds **two** entries for one task id after a `finalize` (double-counting hazard I had not spotted), and it framed the verdict correctly — "the run did not fail on its work, it failed on its gate".

What it cannot reach, and why — this is the case for a supervised trial on top of it:

1. **It can detect the build contradiction but not resolve it.** The skill reads `check_runs`, so it sees green builds beside an agent saying the build is broken. But it is read-only and single-repo: it cannot run `npm run build` itself, cannot run the unsandboxed `lmdb` probe, and therefore cannot tell "the host is broken and the Check Runner is lying" from "the sandbox is the variable". Three nodes in the artifacts assert the host is broken; nothing inside `.worc/` refutes them. **Falsifying a unanimous false claim required acting outside the artifact set.**
2. **Cross-run patterns are invisible.** F9's rate (1/11 on 001, then the first review of both 002a and 002b) and the four-different-diagnoses pattern only exist across tasks. A per-run post-mortem sees one instance and can only call it "an occurrence".
3. **Anything outside a task run is out of frame.** F19 (`worc top --help` naming a flag argparse rejects), F20 (`run` writes no PID file -> "parked" -> `rerun --continue` passes every guard), F23 (`finalize` leaves the task-file move uncommitted — **FIXED as visibility**: the contract not to commit is right, so both surfaces now name the move; see [what was fixed](e2e-trial-mobile-template.fixes.md#f23--the-move-nobody-was-told-about)), F25 (`merge-task` refuses under a live daemon, `--dry-run` included) and every auto-mode result are properties of the **CLI and the operator workflow**, not of a run's artifacts. The skill's map is `<target>/.worc/` plus the orchestrator source; none of these leave a trace there.
4. **It judges the diff against `task.normalized.json`, not against the upstream spec.** So it cannot see F22 — that `docs/tasks/002-...md` claimed nothing documented the ladder while `.rules/architecture.md:95` and `docs/ionic-angular-best-practices.md:53` already did (added 2026-08-26). Catching that needed the target's git history and the human-authored spec, neither of which is in its frame. **F22 REPRODUCED and repaired in the target's working tree, and this line over-cites by one file:** only `docs/ionic-angular-best-practices.md` pre-dates the spec, by 7 days — `.rules/architecture.md` was created 1h35m _after_ the claim was written, so it cannot be evidence the claim was false when made. One file is enough, and it was worse than "already documented": it described the ladder and told a routed page to unsubscribe in `ngOnDestroy`, which is the third trap the spec itself names. See [what was fixed](e2e-trial-mobile-template.fixes.md#f22--the-spec-that-contradicted-a-page-it-never-mentioned).
5. **It cannot verify the product.** It reads the diff; it does not typecheck the specs (`tsc -p tsconfig.spec.json --noEmit`), probe for a browser, or re-run the gate. My independent checks are what turned "the agent says the specs are fine" into "the specs compile".
6. **F24's correction needed an observation it never gets.** Predicting that a tracked `.worc/config.yaml` would ride into the PR, then watching a real publish disprove it, is a live-experiment result. A post-mortem of one run would have inherited my wrong reading of the `git add -A` docstring.

Net: `analyze-task-run` is strong on _this run's_ prompt/model/flow levers and should be run after every task. It is not a substitute for a supervised trial, because everything about the **operator surface**, **cross-run frequency**, and **claims that need falsifying from outside the sandbox** is structurally outside its inputs.

### F24 — corrected a SECOND time, and the severity goes back up

I first over-claimed (predicting the file would land in the PR), then under-claimed (downgrading to a nit when it did not). Both were wrong. The real cost showed up on 002d.

`.worc/config.yaml` — my operator edit — was in the review diff of three consecutive tasks, and the reviewer's verdict on the _identical_ situation escalated each time:

| task | verdict on `.worc/config.yaml` |
| --- | --- |
| 002b | noticed, deliberately ignored ("Not mine, orchestrator-private, left untouched") |
| 002c | **`low`** finding, correct, with an "unless the operator did it deliberately" hedge |
| 002d | **`blocking`** finding — twice, costing a `fixing` round that could not act |

002d round 2 stated the problem better than the evaluator contract can:

> "The prior fix correctly reports that the agent cannot clean this up because this run's contract forbids reading or writing `.worc/`; this is therefore **blocked on human/orchestrator cleanup, not another code-fix loop**."

So the cost is real: an operator config change made while tasks are running poisons the review of every task that follows, with **non-deterministic severity**, and can hard-block a task on something no agent is permitted to touch. Severity restored to **minor-to-major** depending on whether the reviewer picks `low` or `blocking` — and that variance is itself the finding.

Fix stands and is now better motivated: exclude `.worc/` from `current.diff` the way the publish pathspec already effectively excludes it from the commit. One line, and it removes a whole class of unfixable blocking findings.

**My part in it, stated plainly.** This was my contamination, not the pipeline's: I edited `.worc/config.yaml` between runs (a sanctioned auto-mode change) and left it in the working tree across four task runs. On seeing it hard-block 002d I reverted it (`git checkout -- .worc/config.yaml`), which removes the perturbation rather than rescuing the run — the running daemon had already read `auto_mode: true` into memory at start, so 002d continued unaffected and the next review sees a clean diff. Recorded because the trial's rule is to observe without perturbing, and for four runs I did perturb. The upside is that it produced the best evidence in the trial for the `current.diff` exclusion.

Note this also reverts config change #1 as a side effect: `auto_mode.enabled` is back to `false` on disk, which is the value the trial started with.

**FIXED**, and two of this entry's own claims were wrong: the contaminating surface is 36 tracked files rather than one, and the base-merge path was never leaking — a third guard refuses the commit outright. See [what was fixed](e2e-trial-mobile-template.fixes.md#f24--the-runtime-home-in-the-review-diff).

## `confirm_next_task` — the three branches

### Timeout: PASS, fail-closed, and clearly logged

Single-pass `worc watch --poll-seconds 0` with `ask_timeout_s: 90`, 002c merged so 002d eligible:

```
03:30:52  gate fires (one Telegram prompt sent)
03:32:31  level=info msg="next-task gate: not claiming 002d-back-button-audit (timeout)"
          watch: nothing to do (slot free, no pending tasks)
          EXIT=0
```

002d stayed in `tasks/pending/` — fail-closed exactly as `AutoModeConfig` documents. The reason is named (`timeout`), and `_confirm_next_task`'s log expression (`result.failure or ("denied" if result.answered else "no answer")`) means deny, silence and transport failure would each be distinguishable in the log. Good.

### F29 — the single-pass summary contradicts the gate line it just printed

Severity: nit. Lever: `cli.py` `_summarize_watch`. The two lines above are adjacent and disagree: the gate says it is **not claiming a specific pending task**, then the summary says **"no pending tasks"**. `_summarize_watch` (cli.py:3558-3562) prints that sentence whenever `results` is empty, and a gate-declined task produces no `PipelineResult`. So the operator-facing summary of a deliberate decline reads as an empty queue. It should distinguish "nothing pending" from "pending, but not claimed".

**FIXED**, and an unmerged dependency's `WAITING` skip — the same shape, and the commoner half — is reported the same way. See [what was fixed](e2e-trial-mobile-template.fixes.md#f29--no-pending-tasks-printed-under-the-name-of-a-pending-task).

### Approve / deny: NOT tested, and why I did not fake them

Both branches require a human pressing a button in the operator's Telegram chat. The operator is asleep; I have no legitimate way to answer. I did **not** reach for the bot token in `.worc/.env` to post a synthetic approval: impersonating the operator's approval would destroy the only thing the gate is for, and it is an outward action on a third-party service that was not authorised.

What can be said without running them, from `cli.py:1594-1624`: all three negative outcomes converge on one expression —

```python
approved = result.failure is None and result.answered and result.approved is True
if not approved: ... break
```

so **deny and timeout share the same code path and the same fail-closed `break`**; only the logged reason differs (`"denied"` when `result.answered`, `"no answer"` otherwise, or `result.failure`). The timeout branch I did exercise therefore covers the deny branch's _mechanism_; what remains untested is only that a real "no" button maps to `answered=True, approved=False`. Approve is genuinely untested.

### Transport unavailable: PASS, fail-closed, distinguishable reason

Config change #4 (temporary): `telegram.bot_token_env` -> an unset variable.

```
$ worc preflight
telegram: FAIL — env var(s) not set: TELEGRAM_BOT_TOKEN_MISSING_PROBE
preflight: NOT ready

$ worc watch --poll-seconds 0
level=debug reason=missing_env vars=TELEGRAM_BOT_TOKEN_MISSING_PROBE msg="telegram notifier disabled"
level=info  msg="next-task gate: not claiming 002d-back-button-audit (transport_error)"
watch: nothing to do (slot free, no pending tasks)          exit 0
```

The notifier degrades to disabled with a named reason, the gate fails closed with `transport_error` — distinct from `timeout` — and 002d stays pending. Exactly the documented fail-closed contract, and the three negative reasons are all separable in the log.

One nuance worth recording rather than filing: `AutoModeConfig`'s comment says the gate "Requires `telegram.enabled` (**preflight**)", but `worc watch` **started anyway** with preflight reporting NOT ready — preflight is a check the operator runs, not a launch gate. The behavior is safe (fail-closed), but the consequence is that an operator whose Telegram credentials break mid-flight gets a daemon that never claims anything again and says so only in one INFO line per tick. With auto mode on, that is indistinguishable at a glance from an idle queue — the same visibility gap as the WAITING lines.

## Auto-mode scorecard (all probes)

| Capability | Result | Evidence |
| --- | --- | --- |
| `auto_mode.enabled` chaining | **PASS** | 002b claimed 9s after daemon start; 002c claimed on the first tick after PR #5 merged (~20s) |
| Behaviour at a non-`done` terminal | **PASS (by design)** | 002a's `manual_action_required` predates auto mode; `watch_once` breaks on it so a manual task never chains silently |
| Blocked-chain reporting | **PASS, with a caveat** | per-tick INFO naming task + dependency + reason ("PR is OPEN (unmerged)" vs "is pending (not yet run)"); invisible at `logging.level: warning`, no escalation, no dedup |
| `confirm_next_task` — timeout | **PASS** | `not claiming 002d-back-button-audit (timeout)`, task stayed pending |
| `confirm_next_task` — transport down | **PASS** | `telegram notifier disabled` + `not claiming ... (transport_error)`, task stayed pending |
| `confirm_next_task` — deny | **not run** (needs the operator); shares the fail-closed path with timeout (`cli.py:1621-1624`) |
| `confirm_next_task` — approve | **not run** (needs the operator) |
| `poll_interval_seconds` tick | **PASS** | clean 60s cadence across 5+ idle ticks; per-tick `refresh_repo` is what noticed the merge |
| git-pushed task discovery | **partially covered** — the merge of PR #5/#6 was discovered by the same `refresh_repo` fetch/pull path; a task file pushed from another machine was **not** separately tested |
| `queue` partitioning | **PASS** | `--queue nosuchqueue --poll-seconds 0` -> "nothing to do", real tasks untouched |
| `worc watch` / `stop` / `restart` / `top` / `shell` | **PASS** | all exercised; `shell` attaches passively, `top` renders, `restart` hands over in 7s |
| restart mid-task, resume ungated | **PASS** | node finished, parked resumable, resumed 7s later with `confirm_next_task: true` and **no** prompt |
| `stop --force-full` on a wedged daemon | **PASS** | reaped the F28 wedge, cleared the PID file |
| `git.auto_merge: true` | **not run** — see below |

### `git.auto_merge: true` — deliberately not run

The remaining auto-mode question was whether the chain self-propels with `auto_merge: true` + `auto_merge_wait_for_checks`. I did not enable it. Reason: it would merge to `main` with no human gate, and F18 is now **demonstrated** rather than theoretical — `merge-task`'s squash path already produced a subject that violates the target repo's Conventional-Commits rule (`58f16c2`). Turning on unattended merging would have applied that same defective message path to every remaining link, writing more non-compliant history into a real repository, and `auto_merge` uses the same `merge_pull_request` code (`git_manager.py:3021`) with no `--subject`/`--body`. The finding is already established; repeating it unattended buys nothing and costs the repo's history. What that leaves untested: `auto_merge_wait_for_checks` behaviour against GitHub check runs, and whether a failed check blocks the merge. Recorded as a gap, with the reason.

## Cost, chain 002 (Claude-side; every codex attempt reports `usage_cost` NULL)

| task            |  Claude $ | attempts | codex attempts with no cost |
| --------------- | --------: | -------: | --------------------------: |
| 002a            |     14.45 |       10 |                           3 |
| 002b            |     20.69 |       10 |                           2 |
| 002c            |     12.42 |        7 |                           1 |
| **total (a-c)** | **47.57** |       27 |                           6 |

## Delivery assessment — 002d (audit + bookkeeping)

Triggers, all hit:

- Spec status left **`In progress`** (line 5), not Done.
- Device-verification box left **unchecked**, marked "**Not done — needs a human with a device.**"
- **Four manual checks named**, in order, each with what to observe: page guard (dirty/pristine/covered), overlay guard (own cancel path, caller's result, no navigation underneath), inline widget (closed vs open), root-page exit (prompt / second press / expiry / disarm-by-navigation).
- The `test:ci` gap recorded, plus a `Remaining` row at the top naming both human items.
- New `constants.spec.ts` pins the ladder "so a future edit to the ladder trips a test rather than needing this grep re-run by hand" — turning a one-off audit into a standing guard. Not asked for.
- The audit table classifies all **ten** `subscribeWithPriority` hits (4 call sites, 4 spec harnesses, 1 doc comment, 1 doc code sample) and checks each call site against FR-1/2/3/4 individually.

### The audit found a real defect in the ladder itself — verified independently

> "the one finding (`page` shares `99` with Ionic's menu dismissal) is recorded with both candidate fixes and why neither belongs here"

Confirmed against the installed Ionic, which the agent did not quote and I checked myself — `node_modules/@ionic/core/dist/collection/utils/hardware-back-button.js`:

```
OVERLAY_BACK_BUTTON_PRIORITY = 100
MENU_BACK_BUTTON_PRIORITY    = 99
```

So `backButtonPriority.page = 99` **collides with Ionic's own menu dismissal**, and the tie-break is "whoever subscribed last" — precisely the ordering-dependent failure the ladder exists to prevent. It also means the spec's own **AC-10** ("no two live handlers share a level") is violated by the shipped constants, on a rung the whole chain was built on. The audit task found this, recorded it with candidate fixes, and correctly declined to change a shipped constant inside an audit task. That is the single most valuable thing any node produced in this trial.

### F7's worst consequence: the phantom reached the product

002d wrote the sandbox artifact into a **committed repository document**, twice:

- the `Remaining` row: "the implementation environment has no Chrome binary and **its bundler crashes for reasons unrelated to this work**";
- the DoD: "`npm run build` **could not be observed**: on the implementation host the bundler aborts partway with a bare native crash (exit `134`/`139`, no diagnostic) ... so **it is an environment fault**".

The agent's epistemics are careful — "could not be observed" rather than "is broken", and it noted the failure reproduces with the change reverted. But the conclusion is still false, and it is now heading for `main` as a durable claim about the operator's machine. The Chrome half is true; the bundler half is the sandbox. This is F7 escalating from "a false line in a run artifact" to "a false line in the repository", which is the strongest possible argument for fixing it. I corrected this at the merge gate (recorded below with what I changed).

## Run log

Filled in per task as the trial proceeds. Baseline tripwires on the pre-run tree: `grep -rn "safe-area-inset" src/` = **12** hits; `--safe-area-bottom` undefined; two real `subscribeWithPriority` call sites (`searchable-select.component.ts:178` at `inlineOverlay`, `back-button.service.ts:48` at `app`).

| Task | Shape | Status | PR | Notes |
| --- | --- | --- | --- | --- |
| `001-edge-to-edge-bottom-insets` | operator decomposition, 5 subtasks | **done, merged** | [#2](https://github.com/VladimirMakarevich/wastechlab-mobile-template/pull/2) → `main` `5c19180` | 2h23m31s, **$55.86** (Claude only; 11 codex review turns report no cost). `refinement` skipped by `when`. Nodes: planning 1 · implementation 5 · testing 11 (**11/11 passed, zero check failures**) · review 11 · fixing 6 · documentation 1 · publish 1 · supervisor 11. `fix_iterations` 6, **all review-driven**. Review rounds per subtask 2/5/2/2/1. No provider fallback, no retry, no HITL prompt, nothing parked. Every task tripwire passed on the merged result, and `npm run lint` + `npm run build` pass on `main`. |
| `002a-back-button-ladder-docs` | plain, docs only | **parked, then closed by hand** | [#3](https://github.com/VladimirMakarevich/wastechlab-mobile-template/pull/3) → `7155cf3` (+ [#4](https://github.com/VladimirMakarevich/wastechlab-mobile-template/pull/4) `baa24b4` for the task-file move) | 39m23s, **$14.45** Claude (3 codex review turns report no cost). Terminal `manual_action_required`, `reason=no_file_change`, `pr_url=None`, **nothing committed**. 3 review rounds, all rework: F9 refusal, then F21's phantom build twice. Both `fixing` rounds correctly changed nothing. 41% of spend wasted. The one real review finding (unsafe copy-this-shape advice) was applied by hand at the gate. |
| `002b-back-button-reference-guards` | plain, code + specs + i18n | **done** | [#5](https://github.com/VladimirMakarevich/wastechlab-mobile-template/pull/5) → `5e6b624` | 43m, **$20.69** Claude. First auto-mode task: claimed 9s after the daemon started. 2 review rounds (F9 refusal, then accept). **First provider fallback of the trial** — codex `process_crashed`, claude took over and produced the trial's best review (a mutation-coverage gap with a designed test). `documentation` then triaged the two `low` findings by remit. |
| `002c-back-button-exit-confirm` | plain, service + spec + i18n | **done** | [#6](https://github.com/VladimirMakarevich/wastechlab-mobile-template/pull/6) → `58f16c2` via `worc merge-task` | 24m, **$12.42** Claude. Accepted on the **first** review round — the only task in the trial with zero rework. Restart-mid-task probe ran here: soft stop waited out a 410s node, resumed 7s later, ungated. The merge produced F18's artifact: a squash subject with no Conventional Commits prefix. |
| `002d-back-button-audit` | plain, audit + bookkeeping | **done** | [#7](https://github.com/VladimirMakarevich/wastechlab-mobile-template/pull/7) → `08aff09` | 34m, **$13.20** Claude. Two `blocking` rounds on **my** `.worc/config.yaml` edit (F24), then accept with **zero findings** once I reverted it. Found a real ladder defect: `page` (99) collides with Ionic's `MENU_BACK_BUTTON_PRIORITY = 99`, verified independently. Wrote F7's phantom into a repo document; corrected at the merge gate. |
