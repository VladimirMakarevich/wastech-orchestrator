# E2E trial on `wastechlab-mobile-template` — findings

Status: **in progress** Date: 2026-09-01 Owner: Vladimir Makarevich

A supervised end-to-end trial of the whole operator surface against a real target repo:
**`wastechlab-mobile-template`** (Ionic 8 + Angular 21 + Capacitor 8, offline-first template), five queued tasks —
one operator-authored decomposition (`001`, five subtasks, one branch/PR) and a four-task dependency chain
(`002a → 002b → 002c → 002d`) — run one at a time through `worc run` with a human merge gate between them.

Four things are under test, and each finding below is attributed to exactly one lever:

1. **config skills** — did `worc-config` produce a correct, safe, complete `config.yaml` for this repo;
2. **flow / role skills** — did `worc-flow`, `worc-flow-role`, `worc-flow-tune` produce valid, coherent files that
   actually shape agent behavior;
3. **task-authoring skills** — did `worc-task` / `worc-deco-task` produce files the gate accepts and an agent can
   execute with no hidden context;
4. **execution quality** — did the pipeline deliver correct code, real specs and honest documentation.

This document records **findings only**. Nothing was repaired during the trial: no skill, flow, role prompt,
`config.yaml`, task file or source file was edited to make a run succeed.

## Environment under test (pinned)

| Thing | Value |
| --- | --- |
| Target repo | `wastechlab-mobile-template` @ `9098ccb7`, `main`, clean |
| Orchestrator repo | `6ef994cf` (`main`) |
| Installed `worc` | `0.10.3a2.dev155+g3e472b699` — pipx **copy**, not an editable link |
| Providers | `codex 0.144.4` (`logged_in`), `claude 2.1.234` (`logged_in`) |
| Config | `schema_version: 39`, advanced mode ON, checks = `npm run lint` + `npm run build` |

**Build parity is verified, not assumed.** `3e472b699` is an ancestor of `6ef994cf`, and
`git diff 3e472b699 6ef994cf -- src/wastech_orchestrator/core/flow/nodes/ src/wastech_orchestrator/config/
src/wastech_orchestrator/core/prompts.py src/wastech_orchestrator/core/decomposition.py` is **empty** — the audited
source is byte-identical to what actually ran. The same modules are also identical on `origin/dev` (`48fe2f3`), so
every `file:line` below resolves on all three.

## Findings

### F1 — the `review` evaluator is blind to the subtask spec in a decomposed run

**Severity: major.** **Lever: orchestrator source —
[`core/flow/nodes/evaluator.py`](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py).**

The agent runner publishes the decomposition variables; the evaluator runner does not.

[`core/flow/nodes/agent.py:825-828`](../../src/wastech_orchestrator/core/flow/nodes/agent.py):

```python
if ctx.subtask_order is not None:
    variables["subtask_order"] = ctx.subtask_order
    variables["subtask_count"] = self._in.subtask_count
    variables["subtask_spec_path"] = self._in.subtask_spec_path
```

[`core/flow/nodes/evaluator.py:551-572`](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) —
`_prompt_variables` — sets `task_id`, `stage`, `repo_path`, the `build_path_context` set
(`repo`/`task_path`/`plan_path`/`diff_path`/`checks_path`/`review_path`), `memory_path`, and the generic
`{<node_id>_path}` channel. **None of the three subtask variables.** `build_context_footer`
([`providers/base.py:272-289`](../../src/wastech_orchestrator/providers/base.py)) does not carry them either — its
field list is `task / plan / diff / checks / review / prior_fix / human_input / packet`.

`implementation.yaml` puts `review` in the sub-flow:

```yaml
decomposition:
  proposed_by: planning
  sub_flow: [implementation, testing, review, fixing]
```

So `review` runs **once per subtask** — `ctx.subtask_order` is live, and the evaluator already uses it for artifact
namespacing (`evaluator.py:161,207,413,520,545`) — while judging that subtask's diff against the **root** task file
and the shared plan only. It can neither enforce the subtask's own `## Acceptance criteria` nor hold its
`## Out of scope for this subtask` boundary, because it never sees the file those live in. In this trial that file is
the immutable materialized `NN-<slug>.md` the `implementation` node is pointed at as `{subtask_spec_path}` — the
authoritative statement of what the subtask was allowed to do.

The same file already documents an identical omission that had to be fixed once before —
`evaluator.py:_memory_path`:

> `review`/`fixing` are the reviewer-preference nodes in `packet.py`, so review most wants recurring reviewer
> expectations — but the evaluator runner never wired the packet, leaving `review.md`'s `{?memory_path}` block dead.

That is the same class of defect in the same function: a channel the agent runner has and the evaluator runner does
not. Worth fixing as a pair (publish the subtask variables **and** re-check the generic channels for further drift)
rather than one variable at a time.

#### Proven in production, on the first subtask of the first task

The `review` request for **subtask 1 of 5** (`stages/review/run-000005/1-codex/request.json`) carries
`context_paths = {task_path, plan_path, diff_path}` and its 10,620-character prompt contains the word "subtask"
**zero times**. It returned four findings, **three of them `blocking`**, and every one demands work belonging to a
later subtask that had not run. Their `fix:` fields are those subtasks verbatim:

| Finding | `fix:` says | Actually is |
| --- | --- | --- |
| `blocking feedback.scss:14` | add `ion-action-sheet, ion-toast { --ion-safe-area-bottom: var(--safe-area-bottom); }` | subtask **02** step 1 |
| `blocking ionic-overrides.scss:10` | add `ion-modal ion-footer { … }` in `modals.scss` | subtask **03** step 1 |
| `blocking utilities.scss:109` | add `ion-content.default-bottom-space { --padding-bottom: 6rem; }` + the page sweep | subtask **04** steps 1-2 |
| `low ionic-overrides.scss:9` | rationale comments | subtask **02** steps 2-3 |

Subtask 01's own materialized spec ends:

> **Out of scope for this subtask.** Nothing consumes the token yet — that is subtasks 02 … 04. **Do not add an
> overlay, modal or page rule here.**

So the gate blocked the subtask for not doing what the subtask forbade. Meanwhile the `fixing` node's prompt for the
same subtask reads *"You are fixing subtask 1 of 5; keep your change scoped to that subtask's spec: …"* — two nodes
in one run, holding contradictory instructions, purely because of the `agent.py` / `evaluator.py` asymmetry.

#### It is bounded, and that bound is worth stating precisely

