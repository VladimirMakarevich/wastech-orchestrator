# E2E trial on `wastechlab-mobile-template` — what was fixed

Companion to [e2e-trial-mobile-template.md](e2e-trial-mobile-template.md), which records the findings and does not change as they are repaired. This document is the other half: for each finding taken up, what was reproduced, what landed, and — where reproducing it showed the finding itself to be wrong — what the correction is.

Two rules it follows. **Reproduce before repairing:** every entry below names the probe that showed the defect, and a fix whose test does not fail against the previous code is not recorded as one. **Correct the finding, do not quietly work around it:** most entries here revise a claim the trial made — a wrong diagnosis, a wrong scope, an exposure that turned out not to exist, a prediction the trial itself falsified — and those revisions are the most useful thing in the document, because the trial's own numbers and its ranking were argued from them.

Kept separate from the findings document for a mechanical reason too: that document is already over its `mdlint` `SIZE-001` warn budget (26,000 estimated tokens for `docs/backlog/**`, error at 32,000), and appending a fix record per finding would have pushed it past the error threshold. The budget is a ratchet that is never raised to silence a finding.

The table below covers what this document records: the findings that were repaired. For every finding — open, fixed, declined, and what blocks the rest — see [the status index](e2e-trial-mobile-template.status.md).

## Status at a glance

