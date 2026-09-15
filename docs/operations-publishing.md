# Operations: Git Footprint and Publishing

Part of the [operations guide](operations.md): what the orchestrator writes to Git, what the PR body says, auto-merge, and the per-task node overrides that change what gets published.

## 5. Git footprint and the audit commit

There is one canonical layout. Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home — `config.yaml` (plus the commented `config.example.yaml` reference), `guide/` (task docs + config helper + flow-authoring docs + copy-ready skills + `footprint.md`), `flows/` (editable flow + role-prompt copies), `tools/` (delivered `tool`-node executables, e.g. `check_chapter`), `state.db` (+ `-wal`/`-shm`), `orchestrator.pid` (the watch daemon) and `orchestrator.run` (a `worc run`), `logs/` (plan, diffs, stage logs, `summary.json`, validation reports, the ledger, the daemon logs), `memory/`, `security-reports/` (the default home of a `private_control_workspace_report` flow's deliverable — `docs/research/` is the committed counterpart for `repository_document`; both are defaults a flow may move with a flow-level `report_dir: <base>`, the engine still appending `/<task-id>`; a private report pointed at a base outside `.worc/` must be gitignored by you — the publish node refuses a git-trackable private report and parks the task, and the resolved private directory is excluded from code-commit staging for the whole run), `workspace/`, `runs/` (the per-task frozen bundles, sealed exchanges, and quarantine — see [`runs clean`](operations-logs.md#reclaiming-per-task-runtime-state-runs-clean)), and the `tasks/rejected` quarantine. The one sibling runtime root outside `.worc/` is the gitignored `<repo>/.worc-io/` exchange (the redacted, agent-facing current-task surface — WRI-001). The `tasks/` lifecycle dirs (`preparing`/`pending`/`done`/`failed`; the parent is `paths.tasks_dir`) live at the **repo root**, outside `.worc/`, and are **gitignored by default** too: `install` appends three lines to the repo's tracked `.gitignore` — `.worc/`, `.worc-io/`, and an anchored `/tasks/` (anchored so a `src/tasks/` package of your own is not swept in). Say yes to the wizard's `Track task files in git?` question (default no; `--track-tasks` / `--no-track-tasks` answer it without prompting) and the tree stays trackable: the task file and its `<id>.summary.md` (in `done/` or `failed/`) then ride an audit commit as the audit trail. Nothing records the answer beyond the line itself, and `install` never seeds it over a directory git already tracks something in; `install --tasks-dir DIR` scaffolds, configures and ignores another directory from that one value. See [How-To → Track your task files in git](how-to.md#6-track-your-task-files-tasks-in-git). (`preparing/` is the staging area the watch scanner never reads — `promote` moves a finished file into `pending/`.)

For the complete table of what each part is, how large it gets, and which retention setting bounds it, read the shipped page `.worc/guide/footprint.md` — it owns that inventory so this guide does not re-derive it.

The audit commit exists only when the lifecycle tree is tracked in git. With the tree ignored and nothing tracked under it — what `install` seeds by default — `commit_audit` returns before touching any branch (no empty `<branch>-audit` sibling is created), and the whole `git.footprint` block is inert. Two fields under `git.footprint` shape the audit commit:

| Key | Default | Effect |
| --- | --- | --- |
| `audit_commit_message` | (string) | The commit message the orchestrator uses for the audit commit. |
| `audit_on_branch` | `task` | `task` commits the audit onto the task branch; `sibling` commits it onto a `<branch>-audit` branch. |

The orchestrator (never an agent) makes the audit commit, staging **only the current task's own files** — `tasks/<state>/<id>.md` plus `<id>.summary.md` — never `git add -- tasks/` wholesale. The pathspec names those two files in **every** lifecycle folder (`preparing`, `pending`, `done`, `failed` — one set of state names, shared with `install`'s scaffolding and the terminal move), so the file's _appearance_ in `done/` or `failed/` and its _removal_ from wherever it was committed — `pending/`, or `preparing/` if you committed a batch there for review — are staged together, and the base working tree is left without a dangling `D`. In the **mixed state** — tree ignored, but some task files already committed — the tracked half of a lifecycle move is still committed, force-added (git refuses an ignored pathspec even for a path it tracks); the probe is `git check-ignore --no-index` against a path under the tree, so one committed file does not flip the answer for the whole tree. The _code_ commit is likewise **scoped** via an explicit pathspec that excludes `.worc/` (gitignored) and `tasks/` (rides the audit commit) — there is never a `git add .`.