`fixing` refused, changed nothing, and diagnosed the defect unaided — *"The reviewer graded subtask 01's diff against
the whole task's acceptance criteria rather than the subtask's … A human should re-route these four findings to
subtasks 02-04."* Review round 2 then received `prior_fix: …/fixing/run-000006/fixing.out.md`, read that account, and
**accepted with zero findings**. [`review.md:5`](../../src/wastech_orchestrator/packaged/flows/implementation/review.md)'s
`prior_fix` rule is what recovered it.

So this is **not** an unsatisfiable loop — an earlier draft of this entry called it a blocker and that was wrong. It
costs **one wasted review+fixing cycle per subtask**: on subtask 1 that measured review 204.5s + fixing 303.6s
($2.41) + re-check + review 2, roughly **11-12 minutes** of pure waste, repeated for every subtask of every
operator-authored decomposition — about an hour on this five-subtask task, for a defect whose fix is four lines.

The residual risk is the part that does not show up as wasted time: **recovery depends on the fixing agent being good
enough to refuse.** A model that simply complied would have implemented subtasks 02-04 inside subtask 01, and nothing
in the machinery would have caught it — the only node holding the boundary is the one being overruled.

#### The mechanism, confirmed quantitatively

The number of false blocking findings tracks the volume of later-subtask work still absent from the tree:

| Review | False blockers | Demanded |
| --- | ---: | --- |
| subtask 1, round 1 | 3 | subtasks 02, 03, 04 |
| subtask 2, round 1 | 2 | 03, 04 (02 had landed) |
| subtask 2, round 3 | 3 | 03, 04 (04 split into two findings) |
| subtask 3, round 1 | 2 | 04 only — the `modals.scss` blocker vanished the moment subtask 03 landed |

That is the defect stated exactly: the reviewer holds the **root** task's whole-task acceptance criteria and
charges every unfinished part of them against whichever subtask is under review. It is not occasional
misjudgement; it is a systematic accounting error, and it decays only as the remaining subtasks land.

**F1 and F2 partially cancel, by luck.** After subtask 04 lands the only outstanding subtask is `05-docs`, and
`review.md:18` tells the reviewer not to flag missing documentation — so F2 will suppress F1's last false
blocker. Neither defect is excused by that. The practical consequence is that **they must be fixed together**:
repair F2 alone (so the reviewer does judge docs-only deliverables) and subtask 4 immediately acquires a fresh
false blocker about the unwritten documentation.

Convergence, finally, is **high-variance rather than bounded**: subtask 1 needed 2 review rounds, subtask 2
needed 5. The reviewer does talk itself out of the false blockers, but nothing bounds how long that takes
except `budgets.review_fix`.

### F2 — `review.md` tells the reviewer to ignore documentation on tasks whose deliverable *is* documentation

**Severity: minor.** **Lever: role prompt — `<target>/.worc/flows/implementation/review.md`** (and the packaged
[`packaged/flows/implementation/review.md`](../../src/wastech_orchestrator/packaged/flows/implementation/review.md)
it was tuned from).

`review.md:18`:

> The diff may be cumulative — on a shared branch it can include files committed by earlier tasks. Judge only what
> this task's plan changed; do not flag prior-task code as scope drift. **Documentation updates run in a later step
> of this flow, so do not flag missing doc changes.**

