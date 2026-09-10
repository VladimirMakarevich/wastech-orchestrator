# Requirements — Tracker connector

Derived from [problem.md](problem.md). Requirements prefixed `FR-C` describe the connector (its own repository); `FR-W` describe the worc-side contract items that live in this repository. `<tracker>` is any tracker behind an adapter; GitHub is the reference adapter.

## Functional requirements

### Intake

- **FR-C1 (Poll)** — As the operator, I can run the connector against one configured tracker repository so that it lists that repository's open work items every `poll_interval_seconds` (default 300) and also as a single pass (`--once`), the way `worc watch` has a loop and a single-pass mode.
- **FR-C2 (Gate, fail-closed)** — Only a work item that matches an explicit allow rule is ever turned into a task: a configured trigger label and/or a configured author allow-list. A configuration with no rule refuses to start unless it states `gate.allow_all: true` in so many words. An item that loses the trigger label after it was queued is **not** un-queued (the task is worc's now) but is noted in the connector log.
- **FR-C3 (Exactly one task per item, idempotent)** — Each gated item produces exactly one worc task, across connector restarts, crashes between staging and promotion, and repeated polls. A later re-trigger of the same item (reopened, or the trigger label re-applied after a terminal outcome) produces a **new** task whose id carries a sequence suffix; the connector never reuses a task id.
- **FR-C4 (A task worc accepts as-is)** — The generated file passes worc's validation gate unchanged: an `id` matching `^[a-z0-9][a-z0-9._-]{0,63}$`, a non-blank `title` that cannot trip the front-matter injection scan (no leading `-`, no `;`, backtick, `|`, `$(` or newline), a non-empty `## Description`, only keys from worc's allowed set, and a body under worc's size limits (file bytes, line count, bytes per line) — truncated with a visible marker and the item's URL when the source exceeds them. A `branch_name`, when set, is a valid Git ref of at most 50 characters and never equals the base branch.
- **FR-C5 (Provenance)** — The task body opens with a provenance paragraph naming the tracker, the item identifier, its URL and its author, followed by the item's text. Where the item carries no acceptance criteria the body carries none either — enrichment is the `refinement` node's job, not the connector's.
- **FR-C6 (Dispatch fields from configuration)** — The operator configures, once, the dispatch fields every generated task carries: `task_type` (default `implementation`), `queue`, `priority`, `commit_type` (with an optional label→type mapping, e.g. `bug` → `fix`), `auto_merge` (default absent, so worc's `git.auto_merge` decides), and a branch-name prefix. A task field is emitted only when the operator set it; worc's own defaults otherwise stand.
- **FR-C7 (Handoff through the public ingress)** — The connector stages the file in worc's `tasks/preparing/` and promotes it with the `worc promote` command, launched as an argument list. It never writes into `tasks/pending/` directly, never runs `git` in the clone, and never touches the working tree outside `tasks/preparing/`.
- **FR-C8 (Dry run)** — `--dry-run` prints every action the tick would take (items gated, task ids and branch names it would allocate, labels and comments it would post) and writes nothing — no file, no state row, no tracker call with side effects.

### Write-back

- **FR-C9 (State on the item)** — The item reflects where its task is, through a small set of connector-owned labels (or the tracker's equivalent): queued, in progress, PR open, done, failed. Exactly one state label is present at a time; the transition is idempotent (re-applying the current state is a no-op).
- **FR-C10 (Comments)** — The connector posts a comment when the task is queued (naming the task id), when its pull request exists (the PR URL), and when the task ends without a PR (`failed` / `manual_action_required`, or rejected by worc's gate) — the last one names the status and the task id and points the operator at `worc status <id>`; it never pastes logs or diffs into the tracker.
- **FR-C11 (Close on merge)** — When the task's PR is merged the connector closes the item with a closing comment. This is configurable (`write_back.close_on_merge`, default on) so an operator who prefers the tracker's own closing keywords — or wants the issue open for verification — can switch it off.
- **FR-C12 (Status from the sources of truth)** — Task status is read from `worc list --format json`; the PR is discovered from the tracker's code host by the task branch the connector itself named (for GitHub, `gh pr list --head <branch>`). The connector's own state is a cache for idempotency, never the authority — on restart it reconciles from disk (`tasks/preparing/`, `tasks/pending/`), from `worc list`, and from the tracker.
- **FR-C16 (The PR is the owner's to edit)** — After the connector's task has opened a PR, anyone may push further commits to its branch, retitle or edit it, close and reopen it, merge it with any strategy, and delete the branch afterwards; none of this breaks the follow-through. The connector tracks the PR by its number once found, recomputes PR-derived state from the PR's live state on every tick, never pushes, and closes the item on merge regardless of who merged or how.
- **FR-C17 (Follow-up on an open PR)** — When an item is re-triggered while its previous task's PR is still open, the follow-up task continues on the same branch and PR (`branch_mode: existing`, `branch_ref`), which worc's publishing reuses and appends to; a fresh branch is used only once that PR is merged or closed (Q-11, default yes).

### Structure

- **FR-C13 (Tracker-agnostic core)** — The core works only on a normalized `WorkItem` (identifier, title, body, author, labels, state, updated timestamp, URL) and a `TrackerAdapter` interface (list gated items since a watermark, get one item, add/remove label, comment, close, find the pull request for a branch). No core module imports a tracker adapter or knows a tracker's CLI or API.
- **FR-C14 (Adapters are optional installs)** — Adapters ship behind optional dependencies (`pip install "worc-connect[github]"`) and are discovered through a Python entry-point group, so a third-party adapter in another repository is picked up without a change to the core. v1 ships the GitHub adapter only, on the operator's `gh` login.
- **FR-C15 (Triage is optional and off)** — `triage.enabled` defaults to `false`. When `true`, a gated item first becomes a **triage** task (`task_type` naming the connector-shipped flow), and the same task builder then produces the implementation task from the triage report — or a needs-info / duplicate / declined write-back instead of a task. The triage flow and its role prompts are installed into worc's `.worc/flows/` by the connector only when the switch is on; both paths converge on one task builder so the implementation task's shape does not depend on the path.

### worc-side contract (this repository)

- **FR-W1 (`references:` task field)** — A task may carry `references:`, a list of short single-line strings, that worc appends verbatim to the pull-request body it opens, under a fixed heading. Validation is fail-closed at the task gate (list of strings, each non-empty, single-line, bounded in length, passing the same injection scan as every other front-matter value). worc never interprets the strings — a connector uses them for the tracker's own closing keyword (`Fixes #142`, `Closes #142`, `AB#142`) and back-links; worc learns no tracker syntax.
- **FR-W2 (`pr_url` in `worc list --format json`)** — Every JSON entry for a task worc has a row for carries `pr_url` (the URL of the PR it opened, or `null`). The JSON shape is named in the shipped operator guide as the scripting contract.

## Non-functional requirements

- **NFR-1 (Cross-platform)** — the connector behaves identically on Windows, Linux and macOS: `pathlib` throughout, `Path.as_posix()` for any stored or compared path, task files written with `newline=""` and UTF-8, atomic writes via a temp file and `os.replace`, cross-process stop via a sentinel file (no signals), `gh` / `worc` resolved with `shutil.which` (so `.exe` / `.cmd` launchers work on Windows).
- **NFR-2 (Untrusted input)** — item text reaches only the task file. It is never placed in a command argument, an environment variable, a log line (ids and URLs are logged, bodies are not), or the connector's own comments. Comment bodies go to the tracker through a body **file**, never through argv.
- **NFR-3 (No credentials in the connector)** — the connector holds no token: authentication is the operator's `gh auth login` (or the equivalent tool for another tracker). Nothing secret is written to its state, its log, or the task file.
- **NFR-4 (Cannot weaken worc)** — the connector's only write into worc is a task file, and a task file cannot carry `extra_args`, a permission profile, or a flow edit. The connector runs no agent of its own and needs no exception to worc's security envelope.
- **NFR-5 (Fail-closed determinism)** — an unparseable configuration, an absent gate, an unreachable tracker, or an unreadable `worc list` output stops the tick with a logged reason and changes no state; ambiguity never resolves to "create the task anyway".
- **NFR-6 (Cost)** — the polling loop makes **no** model call; a gated item costs exactly one worc task (two when triage is on). A tick with nothing new writes nothing.
- **NFR-7 (Coexistence with the single slot)** — the connector never contends for worc's processing slot, never runs `git`, and tolerates the clone being on a task branch: `tasks/preparing/` is untracked staging and survives worc's checkouts.
- **NFR-8 (Observability)** — every action leaves a log line with the item id, the task id and the outcome; the connector's state rows explain any idempotency decision after the fact; the issue's own comment trail is the operator-visible record.

## Versioned surfaces touched

| Surface | Change |
| --- | --- |
| Task-file front matter (worc) | **FR-W1** adds the optional key `references:`. No `config.yaml` schema bump — the key lives in the task grammar, not in config. |
| `worc list --format json` (worc) | **FR-W2** adds `pr_url` to task entries and names the shape as a contract in the guide. |
| `state.db` (worc) | none — `pr_url` is already recorded by the run (verify where in phase 02; the ledger's `finalized` record carries it). |
| Flow schema (worc) | none. |
| Packaged operator guide (worc) | the task-fields page gains `references:`; the operations page names `worc list --format json` as the scripting surface. |
| Connector configuration | new, connector-owned file; its schema is versioned by the connector, not by worc. |
| Connector state | new, connector-owned SQLite under the connector's home; not a contract. |

## Dependencies & assumptions

- worc is installed in the target clone with `promote` and `list --format json` present (both exist today), and `repo.url` names a hosted GitHub repository so worc's own `gh` calls are pinned.
- `gh` is on `PATH` and logged in for the operator who runs the connector; `gh issue list --json`, `gh issue comment --body-file`, `gh issue edit --add-label/--remove-label`, `gh issue close`, `gh pr list --head --json` are available (GitHub CLI 2.x).
- The connector runs on the **same host and clone** as `worc watch` in v1 (assumption A-1; a remote connector would hand off through a commit to the base branch instead, which worc's watch tick already discovers — deferred, see out-of-scope).
- One connector instance serves one tracker repository and one worc clone (assumption A-2).
- `tasks/preparing/` exists (scaffolded by `worc install`) or is created by the connector under `paths.tasks_dir`.
- The trigger label exists on the repository or the adapter can create it (GitHub: `gh label create`).

## Priority

| Requirement | Priority | Note |
| --- | --- | --- |
| FR-C1, C2, C3, C4, C5, C7, C12, C13 | Must | the v1 core — nothing works without them |
| FR-C9, C10, C11 | Must | the visible half of v1; without write-back the operator cannot see the connector working |
| FR-C16 | Must | the operator's named edge case; costs only tracking the PR by number and recomputing from live state |
| FR-C17 | Could | follows Q-11's default; no worc change needed |
| FR-C6, C8 | Should | C8 (dry run) is the safe first thing to run on a real repository |
| FR-C14 | Should | extras + entry points from day one, but only the GitHub adapter is built |
| FR-W1, FR-W2 | Should | v1.1; independent of the connector and useful to any scripting operator |
| FR-C15 | Could | the switch and the convergence on one builder are v1 design; the flow's content is its own phase |
| NFR-1 … NFR-8 | Must |  |