**The review diff excludes the runtime roots too.** `.worc/` is ignored by _contents_ (`.worc/*`) precisely so an installed repo can re-include `!.worc/flows/`, `!.worc/tools/` and `!.worc/config.yaml` and keep them reviewable in history — so a tracked file under the runtime home is normal, and an operator editing one between runs used to land in the review diff of every task that followed. No agent may read or write that path, so the finding it drew could not be fixed. Both diff-producing paths now agree with the code commit's own pathspec: the diff a `review` step is handed excludes `.worc/` and `.worc-io/`, and so does the base-merge sweep (per root, and only where that root is not ignored as a whole — `git add` exits 1 on a pathspec naming a fully-ignored root that exists on disk, which would have broken every base merge on a default `install`). Pathspecs are anchored at the repo root, where the same argument stops excluding anything if left relative to the process cwd.

**The subject of every commit a task lands is the orchestrator's.** `<type>(<task-id>): <title>`, with the type coming from the task's [`commit_type`](task-authoring.md#commit_type) front-matter key (default `feat`) — the code commit on the task branch, each subtask commit, and the **squash commit** on the base branch alike. That last one used to be left to the target repository's `squash_merge_commit_message` setting, which took the bare pull-request title and produced a subject with no Conventional-Commits type; `merge-task` and auto-merge now pass `--subject` explicitly, adding the `(#N)` suffix GitHub would have appended. The body is deliberately empty — a body assembled from a branch's own single-line commits could only list internal subjects (that is how `chore(worc): audit trail …` once reached a base branch); the readable account stays on the pull request.

Why keep a separate audit commit:

- it preserves the task's intent beside the code, so the branch history shows both "what changed" and "which task it closed";
- it keeps orchestration metadata out of the code commit, so source history stays clean;
- it leaves a durable Git record even if the local `.worc/logs/` artifacts are later deleted.

Example, with the lifecycle tree tracked:

1. The branch starts with `tasks/pending/task-123.md`.
2. The orchestrator implements the change and creates the normal code commit, for example `feat(task-123): add rate limiting`.
3. At finalize/publish time it moves the task to `tasks/done/task-123.md`, writes `tasks/done/task-123.summary.md`, and creates the audit commit, for example `chore(worc): audit trail for task-123`.

After that, a reviewer can read the code diff in the first commit and the task outcome in the second one. The committed `summary.md` is also the PR body, so the same handoff text is visible in GitHub.

### The PR body, and how a reused chain PR is trimmed

The PR body is the whole-task summary. When the supervisor layer wrote prose, that prose is the body; when it did not — the layer is off, the terminal has no prose by design (`failed` / `manual_action_required`), the synthesis call could not run, or the prose came back **collapsed** below a short floor — the orchestrator's deterministic report is the body instead, with sections `Changes / Steps / Checks / Gates / Technical debt / follow-ups / Pipeline nodes skipped`. Either way a `failed` or parked run gets a real report, and the body never inlines the diff: it names the changed paths and points at `logs/<task-id>/current.diff`. See [the summary artifacts](configuration-flows-supervisor.md#the-summary-artifacts-and-when-the-deterministic-report-takes-over).

When several tasks publish into **one reused PR**, each appends its own section (delimited by a `<!-- worc-task:<id> -->` marker). GitHub rejects a body over 65 536 characters, so the orchestrator bounds it below that by **compacting the oldest sections first, in two passes**:

1. **Prose is elided first**, keeping each section's `## Technical debt / follow-ups`.
2. **Only if the body still exceeds the cap** is that section surrendered too.

Follow-ups are the actionable half of a summary, so they are the last thing given up — a single-pass elision once took ~65 of 98 follow-ups out of a 20-task chain PR. The PR-creating task's head, and every task's marker + `## title`, are always kept, so no task drops off the list. A compacted section leaves a stub pointing at that task's **committed** `<id>.summary.md` — which rides the audit commit and is therefore in the PR's own diff, so a reader on GitHub can open it. A task that committed no summary (a synthetic `run` path) falls back to naming the run host (`.worc/logs/<id>/summary.md`) rather than a repository-looking path that would be a dead link. Nothing is ever lost: the full summaries stay on the run host.

### When the branch on `origin` already holds something

Publishing does not assume the branch on `origin` got there by us, and it never reads an existing branch or an open pull request as evidence of foreign ownership — inferring that does not hold, because a recreated `state.db` has no records at all while the branch and its PR are still yours. What the remote holds decides what happens:

| The remote branch… | What publishing does |
| --- | --- |
| **matches our commit** | nothing is sent; the operation is recorded as done |
| **is behind us** | an ordinary push |
| **diverged, and is exactly the commit we recorded pushing** | a lease-guarded force-push replaces our own stale push, and nothing else |
| **diverged from something we never pushed** | those commits are merged in **locally**, the quality gate runs again over the combination, and only a pass lets anything reach `origin` |

In that fourth case the pull request body says which commits were adopted, and the run records the **adopted** commit rather than reporting a commit it never performed. With `publish: push` or `commit` there is no body, so it is said in the run log and as the ⚠️ trace `done (publish adopted commits it did not make)`. **Failing checks push nothing** — the task parks with the combination on disk and your remote branch untouched. A **merge conflict** parks it as well: resolving one needs an agent and publishing runs after the agent is gone, so the working tree is restored and `worc merge-task` is the way through.

An **open pull request on the task head** is adopted: retitled and appended to like any other. One consequence worth knowing before you share a branch — a pull request **you** opened on the task branch is retitled too.

A push is **refused outright** when the destination of `origin` changed during the task — a rewritten remote URL, `insteadOf` / `pushInsteadOf`, or `pushurl`. The message names the host and path it would have gone to, with credentials stripped. The baseline is captured when the branch is prepared, before any provider ran, and the destination is **re-read immediately before sending** — the one moment an error reaches a real branch. It holds for `worc merge-task` too, which runs later in a different process.

### Auto-merge to the base branch (DANGER: bypasses human review)

By default the orchestrator opens a PR and stops — a human reviews and merges it. **Auto-merge** (opt-in, off by default) makes the orchestrator merge the PR itself, removing the last line of defence against shipping a wrong or malicious agent diff. Enable it **only** when protected branches and required CI status checks are already enforcing the quality gate you need.

Configured under `git:` (all default to the safe value):

| Key | Default | Effect |
| --- | --- | --- |
| `auto_merge` | `false` | When true, every successfully published PR is merged to `pr_base`. |
| `auto_merge_strategy` | `squash` | `merge` \| `squash` \| `rebase` — passed to `gh pr merge`. For `merge`/`squash` the orchestrator also passes an explicit `--subject` (`<commit_type>(<task-id>): <title> (#N)`) and an empty body, so the message never falls through to the target repository's `squash_merge_commit_message` setting; `rebase` writes no commit and takes no message. |
| `auto_merge_wait_for_checks` | `false` | When true, arm GitHub-native auto-merge (`gh pr merge --auto`): GitHub merges only after required checks pass. When false, merge immediately. |

**Per-task override (task wins).** A task file may carry `auto_merge: true` / `false` in its front-matter, and that value **wins outright** over the global `git.auto_merge`:

- `auto_merge: false` **always** opts that task out, even when the global flag is on.
- `auto_merge: true` **always** opts that task in, even when the global flag is off.
- absent → the task follows the global `git.auto_merge`.

Resolution order: per-task `auto_merge` (if set) → global `git.auto_merge` → `false`.

> There is **no** separate `auto_merge_allow_per_task` operator gate (it was removed in `schema_version` 11). Auto-merge skips the human PR review, but the task author and the `config.yaml` owner are the **same trusted operator**, so letting a task set `auto_merge` is a publishing-policy choice, not a weakening of the agent sandbox or approvals ceiling — there is nothing to gate. (Contrast the security ceiling, which a task can never relax.)

**What auto-merge does _not_ weaken:**

- The mid-pipeline **dangerous-diff approval** (code deletions / dependency changes) still fires — auto-merge affects only the publish step, never the agent sandbox or earlier gates.
- An **incomplete checks gate is never auto-merged.** If any selected check was **skipped** (a `skip_if_unavailable` set whose toolchain binary was absent — see [`checks`](configuration-checks-git.md#checks)), the PR is left open for a human even when the checks node passed; only a fully-run, all-pass gate is eligible for auto-merge.
- It never passes `--admin`, never force-pushes, and tries exactly once. If the merge is **blocked** (branch protection, pending checks, conflict) the task ends `manual_action_required` with the PR **left open** for a human — never `failed`, never a forced merge. Re-running the task retries the merge idempotently (it never double-merges an already-merged PR).

**Audit.** Every auto-merge writes a `[AUTO-MERGE]` `WARNING` log line, records the merge in the append-only ledger (`auto_merged` + `merge_outcome` = the merge SHA, `"merged"`, or `"armed"`), and persists a `pr_merge` row in `state.db`. The terminal Telegram notification carries the PR URL. When `auto_merge_wait_for_checks` arms a PR (`merge_outcome: "armed"`), the real merge SHA is captured later only if another task `depends_on` it — the readiness probe backfills the `state.db` `pr_merge` row once it observes the PR merged (see [Task dependencies](operations-running.md#task-dependencies-depends_on)); the ledger row keeps its `"armed"` snapshot.

### Disabling flow nodes (per-task)

By default the pipeline (the packaged `implementation` flow) runs `refinement → planning → implementation → testing → review → fixing(loop) → documentation → publish`. The whole-task **summary** is not a graph node — the [supervisor layer](configuration-flows-supervisor.md#supervisor) writes it at task close (it becomes the PR body), so it cannot be disabled per task; removing that layer is the config switch `supervisor.enabled: false`, after which the deterministic report becomes the body. A **task** can disable a node that adds no value for it — convenient for debugging/testing and quick one-off runs without authoring a separate flow.

Any node present in the task's resolved flow may be disabled, keyed by its **node id**. The ids `planning`, `testing`, `review`, `fixing` are the default `implementation` flow's; a custom flow exposes its own (e.g. `code_review`). `refinement` is skipped **deterministically** when the task is already complete (completeness classification `COMPLETE`), never via a task flag. **Which nodes are safe to disable is the operator's responsibility** — they author the flow and run the tasks; there is no fixed skippable allowlist and no `review`-special-case.

> The global `agents.skip_stages` list was **removed in config `schema_version` 10**, and the `agents.allow_review_skip` gate in **`schema_version` 13** (per-task disable is by flow node id; the operator owns which nodes are safe to disable). With fully configurable flows, "skip a node for every task" is redundant — to drop a node everywhere, remove it from the flow (or author an operator flow). Per-task disable below is the surviving, bounded mechanism.

**Per-task disable.** A task disables a node with `enabled: false` in its `nodes:` block (see [task-authoring.md](task-authoring.md#nodes)):

```yaml
nodes:
  planning: { enabled: false }
  testing: { enabled: false }
```

The validation gate checks shape only; node-id **existence** against the task's resolved flow is checked at flow resolution, before any branch/PR side effect. Naming an id absent from the flow (or a node whose skip cannot route to a forward edge) ends the task `failed` with a controlled message.

**What disabling the default-flow nodes does:** `planning` → a stub `plan.md` and a single implementation unit (no decomposition); `testing` → straight from implementation to review, the Check Runner never runs; `review` → commit with no agent review gate; `fixing` → the test/review fix loop runs as a no-op to its cap, then `manual_action_required` with a `stuck.md` report; `documentation` → the target project's docs are not updated for that task.

Disabling a `checks` node is also the sanctioned way to make a gate not run. Do **not** reach for `skip_if_unavailable` for that: it turns a launch failure into a loud skip, and a set that was the only one the diff selected then leaves the gate with nothing run — which parks the task on the same path the launch failure would have (see [`skip_if_unavailable`](configuration-checks-git.md#skip_if_unavailable-and-auto-merge)).

**Audit.** Every disable persists a `node_runs` row with `skipped = 1` and `skip_reason`, and lists the disabled nodes in a `## Pipeline nodes skipped` section of the PR body.

### Overriding a node's model / reasoning / provider (per-task)

The same `nodes:` block can overlay a node's executor for one run — useful for experiments and one-off runs without authoring a separate flow file per (provider, model, reasoning) combination. Each node's flow declaration supplies the default; the task overlay wins for that run:

```yaml
nodes:
  implementation: { model: claude-opus-5, reasoning: high }
  review: { provider: codex }
```

The overlay is **best-effort** and degrades gracefully (so an unattended `watch` queue is never blocked by a typo): a `provider` must be in `agents.allowed` and a `reasoning` must be supported by the resolved provider — an invalid value (or an overlay on a node that runs no agent, e.g. `testing`/`publish`) is **logged as a warning and skipped**, and the node runs on the flow's declared value. `model` is passed through unchecked. The effective (post-override) model, reasoning, and provider appear in the prompt audit (`logs/<task-id>/prompt-audit/`). See [task-authoring.md](task-authoring.md#provider-model-reasoning).

---
