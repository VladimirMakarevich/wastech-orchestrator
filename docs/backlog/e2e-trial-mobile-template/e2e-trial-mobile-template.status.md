# E2E trial on `wastechlab-mobile-template` — status of every finding

The index for the campaign. [The findings document](e2e-trial-mobile-template.md) records what was observed and does not change as findings are repaired; [the fix record](e2e-trial-mobile-template.fixes.md) says what landed and where reproducing a finding proved the finding itself wrong; [the evidence log](e2e-trial-mobile-template.evidence.md) holds the raw Session 1 observations. This file is the one place that answers "what is left" — one row per finding, with what blocks the ones that are open.

**Keep it current in the same change:** a finding that changes state updates its row here and its record in the fix document together, or this becomes the fourth document that disagrees with the other three.

## At a glance

| State | Count | Findings |
| --- | --- | --- |
| Fixed | 16 | F1, F2, F6, F9, F10 (as a warning), F11, F18, F19, F20, F21, F23 (as visibility), F24, F25, F26, F28, F29 |
| Open | 10 | F3, F4, F5, F8, F12, F13, F14, F15, F16, F17 |
| Not ours to fix | 3 | F7 (own ADR), F22 (target's task authoring), F27 (codex CLI) |

Nothing open is rated `major`. Every `major` in the trial is either fixed or is F7, which has its own backlog entry.

## Every finding

`Landed` names the commit on `chore/e2e-trial-mobile-template-findings` (unpushed) and links the record.

| Finding | Severity | State | What it is | Landed / what it needs |
| --- | --- | --- | --- | --- |
| **F1** — reviewer blind to the subtask spec | major | **fixed** | The review evaluator was never told which subtask it was reviewing, so it blocked subtask 01 for not doing work that belonged to subtasks that had not run yet. The runner now publishes the three subtask variables and `review.md` carries a scope block. | `dc69594` — [record](e2e-trial-mobile-template.fixes.md#f1--f2--the-reviewer-under-decomposition) |
| **F2** — "do not flag missing docs" on a docs-only deliverable | minor | **fixed** | One `review.md` clause told the reviewer to ignore missing documentation, which is wrong when the documentation _is_ the deliverable. Made conditional; the predicted runtime harm was falsified by the trial itself. | `dc69594` — [record](e2e-trial-mobile-template.fixes.md#f1--f2--the-reviewer-under-decomposition) |
| **F3** — the root task arrives as a footer path | minor | **open** | `worc-deco-task` never says that the root task reaches an edit node as one optional context line, so an author puts a binding constraint in the root and it goes unrestated in the subtask it must bind. One paragraph in the skill, plus F14's clause. | Skill: `worc-deco-task/SKILL.md` |
| **F4** — two of three install-written security keys | minor | **open** | `worc-config` names `strict_isolation` and `allow_git_evidence` but not `disable_read_isolation`, the third key `install` writes and default-unsafe in the same sense. An operator hardening the master switch on this advice silently keeps read isolation off. | Skill: `worc-config/SKILL.md` |
| **F5** — a spec path left implicit | nit | **open (lesson only)** | Task `002b` names one of the two spec files it requires and leaves the other to inference, with a non-greppable acceptance criterion. The task has already run, so there is no file worth repairing — the value is authoring guidance. | Fold into the authoring skills (see F3/F4) |
| **F6** — drift fires on the operator's IDE | minor | **fixed** | Control-state drift reported `branch.<name>.vscode-merge-base`, which VS Code writes per branch, so the warning fired on nearly every task and told the operator to discard the clone. That one key is excluded from the report and from nothing that refuses. | `3434fc4` — [record](e2e-trial-mobile-template.fixes.md#f6--the-drift-signal-that-cried-wolf-once-per-task) |
| **F7** — the agent cannot run `npm run build`; the Check Runner can | major | **open** | Three nodes wrote confident false conclusions about a broken host into the durable record, and one reached the product. Owned as step 4 of the `full-tool-access` ADR, deliberately out of this campaign. | `docs/backlog/full-tool-access/` |
| **F8** — a repo rule paraphrased more narrowly than the rule | minor | **open (lesson only)** | Task `001` restated a comment rule more narrowly, the agent complied with the restatement, and the reviewer caught the violation by reading the rule itself. Task already ran; the lesson is to cite a rule by name rather than restate it. | Fold into the authoring skills (see F3/F4) |
| **F9** — the preamble forbids the reads it grants | major | **fixed** | Under advanced mode the security preamble both granted and banned reading `.worc-io/`, and a reviewer resolved the contradiction by refusing to review. All three sentences fixed; the write ban is untouched in every configuration. | `6b9e349` — [record](e2e-trial-mobile-template.fixes.md#f9--the-security-preamble) |
| **F10** — a gating verdict that names no path | major | **fixed as a warning** | A blocking verdict whose findings name no path routes to a node whose job is to open a named location, so the round can only end in a refusal. The operator chose to announce the wasted round rather than prevent it — the cost is kept, not removed. | `702add5` — [record](e2e-trial-mobile-template.fixes.md#f10--a-gating-verdict-that-names-no-path) |
| **F11** — the observe turn judged a loop from one side | major | **fixed** | The supervisor diagnosed a rework loop backwards and sent the operator hunting a timeout that never happened, because under the shipped cadence the clean `fixing` round it was reasoning about was never observed. The preceding steps' own reports now reach the prompt. | `dedaff9` — [record](e2e-trial-mobile-template.fixes.md#f11--the-observation-that-judged-a-loop-from-one-side-of-it) |
| **F12** — `status` names the wrong node in a decompose region | minor | **open** | `worc status` reported `documentation` twice while subtask 3's `implementation` was starting, so an operator would believe a five-subtask task had reached its last stage. Functionally nothing was wrong; the symptom is located and the write site is not. | Orchestrator source — needs investigation first |
| **F13** — `prompt-audit` records the override, not the effective value | minor | **open** | The audit artifact carries the per-node override (`None` for every node but one) while the effective model/reasoning lives in `request.json`, so the two disagree. An operator auditing "did the reviewer run at `xhigh`?" cannot answer from the record named for auditing. | Orchestrator source — record the resolved value, or both |
| **F14** — the handoff floor is built from `depends_on` | minor | **open** | The subtask brief's factual floor names only declared dependencies, though every earlier subtask committed to the same branch is a predecessor in fact. A subtask with no `depends_on` gets no handoff at all, even with three already committed. | Orchestrator source — `core/orchestrator.py`, handoff floor |
| **F15** — a correct finding with an invented authority | minor | **open, blocked** | A blocking finding was right about the defect and cited a document that says the opposite, which sends the fixer to the wrong file first. Needs a `review.md` line: a finding citing a document quotes it and names the artifact. | Role prompt `review.md` — **blocked by its size ratchet** (below) |
| **F16** — a property rule restated as a path list | minor | **open (lesson only)** | Subtask 04 wrote "leave full-bleed screens alone" and then enumerated two directories; a non-full-bleed page under one of them was dropped, which cost a real delivery gap. Task already ran; same class as F8. | Fold into the authoring skills (see F3/F4) |
| **F17** — a task cannot contribute to its own commit message | minor | **open** | Two acceptance criteria in one task asked for content in publication surfaces no node can write. It also blocks F18's remaining half: with no channel, every task's commit type is `feat`. | Orchestrator source **or** authoring guidance — needs a shape decision |
| **F18** — the squash message the tool did not write | minor | **fixed** | With no `--subject`/`--body` the target repository's settings chose the message: the PR title (no Conventional Commits type) and every branch commit concatenated, audit-trail commit included, on a real `main`. The orchestrator writes both now, and the dry run prints them. | `cf06ac5` — [record](e2e-trial-mobile-template.fixes.md#f18--the-merge-message-the-tool-did-not-write) |
| **F19** — help for a flag that is not there | nit | **fixed** | `top --log-file`'s help described the path as "passed to `watch --log-file`", an argv argparse rejects. It now names the parent-flag form, and the test pins both forms rather than the prose. | `8c8841d` — [record](e2e-trial-mobile-template.fixes.md#f19--the-help-text-for-a-flag-that-is-not-there) |
| **F20** — `run` reads as parked, and `rerun` would start a second engine | major | **fixed** | `worc run` recorded nothing about itself, so its task read as `parked (no daemon)` for the whole run and `rerun --continue` passed every refusal. `run` now writes its own liveness marker and every probe and guard reads it. | `32ebf2a` — [record](e2e-trial-mobile-template.fixes.md#f20--the-executor-that-recorded-nothing-about-itself) |
| **F21** — no check verdict on a pass | major | **fixed** | The reviewer was never given the check results, so it re-ran the build inside its own sandbox and blocked on F7's phantom failure. `checks.json` is published on the pass edge and `review.md` says the gate is not the reviewer's to run. | `9d158ed` — [record](e2e-trial-mobile-template.fixes.md#f21--the-check-verdict-on-a-pass) |
| **F22** — a task spec contradicted by the repo it describes | minor | **open** | The target's own `docs/tasks/002-…md` claimed nothing documented the back-button ladder while two files already did. Task authoring in the target repository, not orchestrator behavior. | Target repo |
| **F23** — the move nobody was told about | (filed as one line) | **fixed as visibility** | `finalize` moves the task file and commits nothing, leaving a tracked deletion in the operator's tree that no surface mentioned. The contract not to commit is right — the operator may be on `main` — so both surfaces now name the move. | `5df064f` — [record](e2e-trial-mobile-template.fixes.md#f23--the-move-nobody-was-told-about) |
| **F24** — the runtime home in the review diff | minor→major | **fixed** | An operator config edit rode into the review diff of every following task, with severity escalating from ignored to `blocking` — a finding no agent is permitted to fix. `.worc/` and `.worc-io/` are excluded from the review diff and from the base merge. | `c1d4355` — [record](e2e-trial-mobile-template.fixes.md#f24--the-runtime-home-in-the-review-diff) |
| **F25** — the merge gate cannot be operated under a live daemon | major | **fixed** | `merge-task --dry-run` exited 1 while the daemon ran, which is exactly the state an operator using the human merge gate is in. The guard moved below the flag on three commands, each dry run naming the executor that owns the clone. | `39dc2a9` — [record](e2e-trial-mobile-template.fixes.md#f25--a-plan-is-not-a-mutation) |
| **F26** — `confirm_next_task` makes the daemon unkillable | major | **fixed** | The claim gate waited inside a tick with no cancellation, borrowed the 8-hour HITL timeout, and forgot a refusal — so the daemon could only be killed and the operator was re-asked every tick. All three halves fixed. | `774f02a` + `cfac207` — [record](e2e-trial-mobile-template.fixes.md#f26--the-claim-gate-the-stop-ladder-could-not-reach) |
| **F27** — codex `process_crashed` at ~33% on this host | minor here | **open** | Codex died on its own model-cache schema; the orchestrator classified it, fell back, and the fallback produced the better review both times. A provider CLI defect with an environment fix, recorded so the signature is recognisable. | Environment / codex version pin |
| **F28** — the unbounded Telegram call | major | **fixed** | A terminal notification with no deadline wedged the daemon for ~10 minutes, past the stop ladder and into a forced kill. Every call is bounded now except the HITL poll, which carries its own deadline. | `67600b9` — [record](e2e-trial-mobile-template.fixes.md#f28--the-unbounded-telegram-call) |
| **F29** — a declined tick summarised as an empty queue | nit | **fixed** | The gate said it was not claiming a named pending task and the summary directly under it said "no pending tasks". Withheld ids are collected and named, and an unmerged dependency's skip is reported the same way. | `6a860cf` — [record](e2e-trial-mobile-template.fixes.md#f29--no-pending-tasks-printed-under-the-name-of-a-pending-task) |

## What the ten open findings actually need

They collapse into four repairs, two skill edits and one blocked prompt line — three of the ten have no artifact left to repair.

| Finding | Lever | What it takes | Blocked by |
| --- | --- | --- | --- |
| F14 | orchestrator source | Build the factual floor from the subtasks actually committed to the branch rather than from declared `depends_on`, and give a subtask with no declared dependency a floor at all. | — (site located, ready) |
| F13 | orchestrator source | Write the resolved model/reasoning into the prompt-audit record, or both the configured and the effective value. | — (cheapest of the four) |
| F17 | orchestrator source **or** skills | Either give an agent's summary a way into the commit message, or have the authoring skills say these surfaces are not addressable. | A shape decision by the owner; it also gates F18's per-task commit type |
| F12 | orchestrator source | Find the write site that leaves `current_node` naming the main graph's successor inside a decompose region, then fix the bookkeeping. | Needs investigation — the finding names the lever tentatively |
| F3 | skill `worc-deco-task` | State that a constraint binding one subtask belongs in that subtask's body, and that `depends_on` also decides what the next subtask is told. | — |
| F4 | skill `worc-config` | Add `disable_read_isolation` to the security-key step. | — |
| F5, F8, F16 | authoring skills | Nothing to repair: the task files ran months of work ago. The transferable rules are "cite a repo rule, do not restate it" (F8), "a property rule is not a path list" (F16) and "make acceptance criteria greppable" (F5). | — (fold into the F3/F4 pass) |
| F15 | role prompt `review.md` | One line: a finding that cites a document quotes it and names the artifact it is quoting. | **The file's size ratchet.** It sits at 1,792 estimated tokens against a `SIZE-001` warn of 1,800 — about 32 characters. `mdlint` exits 0 on a warning, so this is a deliberate choice, not a wall: remove something, or accept a new warning. The config says the threshold is never raised to silence a finding. |

## Left open inside findings that are otherwise fixed

| From | What remains | Why it was left |
| --- | --- | --- |
| F10 | The wasted fix round is announced, never prevented. On the trial that was 426s and 474s (~$2.9 each) establishing there was nothing to fix. | The operator chose the narrower half deliberately. |
| F11 | The same turn endorsed three false blockers ("I verified all three findings; all are accurate") having never read the subtask spec that forbade the changes they demanded. | F1's starvation on another surface: it needs the spec in the observe turn's reach, not more of the run's own output. |
| F23 | An uncommitted task-file move still rides into the next task's review diff — F24's mechanism on a path the agent may write. The false docstring claim ("covers the same set the code commit does") is corrected in place. | Excluding `tasks/` from the review diff would remove the only surface that shows an agent editing its own task file. A decision with a security side. |
| F18 | Every task's commit subject is `feat(<id>): <title>`; the type cannot be chosen per task. | That is F17. |
| F2 | Fixed as a static defect with no evidence it has ever bitten — the trial's own prediction about it was falsified. | Nothing to do; recorded so the entry is not re-opened as unproven. |

## Verification gaps — not defects, and not fixable by code

| Gap | Why it is open |
| --- | --- |
| `confirm_next_task` approve / deny branches | Both need a human pressing a button in the operator's chat. Faking an approval would destroy the only thing the gate is for. |
| `git.auto_merge: true` | Deliberately never run on the trial: it bypasses the human review gate on a real repository. |
| F20's step 4, proven live | Proving it needs a real `rerun --continue` against a running task, which would corrupt the branch it runs on. Proven statically; a live proof needs a throwaway repo. |

## Cosmetic drift — recorded, no lever worth spending

The target's installed `.worc/config.example.yaml` names an older model than the packaged copy (an artifact of when it was installed), and its generated `config.yaml` header calls the whole runtime home gitignored while the generated `.gitignore` deliberately re-includes the config with its own rationale.

## Budgets a taker will meet

- **`review.md`**: 1,792 of a 1,800 warn / 2,600 error `SIZE-001` budget. This is what blocks F15 and what any future reviewer guidance has to pay for.
- **The findings document**: 27,412 of a 26,000 warn / 32,000 error budget — already over warn, which is why fixes are recorded in the companion file and the finding keeps only a marker line. About 4,600 tokens of room before the gate turns red.
- **The gates are manual here**: `.git/hooks/pre-commit` is not installed and `WASTECH_MDLINT_HOME` is unset, so `python tools/mdlint.py` and `npx prettier@3 --check` have to be run by hand. That is how the corpus drifted twice.
