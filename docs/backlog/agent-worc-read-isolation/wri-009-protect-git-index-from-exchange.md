# WRI-009 — Protect Git control state and commits from provider poisoning

**Status:** open **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** WRI-001, WRI-012

## Problem

The Git Manager correctly computes a scoped working-tree pathspec and filters known runtime/task directories from `changed_code_paths`, but `git commit` commits the entire existing index. The current code intentionally accepts already-staged deletions, and the merge path runs `git add -A`; neither path proves that every staged entry belongs to the exact operation. An agent with `Bash` can run `git add -f .worc-io/<task>/...` (or stage `.worc`/task/unrelated files), bypass ignore rules, and let a later orchestrator-owned commit publish it.

The index is not the only control surface. A workspace-write process can modify a normal clone's `.git` directory or a linked worktree's external gitdir/common dir: change HEAD/refs/config, create merge/rebase state, or install/redirect hooks. It can also change `.gitattributes` to select an external clean/smudge/filter-process/textconv/diff driver. A later orchestrator-owned `git add`/diff/commit/push can then execute provider-selected code outside the provider sandbox and with the orchestrator's child environment.

The new exchange increases the impact because it deliberately contains readable plan/diff/findings/check/HITL content. “Gitignored” and “our `git add` pathspec is scoped” are insufficient once an untrusted process shares the index.

## Required outcome

The orchestrator owns Git control state during a task. Provider attempts cannot mutate it undetected, target-repository hooks cannot execute in orchestrator Git subprocesses, and every commit path proves the complete staged set is exactly within that operation's allowlist before invoking `git commit`.

## In scope

- Resolve the worktree gitdir and common dir with Git itself; support both `.git/` directories and linked-worktree `.git` files without parsing them ad hoc.
- Record a parent-held baseline before every workspace-write provider attempt and compare it after WRI-012 proves the provider containment empty, alongside exchange integrity. Cover the index, HEAD symbolic/detached identity, current-task ref value, config, hooks path/content/target, and merge/rebase/cherry-pick/bisect control markers. A change is a non-fallback policy violation; do not automatically destroy operator-owned baseline state.
- Add provider defense in depth: the Codex permission profile grants the resolved gitdir/common dir read-only, never workspace write; Claude denies `Write`/`Edit` on those resolved paths. The same defense covers the entire `tasks/` lifecycle tree: providers read it, never write it, so a node can neither inject new task files for the daemon nor corrupt lifecycle bookkeeping. Controlled Codex execpolicy and Claude disallowed Bash patterns forbid index/branch/history mutation commands (`git add`, `rm`, `mv`, `reset`, `restore --staged`, `update-index`, `apply --index`, `checkout`/`switch` branch changes, `config`, `commit`, and equivalents). The definitive check remains the control-state/staged-set gate because command spelling/wrappers are not a complete boundary for Claude `Bash`.
- Run orchestrator Git commands with a private, controlled empty hooks directory (or an equivalent cross-platform hook-neutral setting) so target-repo `core.hooksPath` and hook files never execute. Force noninteractive/no-pager/no-editor behavior, disable external diff/textconv where supported, and prevent repository-selected commit signing programs.
- Inventory effective attributes/config before any Git operation that can invoke a clean/smudge/filter-process driver. If the repository requires an external filter (for example LFS), support it only through an explicit operator-controlled executable/identity policy and sanitized environment; otherwise stop in manual action before executing it. An agent edit to `.gitattributes` cannot authorize a new process.
- Before code, merge-resolution, audit, and every other commit, enumerate staged entries with NUL-delimited Git output and reject any entry outside the operation-specific allowlist. An ignore rule never makes a staged entry safe.
- Before the audit commit stages a task-lifecycle file, verify its content against the WRI-011 frozen task packet digest; a mismatch is a security violation, never a commit input — the lifecycle paths being on the audit allowlist does not make agent-rewritten content safe.
- Add `.worc-io/` to runtime artifact classification, ignore/exclude installation, and “never tracked/staged” assertions. Check both `git check-ignore` and `git ls-files --cached`/staged state.
- Define baseline behavior for `new`, `existing`, and `current` branch modes. If an operator-owned staged baseline is permitted by an existing mode, preserve/fingerprint it separately and never sweep it into an orchestrator commit; otherwise fail preflight with an actionable clean-index error.
- Store only redacted path-level evidence for a violation; never copy staged secret contents into a report.

## Acceptance criteria

- [ ] `git add -f .worc-io/<task>/plan.md` during a fake Claude/Codex attempt is detected before downstream/publish and cannot reach any commit.
- [ ] Direct file writes or Git commands that change config, HEAD/task ref, hooks, index, or operation-control markers are detected before downstream/publish.
- [ ] Add/delete/rename and intent-to-add index mutations are detected with spaces, quotes, Unicode, and Windows path rendering using `-z` parsing.
- [ ] Code, merge-resolution, audit, and retry/idempotent commit paths reject a foreign staged file even when their own `git add` succeeds.
- [ ] `.worc/`, `.worc-io/`, task lifecycle paths not owned by the current audit operation, and unrelated code paths are absent from the complete staged set at commit time.
- [ ] Codex receives read-only grants for normal and linked-worktree gitdir/common-dir paths; Claude receives matching Write/Edit denies; wrapped-command/direct-file-write tests prove the parent-held gate still catches bypasses.
- [ ] A provider-created file under `tasks/` (self-injected task) and a provider-rewritten lifecycle task file are both blocked: the write is denied by provider policy, and the audit commit refuses a lifecycle file whose digest differs from the frozen task packet.
- [ ] Orchestrator commit/push/fetch paths cannot execute target-repository hooks or an agent-written `core.hooksPath` target.
- [ ] Agent-controlled `.gitattributes`/repo config cannot make orchestrator Git run a new hook, filter, textconv, diff, signing, pager, or editor process; any supported external filter is explicitly operator-authorized and audited.
- [ ] Existing/current branch modes preserve or explicitly refuse operator staged state according to one documented contract; no automatic reset silently discards it.
- [ ] A violation is a security/manual-action result, not provider infrastructure fallback.

## Verification

- Fake-provider mutation matrix across both providers and both permission profiles.
- Git integration tests for force-added ignored exchange/private files, pre-staged foreign additions/deletions/renames, merge `add -A`, audit commit, and publish retry.
- NUL-delimited Unicode/quoted-name tests reuse the centralized Git parser rather than line parsing.
- Windows/macOS/Linux native tests, including `.git` directory/file worktree layouts, external common dirs, hooks-path redirection, malicious attributes/config drivers, index locks, and worktree-specific refs.

## Out of scope

- Replacing scoped staging with blanket reset/clean.
- Treating ignore files as a security boundary.
- General multi-worktree concurrency.

## Likely implementation areas

- src/wastech_orchestrator/git_manager.py
- src/wastech_orchestrator/providers/codex.py and claude.py
- src/wastech_orchestrator/core/flow provider-attempt lifecycle
- tests/test_git_manager.py and tests/providers/
- .agents/rules/git-workflow.md and security.md
- docs/operations.md and packaged guide