The last clause is correct for a code task — the `documentation` node runs after `review` and would make the finding
moot. It is wrong for a **docs-only** deliverable, and this trial queues two: task `002a` (documentation only, "Do
not touch code") and subtask `05-docs` of `001`. For those the docs are the entire product, and the sentence invites
the reviewer to stand down on the only thing worth reviewing.

The fix is a condition, not a deletion: the instruction should hold only where the diff contains non-documentation
changes. Runtime confirmation of whether the reviewer actually under-reviews `002a` is pending and will be recorded
in the run log section below.

### F3 — `worc-deco-task` never says the root task reaches the edit node only as a footer path

**Severity: minor.** **Lever: skill —
[`packaged/guide/skills/worc-deco-task/SKILL.md`](../../src/wastech_orchestrator/packaged/guide/skills/worc-deco-task/SKILL.md)**
(mirrored into the target's `.claude/skills/worc-deco-task/`; the two are byte-identical).

The skill frames the split as root-context plus steps:

> 1. **Separate root from steps.** The **root** holds the shared context (what the whole change is and why).

and is precise about the subtask side:

> The body is materialized **verbatim** into an immutable `NN-<slug>.md` spec that the edit steps (`implementation`,
> `fixing`) read as `{subtask_spec_path}`, so write it however the step needs.

What it never states is the asymmetry. The prompt renderer substitutes **paths only, never bodies**
([`core/prompts.py:1-38`](../../src/wastech_orchestrator/core/prompts.py)), and none of the installed
`implementation` role prompts reference `{task_path}` at all. The root task therefore reaches the executing node as
one line of `build_context_footer`:

```
Context files (read them as needed; do not assume their contents):
- task: <path>
```

while the subtask spec is named in the prompt body and called an "immutable spec". "Shared context every subtask
inherits" is the author's reasonable reading of the skill; "one optional footer path" is the mechanism. An author
who believes the first will put a load-bearing constraint in the root and not restate it.

**This batch reproduces the gap exactly.** The root task `001` bans a specific utility class:

> do not ship a `.safe-padding-bottom` utility class

Subtask `04-page-bottom-spacing` is the subtask that edits `src/theme/utilities.scss` — the one file where that class
would be written — and its body does not restate the ban. The guard is absent precisely where it is needed, and the
reviewer that might have caught it is the one described in **F1**.

The skill should say plainly: a constraint that must bind one subtask belongs in that subtask's body, because the
root arrives as an optional footer path and (today) is invisible to `review` under decomposition.

**Confirmed at runtime.** The rendered `planning` prompt (7,959 chars,
`stages/planning/run-000002/rendered-prompt.md`) ends with exactly those two footer lines and contains no other
reference to the task; the `{?memory_path}` block dropped as expected (memory is disabled). The exchange copy at
`.worc-io/001-edge-to-edge-bottom-insets/task.md` **is** the full 98-line root task and does carry the
`.safe-padding-bottom` ban at line 87 — so the constraint is *reachable*, merely weakly signposted. The finding is
about signposting, not about the constraint being dropped.

### F4 — `worc-config` enumerates two of the three install-written security keys

**Severity: minor.** **Lever: skill —
[`packaged/guide/skills/worc-config/SKILL.md`](../../src/wastech_orchestrator/packaged/guide/skills/worc-config/SKILL.md).**

`SKILL.md:34-37`:

> 4. Keep these unless the operator overrides them deliberately. **Two of them** are what `install` writes rather
>    than what is safest — say which, so the operator is choosing rather than inheriting.
>    - …
>    - `strict_isolation`: `install` writes `false`, which **is** the advanced mode…
>    - `allow_git_evidence`: `install` writes `true`; it is inert beside `strict_isolation: false`…

`disable_read_isolation` is missing from the list, and it is the third key `install` writes into the `security`
block — the installed config carries all three. It is default-unsafe in exactly the sense the step is about:
[`config/schema.py`](../../src/wastech_orchestrator/config/schema.py) defaults it to `True`, and `configuration.md`
calls that

> a deliberate deployment-posture choice that departs from the project's own default-safe rule for isolation.

It is redundant *today* (`strict_isolation: false` forces read-isolation off via `SecurityConfig.read_isolation_off`,
so the explicit `true` changes nothing) — but that is the case for `allow_git_evidence` too, which the skill does
cover, and for the same reason: it becomes load-bearing the moment `strict_isolation` goes back to `true`. An
operator who hardens the master switch on this skill's advice still silently keeps read-isolation off.

### F5 — task `002b` does not name the path of one of the two specs it requires

**Severity: nit.** **Lever: task file — `<target>/tasks/pending/002b-back-button-reference-guards.md`.**

Step 3 names one spec path explicitly and leaves the other implicit:

> - Create `src/app/pages/demos/forms/reactive-forms-demo.page.spec.ts`: …
> - **Create a spec for the modal guard**: registers at `backButtonPriority.overlayGuard`, …

and the acceptance criterion is only `Both new specs exist`. The colocated `*.spec.ts` convention makes
`database-risk-confirmation.component.spec.ts` the obvious inference, and the sibling bullet sets the pattern — so
this is a nit, not a defect. Recorded because a greppable acceptance criterion is what makes the rest of this batch
auditable, and this one is not greppable.

### F6 — git control-state drift fires a false positive from the operator's own IDE

**Severity: minor** (noise on a security signal, not a breach). **Lever: orchestrator source —
[`git_manager.py`](../../src/wastech_orchestrator/git_manager.py), `_capture_local_config` / `_diff_config`.**

At the close of the `planning` node the run logged:

```
level=warning stage=planning drift="config: repo config key changed:
branch.feat/001-edge-to-edge-bottom-insets.vscode-merge-base"
msg="git control state changed during this node — continuing per policy; if you did not do this yourself,
stop the run and discard the clone before it is committed or pushed"
```

The key is VS Code's. `git config --local --get-regexp vscode` in the target repo returns one per branch —
`branch.main.vscode-merge-base`, `branch.fix/ios-no-firebase-local-mode.vscode-merge-base`, and the new
`branch.feat/001-edge-to-edge-bottom-insets.vscode-merge-base` — so the IDE writes one as it notices each
branch. The node that supposedly drifted was `read-only` with `Write`/`Edit`/`MultiEdit`/`NotebookEdit` denied
and `Bash(git commit:*)` / `Bash(git push:*)` denied in its own argv; it had no way to write a config key.

[`git_manager.py:1710-1727`](../../src/wastech_orchestrator/git_manager.py) fingerprints **every**
`--local`/`--worktree` key with no exclusions, and `_diff_config`
([`git_manager.py:1929-1936`](../../src/wastech_orchestrator/git_manager.py)) reports any delta. The capture's
docstring states its scope is

> exactly the agent-writable config surface

which holds only when the orchestrator owns the checkout. It does not here: `repo.local_path` is the
operator's real working checkout — what `install` writes — and the `planning` request's
`working_directory` confirms there is no clone at all. That surface is therefore also operator- and
IDE-writable.

So the warning fires on **every** task, at whichever node runs while the IDE first sees the new branch, and
tells the operator to abort and discard. The cost is not the noise itself but the desensitization: the
`full-tool-access` backlog entry argues that on the shipped default this warn line is the *only* trace of a
real isolation failure, which "makes that trace part of the mitigation, not a nicety". A signal that cries
wolf once per task is not that. The run continued correctly per policy and nothing was compromised.

Worth noting the sibling asymmetry: `_untrusted_config_programs`
([`git_manager.py:1997-2015`](../../src/wastech_orchestrator/git_manager.py)) — the gate that actually
*refuses* — is filtered to program-launching keys (`_FILTER_DRIVER_KEY_RE`, `_PROGRAM_CONFIG_KEYS`). Only the
reporting path is unfiltered.

### F7 — the agent cannot run `npm run build` inside its sandbox; the Check Runner can

**Severity: major.** **Lever: orchestrator source —
[`providers/claude.py`](../../src/wastech_orchestrator/providers/claude.py), sandbox-policy generation.**

`fixing` reported the project's own build command aborting under it:

```
npm run build → exit 134
fatal error: all goroutines are asleep - deadlock!
goroutine 1 [chan receive]:
  github.com/evanw/esbuild/.../ThreadSafeWaitGroup.Wait
  main.runService ... esbuild/cmd/esbuild/service.go:160
```

It bisected carefully — restored the three changed files from `HEAD`, reproduced the abort, restored its change — and
concluded *"It is a pre-existing host-toolchain failure"*.

**That conclusion is false, and the run's own ledger disproves it.** The Check Runner executed `npm run build`
successfully **twice** in the same task:

```
21:38:09  check=build passed=true exit_code=0 duration_seconds=11.436
21:47:29  check=build passed=true exit_code=0 duration_seconds=11.533
```

with 200+ files freshly written into `www/`. The variable is not the tree — it is the **sandbox**. The node's
generated `claude-sandbox-settings.json` is

```json
{"sandbox": {"enabled": true, "failIfUnavailable": true,
             "allowUnsandboxedCommands": false, "excludedCommands": [],
             "autoAllowBashIfSandboxed": true, …}}
```

so every agent `Bash` runs under Claude's macOS seatbelt sandbox, where esbuild's Go service deadlocks against its
Node plugin host. `grep -rn "sandbox|seatbelt|bwrap" src/wastech_orchestrator/checks/` returns nothing — the Check
Runner runs the command directly. Same command, two environments, two outcomes.

Why it matters beyond the noise:

1. `implementation.md`'s **Verify** section *mandates* the agent run `npm run lint` **and** `npm run build` before
   finishing. Half of the required self-check is impossible for every `implementation`/`fixing` node in this repo.
2. It burns a fix round diagnosing a phantom.
3. It writes a false conclusion — "pre-existing host-toolchain failure" — into the durable run record, where an
   operator would act on it.
4. The latent risk is the interesting one: only `fixing.md`'s "do not work around a missing or incompatible host
   toolchain" rule stopped the agent from "repairing" a build that was never broken. Defense-in-depth held, but by
   rule rather than by a correct environment.

This is **not** the deliberate `test:ci` / Chrome gap: `npm run build` is *in* the check set and is what the role
prompts tell the agent to run. There is no config-level lever today — `excludedCommands` is emitted empty and nothing
can populate it. That makes this a direct, evidenced argument for
[`full-tool-access`](full-tool-access/README.md) step 4 (`unsandboxed_commands`), which is still only proposed.

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

Three things make the misreading easy: the preceding bullet about the three-characters-different `.worc/`
says "do not read it"; "read only the paths you are given" reads as a restriction rather than a grant; and the
trailing sentence says the sandbox may not block "the paths above" before re-forbidding "any
orchestrator-private file", which a reader can take to include `.worc-io/`.

It is **intermittent**, which is worse than deterministic: reviews `run-000005`, `run-000008` and `run-000011`
in the *same task* read the same `.worc-io/` paths without complaint. Same prompt, same provider, different
outcome. The fix is to make the bullet an explicit grant and scope the trailing sentence to `.worc/`.

### F10 — the evaluator contract cannot express "I could not review"

**Severity: major.** **Lever: orchestrator source —
[`core/flow/nodes/evaluator.py`](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)**, with a
supporting line in `review.md`.

The F9 refusal above was accepted as an ordinary verdict — `succeeded`, `exit 0`, a structurally valid
`findings` array — and routed to `fixing` as rework. Nothing in the contract distinguishes *"the diff is
defective"* from *"I was unable to look at the diff"*. `review.md:9` guards the adjacent failure ("**No
findings means the diff is clean** — return an empty `findings` array, not prose. A prose 'looks good'
hard-stops the task") but there is no guard for a structurally valid refusal, and `path` is explicitly
nullable.

So an infrastructure complaint was handed to the one node that cannot act on it. `fixing` spent **474.4s and
$2.87** correctly concluding there was nothing to fix:

> The reviewer never examined the change and named no file, line, or defect — it reported a contradiction in
> its own instructions. There is nothing in the diff for it to fix… It needs a human to hand the review stage
> a context path it is permitted to read.

With a less careful fixer the loop could consume `budgets.review_fix` and park the task on a defect that was
never in the code. A blocking finding carrying no `path` and citing no source line is not rework; it is an
infrastructure failure of the node, and the graph should treat it as one.

### F8 — task `001` paraphrases a repo rule more narrowly than the rule

**Severity: minor.** **Lever: task file — `<target>/tasks/pending/001-edge-to-edge-bottom-insets.md`.**

The task states the constraint as (lines 91-93):

> **Comments state their reason in their own words** — no links and no pointers into `docs/` or `.rules/`
> from shipped TypeScript or SCSS.

The rule it is paraphrasing, `.rules/coding-style.md:88-92`, is broader:

> **Comments must be self-contained.** A comment must be understandable on its own… It must not depend on any
> other file, document, or system continuing to exist.
> **Links and documentation references of any kind are forbidden anywhere in the codebase**… This covers URLs,
> tickets, issues, PRs, and paths or section anchors into `docs/**`, `.rules/**`, `AGENTS.md`, **or any other
> file in the repo**.

The agent complied with the narrower paraphrase and shipped a comment referencing `runtime.scss` and
`see feedback.scss`. **The reviewer caught it** — a `medium` finding citing `.rules/coding-style.md` — because
`review.md:1` tells the evaluator to read the rule for the area the diff touches rather than trust the task.
That is the flow design working as intended, and it is worth recording as a positive alongside the defect.

Two honest qualifications. The same finding's second clause (that the comment misdescribes page breathing
space) conflates design decisions **D-5** and **D-6** and is weak — the comment matched D-5. And the repo is
not consistent about its own rule: `src/theme/dark-mode.scss:26`, untouched by this run, carries a shipped
comment referencing `variables.scss`. So this is a drafting slip in a codebase that is itself loose here, not
carelessness — hence `minor`.

The transferable lesson for task authoring: **cite a repo rule by name and let the agent read it** rather than
restating it, because a restatement can only lose fidelity.

## Not defects — verified and cleared

Recorded so a later reader does not re-open them.

- **The audit trail itself is complete, well-structured and clean.** 21 `prompt-audit` records, one per node
  run, named `<run>-<node>[-sub<NN>].json`, each carrying the full rendered prompt plus route, provider,
  per-attempt status and timings; `state.db` carries 60 artifacts, 14 check runs, 10 evaluations, 23 node runs,
  20 provider attempts and 5 subtasks; `publish_operations` records each subtask commit with a fingerprint, the
  resulting SHA and `pushed_sha: None` (nothing published yet — correct). A scan of every prompt-audit record
  for `sk-`, `ghp_`, `bot<digits>:`, `PRIVATE KEY` and `TELEGRAM_BOT_TOKEN=` returned **no matches**; redaction
  holds. F13 above is a fidelity gap in one field of an otherwise strong surface.

- **`allow_git_evidence: true` is inert here and that is documented, not hidden.** No node in the `implementation`
  flow declares `git_evidence` (only `deep_research.yaml:81,101,121` does), and under advanced mode the grant has no
  capability left to add. `worc preflight` prints `git-evidence: ON (security.allow_git_evidence=true) — inert under
  strict_isolation=false` and the run log repeats it. The product says the true thing out loud.
- **Dropping `npm run test:ci` from the check set is the correct call, not a dodge.** `skip_if_unavailable` is a
  **per-set** flag keyed on the *toolchain binary* being absent
  ([`config/schema.py:380-394`](../../src/wastech_orchestrator/config/schema.py)); `npm` is present and Chrome is
  not, so the set would fail rather than skip. Declaring the command with `skip_if_unavailable: true` would have
  been worse than omitting it with the comment the config actually carries, which also gives the restore recipe.
- **`implementation.yaml` is honest about its own voided key.** Lines 158-161 state that `network_access: false` on
  the `documentation` node is neither a hard guarantee nor defense-in-depth under advanced mode — "every node is
  online there whatever this key says" — and advise pinning `provider: codex`. The operator did not take that
  advice, so the doc node is online; that is a config-level choice the flow warned about, not a flow defect.
- **`hitl:` on an agent node is a permission, not a gate.** `HitlPolicy`
  ([`core/flow/schema.py:45-48`](../../src/wastech_orchestrator/core/flow/schema.py)) lets the agent *optionally*
  emit a `human_input` signal in its typed output; it is not a forced round-trip. Only the bare `hitl` **node kind**
  pauses unconditionally. So `planning`'s `allow_approval: true` does not mean five Telegram approvals across five
  tasks.
- **The operator decomposition path does not consult `agents.decomposition.enabled`.** That key defaults to `False`
  ([`config/loader.py:515`](../../src/wastech_orchestrator/config/loader.py)) and the config omits it, but
  `_validate_operator_subtasks` gates on `task.subtasks` alone
  ([`core/orchestrator.py:787`](../../src/wastech_orchestrator/core/orchestrator.py)) and only requires the flow to
  carry a `decomposition:` block. `001` decomposes correctly; the run confirmed `subtask=1/5`.
- **`--allowedTools` in the argv is not a contradiction of advanced mode.** The observed `planning` argv carries
  `--allowedTools Read,Glob,Grep,Bash,PowerShell,TodoWrite,BashOutput,KillShell,WebFetch,WebSearch` while
  `SecurityConfig.strict_isolation` says "no tool allowlist reaches the agent CLI". The adapter is precise where
  the schema docstring is loose: `--tools` is the *hard existence gate* and is correctly **not** emitted
  ([`providers/claude.py:1004-1015`](../../src/wastech_orchestrator/providers/claude.py)), while `--allowedTools`
  is only the auto-approve baseline, "and the boundary has moved to `--disallowedTools`"
  ([`claude.py:318`](../../src/wastech_orchestrator/providers/claude.py)). Filed here because a shallower audit
  reports this as a breach. The only residue is a **nit**: the `config/schema.py` wording invites exactly that
  misreading.

## Cosmetic drift (no lever worth spending)

- The target's installed `.worc/config.example.yaml:93` says `model: "gpt-5.4"` where the packaged copy says
  `"gpt-5.5"` — an artifact of having been installed at an older version. Everything else in the file is identical.
- `.worc/config.yaml:4-7` describes the `.worc/` home as gitignored "(this config included)", but `.gitignore:76-78`
  deliberately re-includes `!.worc/config.yaml` with its own rationale ("Track the orchestrator config so its
  changes are reviewable in history"). The generated header text is stale against the generated ignore rules.

### F11 — the supervisor's observe turn is blind to the node whose behavior it is explaining

**Severity: major.** **Lever: orchestrator source —
[`core/supervisor.py`](../../src/wastech_orchestrator/core/supervisor.py), `observe()` / `_step_prompt`.**

After review returned rework on subtask 2 for the fourth time, the supervisor wrote a genuinely sharp note. It
named the loop shape cycle by cycle, and concluded:

> That trajectory — real work, then cosmetic work, then nothing — is the signature of a step that has
> exhausted its budget or is failing silently, not one converging on a fix. **It will not converge on its own.
> Rework cycles spent from here are wasted unless someone changes the inputs.**

and observed, correctly and usefully, that *"lint and build have presumably stayed green across all four
cycles precisely because nothing changed; green here carries no information."*

Then it got the cause exactly backwards:

> Between cycle three and cycle four, **the implementer produced nothing at all** … the signature of a step
> that … is **failing silently**.
> **Recommended human action:** check whether the implementation node is **erroring or timing out** before it
> writes.

`fixing` had not failed. It ran 474.4s, exited 0, cost $2.87, and wrote a detailed report explaining exactly
why it changed nothing. Worse, the supervisor wrote *"I verified all three findings; all are accurate"* —
endorsing the **false** blockers, never noticing that subtask 02's spec forbids touching `modals.scss` and
`utilities.scss`. It reads "Fourth consecutive cycle untouched" as failure when it is compliance. An operator
following its recommendation would have gone hunting a timeout that does not exist.

The cause is a wiring gap, and it is one line —
[`core/supervisor.py:506`](../../src/wastech_orchestrator/core/supervisor.py):

```python
prompt = self._step_prompt(task_id, node_id, outcome_kind, final_message, findings)
```

The observe turn receives only the **observed** node's own `final_message` plus its `findings`, and its `_run`
call passes no `supervisor_packet_path` — unlike the finalize turn
([`supervisor.py:670,688`](../../src/wastech_orchestrator/core/supervisor.py)), which *is* grounded in the
packet. So when observing an **evaluator** step, the supervisor sees the reviewer's message and the reviewer's
findings and is structurally blind to what `fixing` said in the round before — precisely the evidence needed
to judge whether a rework loop is productive.

Size is not the obstacle: `_STEP_MESSAGE_MAX` is 500 and the fixing report's whole rationale sits in its first
500 characters, so the packet's `steps[].message` channel
([`supervisor_packet.py:318-319`](../../src/wastech_orchestrator/core/supervisor_packet.py)) would have
carried it — the observe turn simply is not given the packet. The method's own docstring already records a
sibling starvation: *"without them the observation is a bare outcome label with nothing to react to, which is
why the observer made no tool calls on any evaluator step of the run this came from."*

### F12 — `worc status` names the wrong node inside a decompose region

**Severity: minor** (operator surface only). **Lever: orchestrator source — the `current_node` bookkeeping the
status renderer reads.**

Twice, about a minute apart, immediately after review accepted subtask 2:

```
node=documentation   subtask=3/5   fix_iterations=4
```

`documentation` is not in `decomposition.sub_flow` (`implementation.yaml:207`) and the flow states it "runs
once per task (after the last subtask)" (`implementation.yaml:143-145`). It did not run. The node that
actually started, at `22:30:52`, was `implementation` for subtask 3. The status surface appears to name
`review`'s successor in the **main** graph without accounting for the decompose region looping back.

Functionally nothing is wrong — `subtasks/index.json` correctly showed orders 1 and 2 `committed` and 3-5
`pending`, and the graph routed correctly. But an operator watching `worc status` would believe a five-subtask
task had reached its documentation stage while it was in fact starting subtask 3. The lever is named
tentatively: the symptom is precisely located, the exact write site is not isolated.

### F13 — `prompt-audit` records the per-node override, not the effective model/reasoning

**Severity: minor.** **Lever: orchestrator source — the prompt-audit writer.**

`prompt_audit: true` exists so a run can be reconstructed. Across all 21 records of this task the `reasoning`
field is `None` for every node but one supervisor turn, and `model` is `None` for the supervisor:

```
fixing / implementation / planning   claude   model=claude-opus-5   reasoning=None
review                               codex    model=gpt-5.5         reasoning=None
supervisor                           claude   model=None            reasoning=None | low
```

None of those nodes ran at a provider default. The same runs' `request.json` and argv show `planning` at
`reasoning: "xhigh"` / `--effort xhigh`, and `review` at `-c model_reasoning_effort="xhigh"` inherited from
`agents.providers.codex.reasoning`. So the **effective** value lives in `request.json` while the artifact
actually named "prompt-audit" carries only the flow-node override, and the two disagree.

An operator auditing "did the reviewer really run at `xhigh`?" reads `None` and cannot answer from the audit
record. Recording the resolved value — or both, as `configured` and `effective` — closes it.

### F14 — the subtask handoff's factual floor is built from `depends_on`, not from what actually landed

**Severity: minor.** **Lever: orchestrator source —
[`core/orchestrator.py`](../../src/wastech_orchestrator/core/orchestrator.py), the handoff floor assembly.**

```python
if not unit.depends_on:
    return None                    # orchestrator.py:3178
...
for dep in unit.depends_on:        # orchestrator.py:3183
```

In an operator-authored decomposition every subtask commits to the **same branch, sequentially**, so every
earlier subtask is a predecessor in fact. The floor names only the *declared* ones. Subtask 04 declares
`depends_on: ["inset-source-and-token"]`, so its brief's factual section named subtask 01 alone while 02
(`38b97c3`) and 03 (`61648c6`) were committed and were the closer precedents. The supervisor's interpretive
half caught the gap and said so in the artifact:

> **Three predecessors are committed on this branch, not one.** The handoff names only subtask 01;
> `38b97c3` (subtask 02) and `61648c6` (subtask 03) also landed and are closer precedents for your work.

Worse edge case: a subtask with **no** `depends_on` gets `None` — no handoff at all — even with three
subtasks already committed to its branch. As with F1, the compensation came from model quality, not mechanism.

This also adds a clause to **F3**: `worc-deco-task` describes `depends_on` purely as ordering ("a list of
**slugs of EARLIER subtasks only**", "dependencies are linear and backward-only") and never says it *also*
decides what the next subtask is told. An author who declares only the true logical dependency — exactly what
the skill's wording invites — silently narrows the brief.

### F15 — a correct finding with an invented authority

**Severity: minor.** **Lever: role prompt — `review.md`.**

Reviewing subtask 04, the evaluator filed one blocking finding:

> **Phase 04 explicitly includes the user-login sandbox** in the page-bottom-space sweep, but its
> `<ion-content class="ion-padding">` was left without `default-bottom-space`.

The finding is **right**, and it is the first real delivery defect the review layer caught in this run. The
citation is **fabricated**: `plan/04-page-bottom-spacing.md` mentions the user-login sandbox zero times and
says the opposite — "Leave full-bleed screens alone — onboarding, auth". The claim is true of the *plan*
(`plan.md:184`), not of Phase 04.

Findings feed a fixing agent, and `fixing.md`'s guard ("treat the `fix:` hint as a lead, not ground truth …
re-open the source and confirm the corrected claim there") is precisely what absorbed this — the fixer cited
`plan.md` instead and moved on. But a misattributed citation sends the fixer to the wrong document first, and
an operator reading the finding would believe Phase 04 says something it does not. A finding that cites a
document should quote it and name which artifact it is quoting.

### F16 — subtask 04 restates a property rule as a path list, and the path list over-excludes

**Severity: minor.** **Lever: task file — `<target>/tasks/pending/subtasks/04-page-bottom-spacing.md`.**

Step 3 reads:

> **Leave full-bleed screens alone** — `src/app/pages/onboarding/`, `src/app/pages/auth/`, and anything that
> deliberately draws to the edge or fills the viewport without scrolling.

The headline is a **property**; the enumeration is two **paths**. `auth/components/user-login-sandbox/` sits
under one of those paths and is not full-bleed — it has an `<ion-header>` toolbar and an
`<ion-content class="ion-padding">` of cards. The literal reading therefore drops a page the rule intends to
include, and the implementation agent took the literal reading.

`planning` did not: `plan.md:184` lists the page for the sweep with a reason — *"normal header + scrolling
content ending in two buttons — a sandbox screen, not a full-bleed auth screen"* — and `plan.md:190` names the
genuinely full-bleed auth screens as `auth/components/{login,signup,forgot-password}`. `fixing` then read the
exclusion by property too and added the class.

Same class as **F8** — a paraphrase that loses fidelity — but this one cost a delivery gap rather than a style
violation.

### F17 — a task cannot contribute to its own commit message (second instance of the same gap)

**Severity: minor.** **Lever: orchestrator source — subtask commit-message assembly; or task-authoring guidance
in `worc-task` / `worc-deco-task`.**

Subtask 04's acceptance criterion: *"The pages that got the class and the pages deliberately left alone are
both named, with reasons, in the summary **and the commit message**."* The run summary carries both lists. The
commit message does not — `git log -1 --format=%B 9b69db0` is one line, and all five subtask commits are
title-only, because the message is generated from the subtask title with no channel for an agent to add to it.

This is the same structural gap as task `001`'s "put the `grep` output in the PR description" criterion: a task
can ask for content in a publication surface that no node is able to write. **Two instances in one task.**
Either give an agent's summary a way in, or have the task-authoring skills warn that these surfaces are not
addressable.

### F18 — `worc merge-task` cannot control the squash commit message

**Severity: minor.** **Lever: orchestrator source —
[`git_manager.py`](../../src/wastech_orchestrator/git_manager.py), `merge_pull_request`.**

```python
args = ["pr", "merge", pr_url, f"--{strategy.value}"]   # git_manager.py:3021
```

No `--subject`, no `--body`. With `git.auto_merge_strategy: squash` the squash commit's content is therefore
whatever the **repository** setting dictates. On this repo:

```
squash_merge_commit_title:   "COMMIT_OR_PR_TITLE"
squash_merge_commit_message: "COMMIT_MESSAGES"
```

so GitHub concatenates every commit message into the squash body. An operator squash-merging through
`worc merge-task` inherits that silently and learns the outcome only afterwards — and on a repo whose
`.rules/git-workflow.md` forbids agent-attribution trailers "in merge commits, squash commits, and PR titles
and bodies", that is exactly how a forbidden trailer reaches `main`.

`merge-task --dry-run` is otherwise good — it printed status, branch, base, PR, PR state and
"-> update branch w/ base, then merge via 'squash'". It did not print the message policy, which was the one
thing that mattered. Reporting that in the dry-run would be the cheapest fix.

### F19 — `worc top --help` documents an argv form that argparse rejects
Severity: nit. Lever: CLI help text (`cli.py`, `top` parser `--log-file` help).
`worc top --log-file PATH` is described as "the daemon log file to tail (the path passed to
'watch --log-file')". `worc watch` has no `--log-file` option: its parser carries only
`--poll-seconds` and `--queue`. `--log-file` is a **parent-parser** flag, so the working form is
`worc --log-file PATH watch`.
Verified: `worc watch --log-file /tmp/x.log --poll-seconds 0` prints the top-level usage error;
`worc --log-file /tmp/x.log watch --help` parses.
Notable because the codebase already knows this: `cli_shell.start_watch`'s docstring says the parent
flags go "**before** the `watch` subcommand (they are parent-parser options — appending them after
`watch` is why the old auto-spawn died on an argparse error)". The lesson was learned in the spawn
path and not carried into the help text an operator reads.
`worc shell --log-file` is correct — it is a real option on `shell`, which spawns the daemon itself.

### F20 — every `worc run` task reads as "parked (no daemon)", and `rerun --continue` will start a second engine on it
Severity: **major**. Lever: orchestrator source — `cli.py` `_display_status` / `cmd_rerun`'s guard,
`core/orchestrator.py` `RERUN_ELIGIBLE_STATUSES` + `plan_rerun`.

Observed live, 00:38, while `002a` was executing its `planning` node (heartbeats every 30s in the
run log):

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
1. `cmd_run` (cli.py:1846+) writes **no** PID file and installs no stop wiring — only `cmd_watch`'s
   daemon branch does (cli.py:1762-1810).
2. `_display_status` (cli.py:1301-1312) renders any RUNNING row as `parked (no daemon)` whenever no
   *watch-daemon* PID file is alive. `worc run` therefore reports "parked" for its entire duration,
   in `status`, `list` and `top` alike — the docstring's claim is that such a row is "parked at its
   checkpoint, awaiting resume — **not executing**", which is false for the whole `run` path.
3. `cmd_rerun`'s only concurrency guard is that same watch-daemon PID (cli.py:2022-2028) — it passes.
4. `RERUN_ELIGIBLE_STATUSES` = {FAILED, MANUAL_ACTION_REQUIRED, **RUNNING**}
   (core/orchestrator.py:284-286), and `plan_rerun`'s own comment (orchestrator.py:1147-1150) states
   the assumption outright: "a `running` row reaching here is daemon-less (the 'parked (no daemon)'
   state)". The active-slot check at :1156 excludes the task's own id, so it does not self-block.

