# Residues of the read-only git-evidence grant

Status: **proposed** Date: 2026-07-27 Owner: Vladimir Makarevich

Two unclosed gaps left by the read-only git-evidence grant, both consciously deferred on 2026-07-27 during the `deep_research` post-mortem walkthrough. Extracted here because the campaign folder that recorded them is deleted once its items land, and neither gap is closed by anything shipped.

Background: a `read-only` node that declares `git_evidence: true` gets a **shell** on Claude (scoped to read-only git verbs) while the OS sandbox write-denies the whole clone. Both halves are required — the node's declaration and the operator's `security.allow_git_evidence`, off by default. The same 2026-07-27 operator decision that produced these residues also settled that such a node **never parks the task**: a stray working-tree write and Git control-state drift are both reported (console warning + ⚠️ trace) and the run continues.

## A — the orchestrator does not re-check where it is pushing

**Problem.** Because control-state drift on such a node now warns instead of parking, a node that rewrites the clone's push destination gets its rewrite used. `push` runs `git push --set-upstream origin <branch>` and never re-reads what `origin` resolves to, so `url.<attacker>.insteadOf` or `remote.origin.pushurl` planted mid-run sends the branch somewhere else, with the orchestrator's credentials, and everything up to the push looks normal.

**Why the neighbouring guards do not cover it (verified).**

- A planted **hook** cannot execute: every orchestrator git command carries `-c core.hooksPath=<private empty dir>` (`GIT_NULL_HOOKS_DIRNAME` in [`git_manager.py`](../../src/wastech_orchestrator/git_manager.py)), so `.git/hooks/post-commit` never runs in an orchestrator process.
- A **program-launching config driver** still parks the task: `filter.*.clean/smudge/process`, `diff.*.command/textconv`, repo-local `core.sshCommand`/`credential.helper` are refused by `_assert_no_untrusted_filters`, which `commit_code` calls — i.e. after the node ran, so a mid-run plant is caught.
- URL rewrites are **not programs**, so that assertion does not see them. The WRI-009 fingerprint _does_ detect the drift (all repo-local config keys are hashed into it, so the operator warning reads `config: repo config key changed: url.….insteadof`) — but detection is all that is left, and an unattended run has nobody to read it.

**Proposed design — drift, not a ban.** Capture `git remote get-url --push origin` at branch prep (one command; it resolves `insteadOf`/`pushInsteadOf`/`pushurl` to the final URL), re-read it immediately before `push` / `push_branch_update`, and refuse with `ManualActionRequired` when it changed — with the URL redacted, since it can carry a token. A pre-existing operator `pushurl` (pushing to a fork) lands in the baseline and keeps working; only a mid-run rewrite refuses. About twenty lines plus two tests.

Note what this deliberately is **not**: banning the keys, which would break a legitimate operator setup, and would be the "trade functionality away for isolation" that `.agents/rules/security.md` forbids as a first-class rule.

**Why deferred.** The layer beneath still holds — the provider sandbox write-denies the whole clone for this node class, so reaching `.git/config` at all means that enforcement already failed. Do it if a sandbox gap is ever found, or before `deep_research` starts running unattended on a schedule (an unread warning is the whole failure mode).

**Discarded, so it is not re-proposed:** "warn but do not publish" is not a third behavior — any refusal on the publish node _is_ `manual_action_required`, i.e. the same parking one step later.

## B — evaluator nodes with a granted shell produce no warning

**Problem.** The field is accepted on `evaluator` nodes and reaches the provider request, but the before/after comparison that turns a violation into an operator warning lives in the **agent** runner only (`_granted_shell_before` / `_result` in [`nodes/agent.py`](../../src/wastech_orchestrator/core/flow/nodes/agent.py)). So a granted evaluator that wrote to the working tree, or drifted Git control state, is protected by the same sandbox but would report nothing if that sandbox failed.

Originally recorded (P1.4a) as covering only the working-tree half; on 2026-07-27 the same bracket gained the control-state comparison, so **both** signals are now missing for evaluators, not one.

**Proposed design.** Lift the bracket to a place both runners share, or duplicate the three calls in the evaluator runner. The signal fields already exist on `NodeOutcome` (`read_only_write`, `read_only_git_drift`) and the orchestrator's post-node hook already renders both, so this is the capture/compare pair and nothing else.

**Why deferred.** No packaged flow grants an evaluator the verbs, so today it is dead coverage. Worth adding the first time one does — which is a flow-authoring decision, not a code one, so it can arrive without warning.

## Scope / risk

A is a git-manager change on the publish path — the one place a mistake reaches a real branch, so it needs its own test pair and the redaction is not optional. B is a node-runner change with no security consequence of its own (it adds reporting, changes no outcome). Independent; either can ship alone.
