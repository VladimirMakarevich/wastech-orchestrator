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

## Not defects — verified and cleared

Recorded so a later reader does not re-open them.

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

## Run log

Filled in per task as the trial proceeds. Baseline tripwires on the pre-run tree: `grep -rn "safe-area-inset" src/`
= **12** hits; `--safe-area-bottom` undefined; two real `subscribeWithPriority` call sites
(`searchable-select.component.ts:178` at `inlineOverlay`, `back-button.service.ts:48` at `app`).

| Task | Shape | Status | PR | Notes |
| --- | --- | --- | --- | --- |
| `001-edge-to-edge-bottom-insets` | operator decomposition, 5 subtasks | running (subtask 2/5) | — | `refinement` skipped by `when`. `planning` claude/claude-opus-5/xhigh, 516s, **$5.10**, `decompose:false` (honored the operator split). Subtask 1: `implementation` ~8m14s → lint 10.2s + build 11.4s pass → `review` codex/gpt-5.5 204.5s **3 false blocking (F1)** → `fixing` 303.6s **$2.41** refused, changed nothing, diagnosed F1 → lint+build pass → `review` round 2 **accepted, 0 findings** via `prior_fix`. No provider fallback, no retry, no HITL prompt. One IDE drift warning (F6). |
| `002a-back-button-ladder-docs` | plain, docs only | queued | — | blocked until `001` merges |
| `002b-back-button-reference-guards` | plain, code + specs + i18n | queued | — | blocked until `002a` merges |
| `002c-back-button-exit-confirm` | plain, service + spec + i18n | queued | — | blocked until `002b` merges |
| `002d-back-button-audit` | plain, audit + bookkeeping | queued | — | blocked until `002c` merges |