So `worc rerun <id> --continue` against a task that is live under `worc run` passes **every** refusal
and drives a second engine over the same branch in the same clone. The mislabel is not cosmetic: it
is the exact prompt that sends an operator to that command.

`run` is a documented first-class entry point — it is how this whole trial and all of task 001 were
executed — so the state is routine, not exotic.

NOT tested live, deliberately: proving step 4 needs a real `rerun --continue` against the running
002a, which risks corrupting the trial's own branch. Proven statically instead; a safe live proof
would need a throwaway repo.

Cheapest fix: have `cmd_run` write the same PID file (or a run-scoped liveness marker) so the
liveness probe covers both executors; failing that, stop equating "RUNNING + no daemon" with stale.


### F21 — `review` is never given the check results on a pass, so it re-runs the gate inside the sandbox and blocks on F7's phantom
Severity: **major** (it composes F7 + F10 into a review->fixing loop that can park a task).
Lever: orchestrator source — `core/flow/nodes/checks.py` `_publish_first_failure_log`; with a
supporting line owed in `review.md`.

The command-profile checks node sets `{checks_path}` **only on failure**
(checks.py:121-135 — "Publish the redacted first-failure log and set `{checks_path}` before the fail
edge"). On a pass it stays `None`, so nothing about the checks reaches the next node.

Confirmed empirically. `stages/review/run-000045/1-codex/request.json` `context_paths`:
```
task_path, plan_path, diff_path, review_artifacts_path      <- no checks_path
```
while `state.db` had already recorded `npm run lint` and `npm run build` green for that very tree.

The codebase already argues the other way for the *other* profile. `_publish_citation_report`
(checks.py:171-186) sets `{checks_path}` "on **BOTH** outcomes", and its docstring gives the reason:
> "Leaving it to the command-profile failure path alone would deliver the report to nobody exactly
> when the check passed."
The command profile — the one `implementation.yaml` uses — was never given the same treatment.

Why it bites here rather than being merely tidy: task 002a's acceptance criteria end with
"`npm run lint` and `npm run build` pass". The reviewer is asked to judge that criterion, is handed
no evidence of it, and under advanced mode has a Bash tool. So it verifies the only way left to it —
by running the build itself, inside the agent sandbox, where **F7** kills it. Review round 2:
```
severity: blocking | path: null
what: "`npm run build` does not currently satisfy the task gate: it exits 139 after printing
       `Building...`. This appears environment-bound rather than caused by the markdown-only diff,
       but the acceptance criterion is still blocked on this host."
fix:  "Human/operator needs to repair or rerun the build toolchain on a working host; no repo source
       change is indicated by this failure."
```
Note the signal differs by provider — codex/gpt-5.5 gets exit **139** (SIGSEGV), claude/opus gets
exit **134** (SIGABRT) — which is itself evidence the cause is the sandbox, not the tree.

That finding is `blocking`, carries `path: null`, and its own `fix:` states no repo change is
indicated — **F10** exactly, second instance in this task. It routes to `fixing`, which cannot act.
Task 001 never showed this because its reviewer did not run the build; 002a's does, and the result
is a loop over a failure that does not exist, bounded only by `budgets.review_fix: 15`.

`review.md` reinforces it by omission: line 64 already carves out `test:ci` ("Karma cannot execute in
this environment ... Do not raise a finding that the implementer failed to run the test suite") —
the exact carve-out the check gate itself lacks. Nothing tells the reviewer that the Check Runner
owns lint/build, that its verdict is authoritative, or that the reviewer's own sandboxed attempt is
not evidence.

Cheapest fix: publish `{checks_path}` on a pass too (the citation profile's own argument), and add
one line to `review.md` pointing the reviewer at it instead of the shell.

## Reproducibility on chain 002 (task `002a`)

The second task re-ran the same pipeline on a different shape of deliverable — documentation only,
no decomposition. Every defect that could apply did apply, and two of them composed into something
task 001 never showed.

### F6 (IDE-driven git control-state drift) — REPRODUCED, first node of 002a
00:40:59, closing the `planning` node, verbatim shape of the 001 occurrence:
```
level=warning stage=planning drift="config: repo config key changed:
branch.feat/002a-back-button-ladder-docs.vscode-merge-base"
msg="git control state changed during this node — continuing per policy; if you did not do this
yourself, stop the run and discard the clone before it is committed or pushed"
```
Second task, second branch, same key family, same node position. Confirms F6's prediction that this
fires **once per task** at whichever node runs while the IDE first notices the new branch. Run
continued correctly per policy.

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
On task 001 this hit 1 review in 6. On 002a it hit the **first** review of the task. The sample is
still small, but the defect is clearly not rare, and it now has a second, independent occurrence
with the same misreading: the reviewer folds `.worc-io/` into the `.worc/` prohibition.

### F10 — REPRODUCED, and it is the dominant cost of 002a
The refusal was accepted as an ordinary `rework` verdict and routed to `fixing`, which spent
**426.4s** establishing that there was nothing to fix ("Its own `fix:` hint asks for
orchestrator-side action ... not a repo edit"). Same shape as 001's 474.4s round. The evaluator
contract still cannot say "I could not review", so an infrastructure fault is still spent as a fix
round.

### F7 — REPRODUCED, with harder proof and a new escalation
`fixing` again reported `npm run build` dying under it (exit 134, SIGABRT), and again wrote a
confident false conclusion into the durable record:
> "**aborts on this host, environment-bound, not caused by this change** ... It reproduced
> identically across five runs, including with an 8 GB heap and single-threaded settings ...
> **a human needs to look at this host's Node/build toolchain.**"

`state.db` `check_runs` for this task refutes it outright — the Check Runner ran the same command
successfully **twice**, the second time minutes *after* that text was written:
```
npm run lint   exit 0 passed 2026-09-01T22:53:32Z
npm run build  exit 0 passed 2026-09-01T22:53:42Z
npm run lint   exit 0 passed 2026-09-01T23:01:55Z
npm run build  exit 0 passed 2026-09-01T23:02:03Z   <- after fixing's "aborts on this host"
```
The diagnosis is *more* elaborate than 001's (five runs, an 8 GB heap, a macOS crash report,
`tsc --noEmit` as a control) — the agent spends more effort the more carefully it investigates,
because every observation inside the sandbox is consistent.

**New this run — the phantom caused destructive action in the operator's live checkout.** The same
report states:
> "While isolating the build abort **I removed `www/`**, which held output from an earlier build."

`www/` is git-ignored with no tracked files, and the next Check Runner build repopulated it (199
files present now), so nothing was lost. But F7's cost is no longer only a wasted round plus a false
record: chasing the phantom moved an agent to delete a directory in `repo.local_path`, which is the
operator's real working checkout, not a clone. That is the concrete argument that F7 is a sandbox
*correctness* bug, not noise — and it raises F7 above a pure-waste finding.

## What the review-path defects cost, measured

The defects above cost nothing in delivered quality — every tripwire on the shipped code passed. What they
cost is time and money, quietly, under every budget cap and with no warning emitted.

Node timeline for task `001` through subtask 2 (times local; `sec` and `cost` from each node's `result.json`):

| started | node | run | provider | sec | cost | findings |
| --- | --- | --- | --- | ---: | ---: | --- |
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

And that understates it: **every `codex` node reports `cost: None`** — its `normalized_usage` carries token
counts with no price — so the six review turns (1,061s of `gpt-5.5` at `xhigh`, four of them producing false
blockers) are absent from the total. `worc`'s per-task cost is in practice the Claude half of the bill. A
small finding of its own; lever, the codex adapter's usage normalization.

### The cost asymmetry runs the wrong way

Subtask 04's review produced the run's first **real** delivery finding. Fixing it took `fixing` **130.2s and
$1.23** — the cheapest round of the whole task. Refusing the four false ones took:

| Round | Wall-clock | Cost | Verdict |
| --- | ---: | ---: | --- |
| `fixing` 000006 | 303.6s | $2.41 | refused (F1) |
| `fixing` 000015 | 474.4s | $2.87 | refused (F9/F10) |
| `fixing` 000018 | 369.1s | $2.72 | refused (F1) |
| `fixing` 000024 | 217.5s | $1.72 | refused (F1) |
| **total** | **1,364.6s** | **$9.72** | |
| `fixing` 000030 | 130.2s | $1.23 | **fixed a real defect** |

**7.9× the cost and 10.5× the wall-clock to reject false findings versus to fix a true one.** The reason is
structural: a true finding is just work, while a false one obliges the fixer to research it, disprove it, and
write a defensible negative. The system is cheapest when it is right and most expensive when it is wrong,
which is exactly backwards — and it is the strongest single argument for putting F1, F9 and F10 ahead of
everything else.

## Run log

Filled in per task as the trial proceeds. Baseline tripwires on the pre-run tree: `grep -rn "safe-area-inset" src/`
= **12** hits; `--safe-area-bottom` undefined; two real `subscribeWithPriority` call sites
(`searchable-select.component.ts:178` at `inlineOverlay`, `back-button.service.ts:48` at `app`).

| Task | Shape | Status | PR | Notes |
| --- | --- | --- | --- | --- |
| `001-edge-to-edge-bottom-insets` | operator decomposition, 5 subtasks | **done, merged** | [#2](https://github.com/VladimirMakarevich/wastechlab-mobile-template/pull/2) → `main` `5c19180` | 2h23m31s, **$55.86** (Claude only; 11 codex review turns report no cost). `refinement` skipped by `when`. Nodes: planning 1 · implementation 5 · testing 11 (**11/11 passed, zero check failures**) · review 11 · fixing 6 · documentation 1 · publish 1 · supervisor 11. `fix_iterations` 6, **all review-driven**. Review rounds per subtask 2/5/2/2/1. No provider fallback, no retry, no HITL prompt, nothing parked. Every task tripwire passed on the merged result, and `npm run lint` + `npm run build` pass on `main`. |
| `002a-back-button-ladder-docs` | plain, docs only | queued | — | blocked until `001` merges |
| `002b-back-button-reference-guards` | plain, code + specs + i18n | queued | — | blocked until `002a` merges |
| `002c-back-button-exit-confirm` | plain, service + spec + i18n | queued | — | blocked until `002b` merges |
| `002d-back-button-audit` | plain, audit + bookkeeping | queued | — | blocked until `002c` merges |
