# E2E trial on `wastechlab-mobile-template` — what was fixed

Companion to [e2e-trial-mobile-template.md](e2e-trial-mobile-template.md), which records the findings and does not change as they are repaired. This document is the other half: for each finding taken up, what was reproduced, what landed, and — where reproducing it showed the finding itself to be wrong — what the correction is.

Two rules it follows. **Reproduce before repairing:** every entry below names the probe that showed the defect, and a fix whose test does not fail against the previous code is not recorded as one. **Correct the finding, do not quietly work around it:** most entries here revise a claim the trial made — a wrong diagnosis, a wrong scope, an exposure that turned out not to exist, a prediction the trial itself falsified — and those revisions are the most useful thing in the document, because the trial's own numbers and its ranking were argued from them.

Kept separate from the findings document for a mechanical reason too: that document is already over its `mdlint` `SIZE-001` warn budget (26,000 estimated tokens for `docs/backlog/**`, error at 32,000), and appending a fix record per finding would have pushed it past the error threshold. The budget is a ratchet that is never raised to silence a finding.

## Status at a glance

| Finding | Severity as filed | State | Where |
| --- | --- | --- | --- |
| F1 — the reviewer is blind to the subtask spec | major | **fixed** | `core/flow/nodes/evaluator.py`, `review.md` |
| F2 — "do not flag missing docs" on a docs-only deliverable | minor | **fixed** | `review.md` |
| F24 — `.worc/` in the review diff | minor→major (variance was the finding) | **fixed** | `git_manager.py` |
| F9 — the preamble forbids the reads it hands the node | major | **fixed** | `core/flow/security_preamble.py` |
| F21 — no check verdict on a pass | major | **fixed** | `core/flow/nodes/checks.py`, `review.md` |
| F10 — the contract cannot say "I could not review" | major | **addressed as a warning** (operator's call) | `core/flow/nodes/evaluator.py`, `orchestrator.py`, `review.md` |

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