| Finding | Severity as filed | State | Where |
| --- | --- | --- | --- |
| F1 — the reviewer is blind to the subtask spec | major | **fixed** | `core/flow/nodes/evaluator.py`, `review.md` |
| F2 — "do not flag missing docs" on a docs-only deliverable | minor | **fixed** | `review.md` |
| F24 — `.worc/` in the review diff | minor→major (variance was the finding) | **fixed** | `git_manager.py` |
| F9 — the preamble forbids the reads it hands the node | major | **fixed** | `core/flow/security_preamble.py` |
| F21 — no check verdict on a pass | major | **fixed** | `core/flow/nodes/checks.py`, `review.md` |
| F10 — the contract cannot say "I could not review" | major | **addressed as a warning** (operator's call) | `core/flow/nodes/evaluator.py`, `orchestrator.py`, `review.md` |
| F28 — the terminal Telegram notification has no timeout | major | **fixed** | `notify/telegram.py` |
| F26 — `confirm_next_task` makes the daemon unkillable | major | **fixed** | `notify/`, `composition.py`, `cli.py`, config |
| F20 — `run` reads as parked, and `rerun --continue` starts a second engine | major | **fixed** | `process_control.py`, `cli.py` |
| F25 — the merge gate cannot be operated under a live daemon | major | **fixed** | `cli.py` |
| F18 — `merge-task` cannot control the squash commit message | minor | **fixed** | `git_manager.py`, `orchestrator.py`, `cli.py` |
| F11 — the observe turn is blind to the step it is explaining | major | **fixed** | `core/supervisor.py` |
| F29 — the summary of a declined tick reads as an empty queue | nit | **fixed** | `cli.py` |
| F19 — `top --help` documents an argv argparse rejects | nit | **fixed** | `cli.py` |
| F6 — control-state drift fires on the operator's own IDE | minor | **fixed** | `git_manager.py` |
| F23 — `finalize` leaves the task-file move uncommitted | (one line, no section) | **fixed as visibility** | `cli.py`, `orchestrator.py` |
| F14 — the subtask handoff's factual floor is built from `depends_on` | minor | **fixed** | `core/orchestrator.py`, `core/supervisor.py`, `implementation.md` |
| F13 — `prompt-audit` records the override, not the effective value | minor | **fixed** | `routing/router.py`, `core/flow/observability.py`, `config/schema.py` |
| F17 — a task cannot contribute to its own commit message | minor | **fixed** (shape chosen by the operator) | `task/model.py`, `task/validation_gate.py`, `core/orchestrator.py`, `worc-task`, `worc-deco-task` |
| F12 — `status` names the wrong node in a decompose region | minor | **fixed** | `core/orchestrator.py` (`_fan_out_subtasks`) |
| F3 — the root task arrives as a footer path | minor | **fixed** | `worc-deco-task/SKILL.md` |
| F4 — two of three install-written security keys | minor | **fixed** | `worc-config/SKILL.md` |
| F5 + F8 + F16 — the three authoring lessons | nit / minor | **fixed as guidance** | `worc-task/SKILL.md`, `worc-deco-task/SKILL.md` |

## F1 + F2 — the reviewer under decomposition

Fixed as a pair, which the finding requires: F2 currently _suppresses_ F1's last false blocker, so repairing F2 alone would have given subtask 04 a fresh false blocker about documentation nobody had written yet.

### F1, reproduced

Two tests, both red against the previous code. The first renders a `review` role prompt inside an active decompose region and asserts the subtask clause appears; it rendered as the bare word `Review.`, the whole `{?subtask_spec_path}` block dropped, because `_prompt_variables` never published the variable the block is keyed on. The second compares the agent runner's published variable names against the evaluator runner's directly.

### F1, what landed

`evaluator.py::_prompt_variables` publishes `subtask_order`, `subtask_count` and `subtask_spec_path` when `ctx.subtask_order` is set — the same three the agent runner has always published, guarded the same way, so a whole-task run renders nothing rather than "subtask None of None". The runner already used `ctx.subtask_order` for its own artifact namespacing; it simply never passed it on.

`predecessor_context` is deliberately **not** published to an evaluator: it is the author's handoff brief, assembled for the node that writes the subtask. The anti-drift test asserts that this is the _only_ difference between the two runners' key sets, so the next channel wired into one and forgotten in the other fails a test instead of a run. That test exists because this is the second time these two diverged on one channel — `_memory_path`'s own docstring records the first, where the memory packet was wired for agent nodes only and left `review.md`'s `{?memory_path}` block dead.

Publishing the variable is only half a fix: a variable nobody references changes nothing. The packaged `review.md` gains a `## Subtask Scope` block, delimited and placed exactly like the sibling blocks in `implementation.md` and `fixing.md`, telling the reviewer which subtask it is looking at, that the spec — not the root task — is what the diff is measured against, that later-subtask work is not missing from this one at any severity, and that a change the spec forbids is a finding rather than something to ask for. That last clause is the trial's exact failure: subtask 01's spec ended "**Do not** add an overlay, modal or page rule here", and the reviewer blocked it for not adding them.

### F2, and an honest downgrade

The clause ("Documentation, changelog, and status-doc updates run in a later step of this flow, so do not flag those as missing") is correct for a code task and wrong for a docs-only deliverable, where the docs are the entire product. It is now conditional — "unless the diff is **only** documentation, which makes it this task's deliverable and its gaps the review" — which is the shape the finding asks for: a condition, not a deletion.

**The runtime risk is unproven, and the trial's own author said so.** The prediction that the clause would make the reviewer under-review the docs-only subtask was **falsified**: that review ran 383.8s, its longest turn of the run, and found a real documentation defect. The reason is precise rather than reassuring — the clause speaks to _missing_ doc changes, and that finding was about an inconsistency _inside_ doc changes that had been made, so the instruction was never engaged. F2 therefore stands as a static defect (the sentence is unconditional and would still misfire where the gap really is missing documentation) with no evidence that it has ever bitten. Fixed because it is one clause and the condition is free, not because it was measured.

### The budget this cost

`review.md` is the most cost-sensitive Markdown in the repository — paid for on every node run of every task — and `mdlint` ratchets it at 1,800 estimated tokens, a threshold calibrated when this same file was the largest role prompt at 1,458. The F21 and F10 lines took it to 1,749, leaving 206 characters. F1's section did not fit.

It was paid for rather than waived: the F10 bullet was folded into the existing "each entry states…" rule, which already mandated the path and so overlapped with it, and the F21 and F2 clauses were tightened. Net result 1,792 tokens — inside the ratchet, one rule about finding shape instead of two bullets restating each other. Worth stating for whoever takes the next finding: **the review prompt has about 30 characters of headroom left.** Further prompt guidance needs either something removed or a deliberate decision about that threshold, which the config says is never raised to silence a finding.

## F24 — the runtime home in the review diff

Fixed on the branch that carries this document. Two things the finding got wrong turned up while reproducing it, and both are worth keeping because they change what the fix is _for_.

**The scope is 36 files on this target, not one.** The finding traced the exposure to `!.worc/config.yaml`. The installed `.gitignore` actually ignores the runtime home's _contents_ (`.worc/*`) precisely so it can re-include three things — `!.worc/flows/`, `!.worc/tools/`, `!.worc/config.yaml` — and `git ls-files .worc/` on the target returns **36 tracked files**: the config, every flow YAML, and every role prompt. So the contaminating surface is not one operator toggle; it is every `worc-flow-role` edit an operator makes between runs. The exclusion is therefore of the whole runtime root, which is what the code-commit side already does (`changed_code_paths` drops everything under `_excluded_dirs`).

**The base-merge exposure does not exist — a third guard the code-read missed catches it.** Item 3 above says a modified tracked `.worc/config.yaml` "would be swept into that merge commit" by `finalize_base_merge`'s `git add -A`. It would not. `commit_merge_resolution` calls `assert_staged_allowed` → `assert_exchange_never_staged`, which checks the _index_ rather than the ignore rules and refuses outright. Reproduced by reverting the fix and running the real merge:

```
ManualActionRequired: refusing to commit: runtime artifact path(s) would be committed
(.worc/config.yaml); the exchange/private home must never enter a commit.
```

Nothing leaks. What is actually wrong is the other way round: the guard fires on an **ordinary operator action** — editing the config or a flow prompt while a task runs — and reports it in the vocabulary of a security violation, with no remedy but reverting that edit. So the base-merge half of the fix converts a fail-closed dead end into a non-event, and leaves the guard for the case it is really for (a force-added path). Severity of that half: **minor**, and usability rather than integrity.

**What landed**, in `git_manager.py`:

1. `write_current_diff` diffs `-- :/ :(exclude,top).worc/ :(exclude,top).worc-io/` — the review diff no longer carries a path no agent may read or write. `:/` + `exclude,top` anchors the pathspec at the repo root rather than the process cwd; a bare `:(exclude).worc/` silently stops excluding anything from a subdirectory (verified on real git).
2. `commit_merge_resolution` excludes the same roots from its `git add -A` — **per root, and only when that root is not ignored as a whole**. `git add` refuses (exit 1, "The following paths are ignored … use -f") whenever a pathspec names a root that exists on disk and is entirely ignored: it stages the right set and _then_ reports failure. A blanket exclusion would therefore have broken every base merge on a default `worc install`, which ignores all of `.worc/`. This is the identical trap `staged_pathspec` already documents for the task lifecycle dir, and it has the identical escape — a fully-ignored root cannot hold a tracked file, so it needs no guard. Both shapes are covered by a test.
3. The two docstrings that carried the false premise ("`.worc/` is gitignored, so `git add` skips it") now name the real reasons: an explicit changed-code-path list on the commit side, `assert_exchange_never_staged` on the merge side.

## F9 — the security preamble

The paragraph above calls the refusal an easy misreading and lists three things that make it easy. There is a fourth, and it settles the question: **under advanced mode the block forbids reading `.worc-io/` outright.** Rendered at exactly the configuration this trial ran (`read_isolation_off=True, advanced_mode=True` — advanced mode is ON in the pinned environment table), the third paragraph ends:

> And do not read or write `.worc/`, `.worc-io/`, `.git/` or `tasks/`. Both are checked after you finish…

Verified present at the audited commit (`git show 3e472b699:…/security_preamble.py`), so it is what actually reached the reviewer. The trial only quoted the first two paragraphs, which is why the entry reads as a wording nit.

That changes the diagnosis. The prompt did not merely invite a misreading — it contained a **flat contradiction**: one bullet grants "read only the paths you are given" under `.worc-io/`, and a later paragraph bans reading `.worc-io/`. A model resolving a contradiction is not being careless, and that is a better explanation of the intermittency than "same prompt, same provider, different outcome": it hit 1 review in 6 on task `001` and then the _first_ review of both `002a` and `002b`, because there was nothing in the text to converge on.

Fixed in all three sentences, on the branch that carries this document:

1. The bullet leads with the grant — "the paths you are given under it are yours to read — that is what it is for, and nothing below takes that back" — instead of leading with a restriction that reads like the `.worc/` ban three characters away.
2. The read-isolation paragraph's blanket ("or any orchestrator-private file") is replaced by the two things it actually means (`.worc/` and credential/environment files), plus one sentence saying the exchange paths are not among them.
3. The advanced-mode paragraph bans **writing** `.worc-io/` and no longer bans reading it.

The write ban is untouched in every configuration — the exchange is the immutable surface a post-node fingerprint checks, so a node writing it is a containment event — and `.worc/` stays read- and write-banned in all three places. Three tests pin exactly that split, and each fails against the old wording.

## F21 — the check verdict on a pass

Both halves, exactly as this entry proposed.

`checks.py` publishes `{checks_path}` on the pass edge too, with a payload chosen for its reader rather than copied from the failure path: the failure edge still hands `fixing` the first failing command's log, because that is the text it acts on, while a pass writes `checks.json` — one entry per command with `command`, `exit_code`, `passed`, `skipped` and `timed_out`. `skipped` is carried explicitly so a reader cannot count a check whose toolchain was absent as a pass; that distinction is what a reviewer asked to confirm "the checks pass" actually needs, and a log tail does not carry it.

`review.md` gains the line the entry says it is missing: the check gate belongs to a Check Runner outside the reviewer's sandbox, its exit codes are the authoritative verdict, the per-command results are at `{?checks_path}`, and the reviewer neither runs the build itself nor raises a finding from having done so — its sandbox is not the environment the gate runs in, so a failure there is evidence about the sandbox.

Note this does not fix **F7**; it removes F7's reach into the review path. The reviewer no longer has a reason to run the build, so F7 stops manufacturing blocking findings there. An `implementation`/`fixing` node is still told by `implementation.md` to run `npm run build` and still cannot, which remains step 4 of the `full-tool-access` ADR.

One consequence worth recording, because it showed up as four broken tests: a passing `command_profile` node now creates its per-run artifact directory, which it previously did only on the citation and dependency-scan paths. Four unit tests were passing the shared `/art` placeholder as their artifacts root and had to be given a real one, the way the citation tests already did.

## F10 — a gating verdict that names no path

The entry proposes treating a pathless blocking finding as an infrastructure failure of the node: park it, notify, do not spend a fix round. The operator chose the narrower half deliberately: **warn, do not block.**

What ships: the evaluator runner flags a verdict that gates while **none of its gating findings names a path**, and the orchestrator's post-node hook turns that into an operator warning on the console — always, independent of Telegram — plus a ⚠️ `rework (no gating finding names a path)` label on the live trace, through the same surface `rework_exhausted`, `unexpected_write` and `git_control_drift` already use. Routing is untouched: the verdict still takes the rework edge.

State the cost plainly, because the choice keeps it: **the wasted fix round is not prevented, only announced.** On this trial that was 426s and 474s (~$2.9 each) in two rounds that correctly established there was nothing to fix. What the operator gains is the ability to tell a wasted round from a productive one _while it is running_, rather than reading it out of the ledger afterwards — and the loop stays bounded by `budgets.review_fix` as before.

The trigger is deliberately narrow: **all** gating findings pathless, not any. One pathless blocker beside a located one still leaves `fixing` real work, and an advisory pathless finding routes nowhere and costs nothing. Four tests pin those three negatives against the positive.

`review.md` gains the matching line, and its first draft had to be thrown out: telling the reviewer to report an inability-to-review by returning an empty `findings` array would have made it **accept** — an empty array is the well-formed clean verdict, so the flow would have published an unreviewed diff. What it says instead is that a blocking finding names a path, never an invented one, and that an inability to review belongs in the finding's own text in those words.

Incidental, found in the same dict: `TRACE_ADOPTED_COMMITS` was missing from the emoji map, so the one publish case whose own docstring calls its ⚠️ "the only place it is said" rendered as a neutral ▶️. Fixed with a test.

## F28 — the unbounded Telegram call

Reproduced as a defect of the _caller_, which is where the finding lands after the code is read properly. Six tests, all red against the previous code and none of them slow: the helper that owns the synchronous contract, `_run_sync` ([telegram.py](../../../src/wastech_orchestrator/notify/telegram.py)), took no deadline argument at all, so the two tests that pass one failed with a `TypeError` rather than by hanging, and the two notifier-level tests failed on the absent `_CALL_TIMEOUT_S`. `grep -c "is_cancelled" notify/telegram.py` still returns **0** — that half is F26's and lands with it.

**One half of the proposed fix is a no-op, and it is the half the entry leads with.** "Pass an explicit timeout into `send_message`" restates a default: `python-telegram-bot` 22.8's `HTTPXRequest` already ships `connect_timeout`, `read_timeout` and `write_timeout` at 5.0s and `pool_timeout` at 1.0s, so every HTTP phase of every call was **already** bounded when the daemon wedged for ten minutes. Whatever stalled was therefore not an HTTP phase, and no per-request timeout would have reached it. What was missing is a bound on the _caller_ — and that is the only thing "best-effort" was ever short of, because the code delivers "never raises" exactly as documented.

**A second unbounded stretch, which the finding does not mention and which no `wait_for` covers.** `asyncio.run` closes its runner by calling `loop.shutdown_default_executor(asyncio.constants.THREAD_JOIN_TIMEOUT)`, and that constant is **300 seconds** (verified against the running interpreter's own `asyncio.runners.Runner.close`). So a blocking call already handed to the default executor keeps the send parked for up to five minutes _after_ its await is cancelled. Two such calls sit inside the observed ten-minute window; recorded as consistent with the observation rather than as its proven cause, since the live wedge left a `sample` and not a stack of the executor.

**What landed.** `_run_sync` takes `timeout` and enforces it in two layers, because either alone is a half-fix: `asyncio.wait_for` cancels the await (the clean path, which lets the coroutine unwind), and the thread it runs on is abandoned when that does not return within a two-second grace (the hard path, which covers an await that swallows cancellation and the executor join above). Abandoning is safe by construction — the thread is a daemon, it shares no state with the caller, and every caller already treats the failure as a transport error: `_safe_send` logs one redacted warning, and `start_ask` returns an undelivered handle that `wait_for_answer` maps to `transport_error`, which is fail-closed.

The `timeout` is passed by **every** client method except `poll_reply`, which carries the operator's own hours-long deadline; a test pins that policy per method, because clipping the HITL poll to 20s is the obvious way to "fix" this and would silently break the human gate.

Incidental simplification, not a separate change: the helper's two branches collapsed into one. The `asyncio.get_running_loop()` probe and the `ThreadPoolExecutor` fallback existed only to answer "does the caller already own a loop?", and running every call on its own thread makes the question moot. The regression test still covers the case that branch was for — a `_run_sync` call made from inside a running loop.

## F26 — the claim gate the stop ladder could not reach

Three defects under one finding, and they are independent enough that each landed with its own test and its own commit: the wait could not be interrupted, the gate borrowed the HITL timeout, and a decline was forgotten between ticks. The first is the safety one — it is the reason the ladder's hardest rung was reached on a healthy daemon.

### The wait is abandonable

Reproduced at both ends of the wiring. `grep -c "is_cancelled" notify/telegram.py` returned **0**, exactly as filed. The proof that it was a _wiring_ gap rather than a missing feature is the composition test: reverting one line (`build_notifier(config.telegram)`) turns it red, because the predicate the daemon already builds — the same object the flow engine and the router receive — simply stopped at their two constructors.

What landed, in one line of `composition.py` and the path it opens: the notifier holds the predicate, `wait_for_answer` consults it before entering the wait, and the poll loop consults it between requests, raising rather than returning so a stop is never mistaken for a timeout. The Core-facing vocabulary gains a fourth `AskFailure`, `cancelled`, which `core/hitl.py`'s `_RESETTABLE_STATUSES` picks up for free — that set is derived from `get_args(AskFailure)` precisely so a new failure mode is never silently skipped, and this is the first time that seam has been used.

Two decisions in it worth stating, because neither is forced by the finding:

**`start_ask` withholds the prompt when a stop is already pending.** Sending it would leave the operator a button nothing reads: the wait is abandoned immediately and the answer arrives after the process is gone. `wait_for_answer` therefore checks cancellation _before_ its undelivered-handle branch, so a withheld prompt reports `cancelled` and not `transport_error` — otherwise the fix would manufacture a diagnosis pointing at a network problem that never happened. A test pins that ordering.

**The long poll is capped at 5s (was Telegram's 50).** The predicate is only read between requests, so the chunk — not the predicate — is what decides how fast a stop is noticed. At 50s a `stop` still misses its own 30s patience and still escalates to a tree kill, which would have made the rest of the fix decorative. The cost is a request every 5s while a prompt is pending, and only while one is pending. A test asserts the value passed to `getUpdates` stays inside the ladder's patience rather than asserting the constant, so the property is what is pinned.

An incidental refactor came with it: `wait_for_answer` crossed the `PLR0911` return ratchet at 7, so the two duplicated transport-error results became `_transport_error(handle)` and the reply-mapping tail became `_answer(handle, reply)`. The ratchet is a burn-down backlog; adding a per-file ignore to it would have been the wrong direction.

### The gate has its own timeout

Reproduced by construction: `dataclasses.replace(auto_mode, confirm_timeout_s=900)` raised `TypeError` because the key did not exist, and the gate read `config.telegram.ask_timeout_s` — the finding's point exactly. What landed is `orchestrator.auto_mode.confirm_timeout_s`, default **900s (15 minutes)**, validated `> 0`, `CONFIG_SCHEMA_VERSION` 39 → 40, and the packaged `config.example.yaml` + operator reference carry it.

The two timeouts are answers to different questions, which is why one key could not serve both: `telegram.ask_timeout_s` bounds a node asking a human to decide something **mid-run**, where the alternative to waiting is parking a half-finished task, so eight hours is right. The claim gate holds an **idle slot** and gets another chance on the next tick, so the same eight hours buys nothing and costs a daemon that (before the fix above) could only be killed. Zero is rejected rather than treated as "off": it would fail closed on every tick with no chance to answer, which reads as a broken auto mode; off is `confirm_next_task: false`.

### A refusal is remembered

The finding's second horn — "an operator who says no once is asked again 60 seconds later, forever, with no backoff and no 'asked N times' state" — reproduced as a test that drives three ticks and counts prompts: **3 before, 1 after**.

`watch_loop` now owns a `{task_id: cool-off end}` map for the daemon's lifetime and passes it into `watch_once`; a refusal — deny, silence, or no transport alike — is remembered for **an hour**, after which the gate asks again. Four properties are pinned by tests, and three of them are the ones easy to get wrong: a refusal is not permanent (a decline means "not now", and a daemon runs for days), an approval remembers nothing, and a caller that passes no map behaves exactly as before (a single pass has nothing to remember).

Three deliberate non-changes, each of which would have been scope creep:

- **The memory is not persisted.** A durable decline would need its own expiry story and an operator command to clear it, and a restart is already the operator's own "ask me again".
- **A remembered refusal still ends the cycle** rather than falling through to the next pending task. That is exactly today's behavior — a fresh decline `break`s too — so a lower-priority task was never reached in this state either. Changing it is a queue-semantics decision, not this finding.
- **The skip is logged at `debug`, not `info`.** An hour of info lines saying "still refused" is the noise the finding complains about in another form. The consequence is that the operator-facing summary for such a tick says "nothing to do", which is **F29** — the same sentence, reached by a second route.

## F20 — the executor that recorded nothing about itself

Every step of the chain reproduced exactly as filed, and the fix is the one the finding calls cheapest — with one substitution it warns against taking literally.

**Not the same PID file.** The finding offers "have `cmd_run` write the same PID file (or a run-scoped liveness marker)". The first option would have created a worse bug than it fixed: `worc stop` reads that file and asks the recorded process to finish, and `run` installs **no** stop wiring — no `SIGTERM` handler, no stop-file poll — so a `stop` against it would wait out its 30s timeout, get no confirmation and escalate to a kill, on a process that was working correctly. That is the same shape as the F28 wedge and it would have been reachable from a single keystroke. So `run` writes its own `orchestrator.run` beside the daemon's `orchestrator.pid`: every "is anything executing in this clone?" probe now answers yes, while the stop ladder keeps addressing only the process that can answer it.

What the two markers feed:

- **The label.** `_display_status`'s parameter is now `executor_alive` rather than `daemon_alive`, fed by a probe that reads both files. The rename is the point rather than tidiness: the old name is what made "no daemon" and "nothing is executing" look like the same question. `parked (no daemon)` still means what its docstring says — parked at a checkpoint, awaiting resume — and now it is only printed when that is true.
- **The guards.** `rerun`, `finalize`, `merge-task` and `prs --sync` carried four copies of the same daemon check; they now share one `_executor_refusal`, which names either owner and gives the advice that fits it (the daemon is stoppable; a `run` is not, so the only honest instruction is to wait or interrupt it where it runs). `watch` gains the same refusal, and so does `run` itself — a second `run` in another terminal was never refused either, which the finding does not mention.
- **A prompt nobody noticed.** `stop`'s parked-slot note recommends `rerun <id> --continue`, and it is printed after any `stop`. Against a live `run` that is the one command that must not be issued, so the note now names the executor holding the slot and says the stop ladder does not reach it. The finding traces the mislabel through `status`/`list`/`top`; this fourth surface is the one that hands the operator the dangerous command in a sentence.
- **A comment that was load-bearing.** `plan_rerun`'s own comment justified accepting a `running` row by asserting "`cmd_rerun` already refused if a live watch daemon owned the slot, so a `running` row reaching here is daemon-less". It now says "any executor", which is what the guard finally checks; before the fix that sentence was the false premise the whole hazard rested on.

Eight tests, all red against the previous code: the marker is written for the duration and reaped in a `finally` (a leaked marker would refuse those commands forever), the probe and the label agree, the stop note no longer offers `rerun`, and `rerun`/`watch`/`finalize`/`prs --sync` each refuse before building an engine.

**Two things deliberately left alone.** `logs clean` still waits on `_daemon_alive`, not the new probe: it is about the daemon's own rotating log handle under `.worc/logs/`, and a `run --log-file` writes wherever the operator points it — a real but different question, unobserved and not this finding. And the console's `up` does not pre-check the marker; it spawns a daemon that now refuses on its own, and `start_watch` already surfaces a verified-failed spawn with the real error.

## F25 — a plan is not a mutation

Reproduced exactly as filed (`merge-task --dry-run` exits 1 under a live daemon), and the fix is the finding's first bullet. The second — teaching `worc shell` to stop, merge and restart as one operation — is **not** taken: it is a new console verb with its own failure modes (a merge that fails after the daemon is down leaves the operator somewhere neither command promised), and the composition problem it exists to solve is smaller once the plan is readable without stopping anything. Recorded as declined rather than deferred, so nobody looks for it.

**The rule already existed one function away.** `_cmd_prs_sync` guards only its write path and says so in a comment: "The read-only dry-run is always safe." So this was not a policy question but three commands that had not been brought to the policy — `merge-task`, `finalize` and `rerun` all placed the guard above the flag. The finding names only `merge-task`; fixing that one and leaving the other two would have left the inconsistency that made this defect possible in the first place, so all three are fixed, each with its own test pair (the dry run passes; the real thing still refuses).

**The exemption is not silent.** Each dry run prints the owner as a note beside the plan, for a reason that is load-bearing on the `rerun` path specifically: `plan_rerun` accepts a `running` row as directly recoverable, and its own comment justifies that by "`cmd_rerun` already refused if any executor owned the slot". Past the guard that premise no longer holds, so a plan printed there must say who is holding the clone — otherwise the fix would have replaced a refusal with a confidently wrong plan. The note is what keeps `plan_rerun`'s premise visible instead of merely true-most-of-the-time.

The message therefore stopped carrying its own verb (`_executor_refusal(config, verb)` → `_executor_owner(config)`): the same sentence is now needed as a refusal (`merge-task: <owner>`) and as a note (`merge-task: note: <owner>`), and composing it at the call site is what makes both read like the command that printed them.

**Residual, stated because it is real:** a dry run under a live daemon builds a full `Orchestrator`, which opens `state.db` read-write and runs its `CREATE TABLE IF NOT EXISTS` schema pass — a brief write lock, not a mutation of task state. This is not new (`prs --sync`'s dry run has always done it, and every `plan_*` is documented "read-only; mutates nothing" about the _task_, not about the connection), and WAL keeps it clear of the daemon's own short transactions. A genuinely read-only plan path would need `build_orchestrator` to accept a read-only store, which is a larger change than this finding earns.

## F18 — the merge message the tool did not write

The evidence is already in the finding — a real merge, a real subject on a real `main` (`58f16c2`) — so reproduction here is of the code path: two argv tests, red because `merge_pr` took no `subject`/`body` at all.

**The subject was never the orchestrator's to lose; it was GitHub's to invent.** Worth stating precisely, because the entry's framing ("`merge_pull_request` without `--subject`/`--body`") makes it sound like a missing flag on an otherwise-correct message. Without the flags the target repository's settings decide, and this one is set to `COMMIT_OR_PR_TITLE` + `COMMIT_MESSAGES`: with two commits on the branch that resolves to the **pull-request title**, which the orchestrator sets to `p.task.title` — the bare task title, no Conventional Commits type. So the subject that violated the target's first git rule was assembled from an orchestrator input by a GitHub setting, and no flag was going to fix one half without the other.

**What landed.** A single `task_commit_subject(task_id, title)` in the core, used for both commits a task produces: the code commit on the task branch (which already had this shape, inline) and now the squash/merge commit, which adds the `(#N)` suffix GitHub would have appended itself. One place, because the two disagreeing was the defect. `merge_pr` gains `subject`/`body`, emitted only for `merge`/`squash` — a `rebase` writes no commit and `gh` takes no message for it, which is its own test.

The body is deliberately **empty**. Every commit this orchestrator makes on a branch is a single line, so a body assembled from them can only be a list of internal subjects — which is exactly how `chore(orchestrator): audit trail` reached `main`. The readable account of the change is the pull-request body, and it stays on the pull request rather than being copied into the base branch's permanent history.

**The type is still `feat` for every task**, inherited from the code commit this has always produced. That is not a fix, it is the pre-existing convention, and choosing it per task requires the task file to be able to say so — which is the `full-tool-access` backlog's "a task cannot contribute to its own commit message" (F17), untouched. Stated here so the `docs:`-shaped task that lands as `feat:` is a known limit rather than a new surprise.

One consequence worth recording, because it showed up as two failing tests: the **auto-merge** path takes the same message (`git.auto_merge: true`, which this trial deliberately never ran). Two `test_orchestrator` cases pin `gh pr merge`'s argv exactly and had to be updated — which is the intended reading of them: the argv is a security-relevant surface (no `--admin`, no `--dangerously*`), so anything added to it is meant to break a test and be looked at.

**And the dry run says it.** The finding's "cheapest fix" — `merge-task --dry-run` reporting the message policy — is what makes this checkable without merging: the plan carries the subject the merge would write (`MergePlan.commit_subject`) and prints it under the strategy line, with the empty body named. It is the one thing a merge leaves in the base branch forever and the one thing the plan omitted. Reachable under a live daemon as of F25, which is the configuration an operator running the human merge gate is actually in.

## F11 — the observation that judged a loop from one side of it

The wrong diagnosis is reproduced exactly as filed, and so is the wiring gap. But the finding's proposed lever — "its `_run` call passes no `supervisor_packet_path`, unlike the finalize turn" — is treatment of a symptom, and taking it literally would have cost a published packet per observation (a full build plus an exchange copy on every deviation of a deep fix loop) to deliver one field of it.

**The mechanism is the cadence, and the finding stops one step short of it.** The entry says the observe turn "receives only the observed node's own `final_message` plus its `findings`", which is true but leaves the obvious rebuttal standing: the observe session is warm (`resume_own_lineage`), so why had it not seen `fixing`'s report when `fixing` ran? Because it never observed that step. The shipped cadence is `events` with triggers `rework`/`failure`/`fallback` ([`observe_cadence.py`](../../../src/wastech_orchestrator/core/observe_cadence.py)), and a `fixing` round that finishes `done` is not a deviation — so no turn is spent on it and its report never enters the session at all. The observation of the evaluator that reworked afterwards was therefore judging a loop from the only side of it that had ever been shown. "The implementer produced nothing at all" was an inference from an absence in its own context, not a misreading of evidence it had.

That is worth correcting in the record because it changes what a sufficient fix is: not "give the observe turn the whole run", but "give it the rounds it was never shown".

**What landed.** `_step_history` prepends what the preceding steps reported — one line per step, `- <node> (<outcome>): <message>` — read from the same durable sources the finalize packet is built from (the `node_runs` rows plus each run's own `<node_id>.out.md`), so the two surfaces cannot disagree about what a step said. Bounded twice, because this is a per-observation cost: the last **6** runs, each message folded to one line and capped at the packet's existing per-step cap (500 characters — which the finding itself establishes is enough, having checked that the fixing report's whole rationale sits in its first 500). Best-effort like the rest of the layer: a store or filesystem error costs the section, not the observation.

The section's own text carries the instruction that the trial's failure needed: judge a loop from the sequence rather than from an absence of visible work in it, because a step that deliberately changed nothing says so there. That is the sentence the observer was missing, and it is worth more than the data alone — the supervisor had the loop's shape right ("real work, then cosmetic work, then nothing") and drew the opposite conclusion from it.

Five tests. The positive one is built as the trial's exact situation — an unobserved `fixing` round followed by an observed `rework` — and two negatives pin the traps: the observed step is not repeated as its own history (a single step rendered twice looks like the loop the observer is asked to judge), and the first step of a run gets no section at all.

**Not fixed, and it is the more expensive half of the entry:** the supervisor also wrote "I verified all three findings; all are accurate" about blockers that were false, having never read the subtask spec that forbade the changes they demanded. That is the same starvation as F1 on a different surface, and it needs the subtask spec in the observe turn's reach, not more of the run's own output. Left for its own change rather than folded in here.

## F29 — "no pending tasks", printed under the name of a pending task

Reproduced as filed: two adjacent lines disagreeing, because a gate-declined task produces no `PipelineResult` and `_summarize_watch` reads an empty result list as an empty queue.

The fix needed a channel that did not exist — nothing carried "we looked and deliberately did not take it" out of a tick — so a pass now collects the ids it withheld and the summary names them (`nothing claimed (1 pending task(s) held back: 002d) — the log says why`). Exit code stays **0**: a fail-closed decline is the gate working, and the trial verified that exit code as correct.

Two things beyond the entry:

- **An unmerged dependency is withheld too.** A `WAITING` skip has the identical shape — a named pending task, no result, and the same "no pending tasks" summary — and it is a non-blocking skip, so it is the case an operator meets most often. Fixing only the gate would have left the sentence lying in the more common half.
- **The parameter budget was full, and the ratchet is not for widening.** Threading the collector took `watch_loop` to 11 arguments against a `PLR0913` limit of 10. Rather than add the code to `cli.py`'s per-file ignores — the burn-down list, which is only ever meant to shrink — two merges paid for it, and both are things the code already treated as one: the gate's cool-off map and the withheld list became one `WatchNotes` ("what the pass decided about tasks it did not run"), and `stop_event`/`stop_file` became one `StopChannels`, which is how the loop's own docstring had described them all along ("Two stop channels are checked around ticks and during idle sleep"). Net: 10 arguments, two fewer loose parameters than before the finding.

## F19 — the help text for a flag that is not there

One sentence, and the entry has it exactly right, down to the observation that makes it worth fixing rather than shrugging at: **the codebase already knew.** `cli_shell.start_watch`'s docstring records that appending the parent flags after `watch` "is why the old auto-spawn died on an argparse error to a `DEVNULL`'d stderr" — the lesson was learned in the spawn path and not carried into the help text an operator reads.

`top --log-file`'s help now names the working form (`worc --log-file PATH watch`). The test does two things rather than one: it asserts the text no longer names `watch --log-file`, and it pins **both argv forms** — the parent-flag form parses, the appended form raises. A help string is a claim about the parser, so the test that guards it should fail when either half stops being true, not only when someone edits the prose.

Checked while there: no operator-facing document repeats the wrong form (`grep` over the tracked corpus finds it only in this trial's own notes and in the new test's comment), so nothing else needed the same repair.

## F6 — the drift signal that cried wolf once per task

Reproduced directly: a `branch.<name>.vscode-merge-base` written between the two captures produces `config: repo config key changed: …` and a warning telling the operator to stop the run and discard the clone.

The finding's own reasoning is what makes this worth fixing rather than shrugging at, and it is a security argument, not an ergonomic one: on the shipped default this warn line is the **only** trace a real isolation failure leaves, which the `full-tool-access` entry argues makes it part of the mitigation rather than a nicety. A line that fires on almost every task is not that line.

`_capture_local_config` now drops exactly one key pattern. Two things bound the exclusion:

- **What it costs.** Nothing that git can act on. `branch.<name>.vscode-merge-base` is read by an editor and by no part of git — it cannot launch a program, move a ref, bind a hook or redirect a remote — so the report loses no signal by not carrying it. A test pins the neighbour (`branch.<name>.merge`, which git very much does read) as still reported.
- **Where it is not applied.** Only the reporting path. `_untrusted_config_programs`, the gate that actually _refuses_, is filtered to program-launching keys and never saw this one.

The alternative the finding hints at — filtering the report the way the refusal path is filtered, to program-launching keys only — was **rejected**. That would silence a planted `core.hooksPath`, a moved ref's config, a rewritten `pushurl`: everything the fingerprint exists for, in exchange for the same result on this one key.

Two docstring claims were false and are corrected in the same change: the capture called `--local`/`--worktree` "exactly the agent-writable config surface", which holds only where the orchestrator owns the checkout — on the default it is the operator's own working copy, written by them and by their editor too. And [`.agents/rules/git-workflow.md`](../../../.agents/rules/git-workflow.md) said "the fingerprint itself is never dropped, only its consequence"; it now names the one exclusion and why, because an unqualified never is what a later reader would have trusted.

## F23 — the move nobody was told about

This finding has no section in the trial document: it exists as eleven words inside the `analyze-task-run` list ("`finalize` leaves the task-file move uncommitted"). So it was reproduced from the code and from a real repository rather than confirmed, and what came back is one true claim, one consequence the words do not mention, and one guess of mine that the code refuted.

**True, and by contract.** `finalize_task` calls `_relocate_task_file` and makes no commit — its docstring says so ("Runs no pipeline and never commits/pushes/PRs"). With the task file tracked in `tasks/pending/` (the shape a git-distributed task actually has, and the shape the trial ran), `git status` after a finalize shows ` D tasks/pending/task-1.md` plus an untracked `tasks/failed/`. A test pins exactly that.

**The contract is right, so the fix is not "commit it".** The operator may be on `main` — `finalize`'s own cleanup checks out the base branch — and committing there behind their back is worse than a change they can see. What was wrong is that they could not see it: the plan reported status, PR url, cleanup, branch and ledger, the result reported the status, and neither mentioned a file move that had just dirtied their tree. Both surfaces now name it (`file: tasks/pending/task-1.md -> tasks/failed/task-1.md (not committed)`), the destination comes from one pure `lifecycle_destination` so the plan cannot predict a different move than the one that happens, and an `--as abandoned` finalize — which leaves the file in `pending/` — prints nothing.

**My guess, refuted by the code.** I expected the leftover to make the _next_ `finalize` refuse, since `plan_finalize` rejects an unaccounted-dirty tree. It does not: `_unaccounted_dirty_paths` filters through `_is_artifact_path`, and `_excluded_dirs` carries the task lifecycle dir, so a moved task file is expected dirt. Recorded because it is the kind of "obvious" second-order consequence that is worth checking rather than asserting.

**The consequence the eleven words do not mention, deliberately left open.** The leftover _does_ ride into the next task's review diff: `write_current_diff` excludes only the runtime home, while the code commit's `changed_code_paths` also drops the lifecycle dir — and that docstring claimed the two "cover the same set", which was false. This is F24's mechanism on a path the agent may write, and F24's own evidence says what a stray unfixable path costs a reviewer (escalating to a blocking finding by the third task). It is **not** excluded here: nothing else reports an agent editing its own task file, so hiding `tasks/` from review would remove the only surface that would show it. The false claim is corrected in place and the trade-off named there; the exclusion is a decision with a security side, not a cleanup to fold into a nit.

## F14 — the handoff floor built from a declaration instead of from the branch

Reproduced by inverting the trial's own decomposition inside the existing handoff test: subtask 2 declares no dependency at all and subtask 3 declares one of the two subtasks committed before it. Against the previous code subtask 2's prompt carries no `{predecessor_context}` — `_assemble_predecessor_context` returns `None` on the first line for an empty `depends_on` — and subtask 3's brief names subtask 01 only. That is the trial's artifact exactly, and the second half is the worse one: the depless subtask is not under-informed, it is uninformed.

**The floor now comes from what landed.** `_assemble_predecessor_context` reads the store's subtask rows, keeps every one before this subtask that carries a commit, and orders them oldest-first; `depends_on` is no longer consulted for _which_ predecessors exist. The rows come from the same table `_commit_subtask` writes, so "committed" means a verified SHA rather than a position in the manifest — a resumed run that skipped an already-committed subtask still names it, because the row is what is read.

**`depends_on` keeps a job, and it is the smaller one.** The predecessors a subtask declares are marked `(declared dependency)` in the floor heading, so the author's "build on this one" signal survives instead of being flattened into a list. The distinction is worth one string because the two facts differ: a declared predecessor is what the author says this subtask stands on, an undeclared one is what happens to be underneath it. The supervisor's handoff prompt says which is which, so its interpretive brief can weight them.

**Nothing grows in the prompt.** `{predecessor_context}` is a path, not the content — the floor is written to `logs/<task-id>/subtasks/NN-slug.handoff.md` and read by the agent from there — so a five-subtask decomposition's last brief naming four predecessors costs the node prompt nothing. The one place the floor is inlined is the supervisor's own handoff turn, which is a single read-only turn on a warm session.

The three documentation surfaces that described the old selection are corrected with it: `prompt-variables.md` (the "no `depends_on` predecessor" absence condition, now "the subtask is the first one to run"), the `handoff_role_file` paragraph in `guide/flows/README.md`, and the packaged `implementation.md` prompt block, which told the agent the brief covered "the subtask(s) this one depends on" — the sentence an agent reads, and the one that would have made it distrust the extra predecessors as noise.

**What this does not fix.** The finding's last paragraph is a clause on **F3**, and it stays open with it: `worc-deco-task` still describes `depends_on` as pure ordering. The narrowing it invited no longer costs the successor its facts, so the clause is now about emphasis rather than starvation — but an author still cannot tell from the skill that the field is read by anything but the scheduler.

## F13 — the audit record that could not answer the question it exists for

Reproduced from the writer, not from the trial's artifacts: `write_prompt_audit` was handed `model=node.model, reasoning=node.reasoning` by both runners, so the record's `model` and `reasoning` were the flow node's overrides. Any node taking a provider default recorded `None` for both while the same run's `request.json` carried the resolved value — the two disagreeing about one run, which is the whole of the finding. The existing test asserted the old behavior with the comment "the effective reasoning (post-override) is auditable alongside the model" beside a value that was neither effective nor post-override; that comment is what a false surface looks like once it has been believed.

**One value per stage would have been a new false claim.** The finding says "record the resolved value", and the naive reading — resolve the node override against the route's primary provider once — is wrong in exactly the case an operator asks about. The trial's own `review` node fell over from `codex` to `claude` (that is F27), and the Router _clears_ the pinned model on a cross-provider switch so the substitute re-resolves against its own config. A single stage-level "effective model" would have reported `gpt-5.5` for a run that happened on Claude. So the value is recorded **per attempt**, where it is a fact rather than an inference.

**Where it is stamped.** `ProviderAttempt` gains `model` / `reasoning`, and every one of the six construction sites in the Router now goes through one `_attempt_row` factory — the point being that a seventh site cannot be added without them. The Router is the only layer holding both halves (the per-attempt request and that provider's config block), which is why it and not the audit writer resolves this.

**The resolution rule now has one home.** `request.X or config.X` was written out at four sites — both adapters' argv builders, the on-disk request representation, and Claude's representation extras — and the audit would have been a fifth. It is two methods on `ProviderConfig` (`effective_model` / `effective_reasoning`) that all of them call, so what the audit reports and what the argv carries cannot drift apart. One honest gap is documented on the method rather than papered over: Codex maps a configured `max` onto its `xhigh` ceiling, so the _configured_ level is what the audit names and the translated one is in the attempt's recorded `argv`.

**The record's shape.** `model` / `reasoning` are the settled attempt's effective values — the plain names carry what ran, because that is what an operator opening the file is asking — and the override keeps its own `model_configured` / `reasoning_configured` pair, `null` for a node that pins nothing. That is the finding's "both, as `configured` and `effective`", with the effective one under the name that was already being read as effective. The settled attempt is identified by the identity of the result the stage returned, not by matching `provider_used`: a stage can invoke one provider more than once (a session-unavailable retry, a transient retry) and only one of those rows produced the verdict.

**A documentation claim became true rather than being corrected.** `decision-guide.md` already told operators that "the effective post-override provider, model, and reasoning are recorded per node … that is where you confirm an overlay was honored rather than warned and skipped" — a promise the artifact did not keep, and the one surface that would have sent an operator to this record in the first place. It now holds, and it says where an ignored overlay shows up: the difference between the plain names and the `_configured` pair. `config/runtime.md` and the `analyze-task-run` skill (the reader that found F13) name the new keys too.

## F17 — the commit message a task could not reach, and the half of it that was never true

The finding offers two shapes ("either give an agent's summary a way in, or have the task-authoring skills warn that these surfaces are not addressable") and the operator chose a third that is the first one narrowed: **the task file** gets a way in, the **agent** does not, and the skills carry the warning as well.

**One half of the finding was already false.** It names two surfaces — the commit message and the PR description ("task `001`'s 'put the `grep` output in the PR description' criterion") — as equally unreachable. The PR description is reachable: `publish.py` opens the pull request with the committed `summary.md` as its body, and that summary is authored by the supervisor's finalize turn. So a criterion asking for content in the PR body is answerable today, through the summary, and only the commit message had no channel at all. Both authoring skills now say which is which, because "these surfaces are not addressable" would have taught an author to avoid the one that works.

**What the task can now say.** A `commit_type` front-matter key, one of the eleven Conventional-Commits types, defaulting to `feat` — which is what every task produced before it existed, so no task file changes meaning. It reaches all three commits a task lands through the single `task_commit_subject`: the code commit on the branch, **each subtask commit** of a decomposition (hardcoded `feat` on its own line before this), and the squash commit on the base branch. That last one is F18's remaining half, and it closes with this.

**What the task deliberately cannot say.** The scope stays the task id, always. It is the one thing that makes a subject greppable back to the task that produced it, and an author choosing it would be choosing whether their own work is traceable. And the _body_ stays orchestrator-written: an agent's prose does not enter permanent history through this key. The readable account of the change is the PR body, where it already is.

**Fail-closed, unlike its neighbours.** `priority` folds an unknown value to `mid` (a scheduling hint must never block a task) and `commit_type` does the opposite: an unknown type rejects the task. The value lands on the target's base branch, where a typo that silently deferred to `feat` would be discovered after the merge, by which time no re-run fixes it. The merge path, which runs long after and holds no live task object, reads the type back from the persisted `task.normalized.json` and falls back to the default when that file is gone — a merge subject must never fail to exist because a log directory was cleaned.

**A defect found next door and fixed with it.** `write_normalized` did not persist `trust_level`. A resumed task therefore lost its per-task `strict` and fell back to the instance default — an approval threshold for the dangerous-diff gate, quietly relaxed by a restart, in the one direction it must never move. It is two lines in the same round-trip this change was already editing, it now has its own test, and it is recorded here rather than left as a silent rider: it is not part of F17, and nothing in the trial found it.

## F12 — the checkpoint that named the stage after the one still running

The finding located the symptom precisely and said so: "the write site is not isolated". It is `engine.py`'s region exit, and the reason it is not a bug there is what decides the shape of the fix.

**The mechanism.** A region run ends when a forward edge leaves the region, and the engine writes and persists `current_node = edge.to` _before_ it notices that the edge left:

```python
self._run_state.current_node = edge.to        # 'documentation'
self._recorder.save_checkpoint(self._run_state)
if self._region is not None and edge.to not in self._region:
    return FlowRunResult(status=Status.DONE, final_node=node.id)
```

So after subtask 2's `review` passes, the checkpoint names `documentation` — the post-region node. That write is **load-bearing for the `pre` region**: after `planning` the same line records the sub_flow entry, which is what makes a resumed run continue past planning instead of re-deciding the split (`in_pre` is computed from exactly this value). Removing it would have traded an operator-surface defect for a re-planned resume.

**Why the window was a minute wide.** The trial saw the wrong node twice, about a minute apart, and that is not a race — it is the work between two region phases: the subtask commit, the redacted spec publish, and the **supervisor's handoff turn**, which is an LLM call. `active_subtask` is bumped inside that window too, which is why the two halves of the line disagreed: `subtask=3/5` beside `node=documentation`.

**The fix, in the driver rather than the engine.** `_fan_out_subtasks` re-points the checkpoint at the region entry before committing a subtask that has a successor. The last subtask deliberately keeps the engine's value — for it the post-region node _is_ what runs next, so the previous behavior was right exactly once out of five. Resume is untouched: the fan-out path re-enters from the committed subtask rows and never reads `current_node` (only the `pre` exit does), and a `rerun --continue` on an interrupted decomposition now names the node it will actually re-enter.

The test probes inside the window — a spy on the subtask commit reads the persisted checkpoint — and asserts `["implementation", "implementation", "documentation"]` across three subtasks. Against the previous code it returns `documentation` three times, which is the trial's screenshot.

## F3 — the asymmetry between a root task and a subtask spec

Re-verified before writing, because part of this finding had already been repaired by something else. The sentence it quotes ("so write it however the step needs") is gone: the F1 fix rewrote that bullet, and it now says the review step reads the spec too and that a binding constraint belongs in the subtask body. What was still missing is the half the finding is actually named for — **why** that is so.

The mechanism holds today. `build_context_footer` renders paths only, the root task arrives as `- task: <path>` under "read them as needed", and no role prompt of the packaged `implementation` flow references `{task_path}` at all (`blog_article`, `blog_article_revise`, `content_chapter` and `deep_research` do — which is why the renderer's behavior reads as normal until you look for it here). So the skill now states the asymmetry where the author is deciding what to put in the root: the shared _why_ goes in the root, every binding _must_ goes in the step it binds, restated even when the root already says it.

**F14's clause landed with it.** `depends_on` was described as pure ordering, and the finding's addendum said it also decides what the successor is told. After F14 that is no longer starvation — the floor names every subtask committed on the branch — so the skill says what is left and only that: the field marks the predecessors the brief labels as declared, and declaring the narrow truth costs the successor emphasis, not facts. Writing "it decides what the next subtask is told" would have been the old, now-false version of the same warning.

## F4 — the third key `install` writes

Verified against both sides: `config_writer.py` writes `strict_isolation: False`, `disable_read_isolation: True` and `allow_git_evidence: True` into the `security` block, and `SecurityConfig.disable_read_isolation` defaults to `True` in code as well — read-isolation is off out of the box, which is the same class of default-unsafe the step exists to name. The step said "two of them" and listed two.

The bullet does not merely add the key: it names the trap the finding is about. Because the effective state is `disable_read_isolation OR NOT strict_isolation`, an operator who follows this skill's advice to harden `strict_isolation` and leaves the other key as installed **still runs with read-isolation off**, and no surface says so. That is a two-line edit presented as one, so the skill now says to make both in the same change.

## F5 + F8 + F16 — three lessons with no artifact left to repair

All three are task-authoring defects in task files that ran months of work ago, so there is nothing to fix and something to transfer. They are now rules in `worc-task`, beside the acceptance-criteria guidance where an author is in the middle of the mistake:

- **F8 — cite a rule, do not restate it.** A paraphrase replaces the rule: the agent complies with the paraphrase and the reviewer, which reads the rule itself, files the violation. The rule extends to spec files a task depends on — name every one with its path, which is **F5**'s first half (one named, one left to inference, and the inferred one may never be opened).
- **F16 — a property is not a path list.** "Leave full-bleed screens alone — `onboarding/`, `auth/`" is read as a definition, so a non-full-bleed page under those directories is skipped and a full-bleed page elsewhere is changed. State the property; mark paths as examples of it, never as its extent.
- **F5 — make a criterion greppable.** Prefer a command or a named symbol over a description, so the review step and the operator reach the same verdict.

`worc-deco-task` gets a pointer rather than a copy, with the two that bit inside a subtask body named explicitly (F16 was subtask 04's step 3, F8 was task `001`) — a subtask body is authored under the same rules, and duplicating them into a second skill is how two files start disagreeing.
