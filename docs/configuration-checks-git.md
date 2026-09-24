# Configuration: Validation, Checks, and Git

Part of the [configuration reference](configuration.md): the `config.yaml` blocks that decide what is accepted, what is run against it, and how the result is published — `validation`, `checks`, and `git`.

## `validation`

Controls the task input hardening gate. The gate runs before branch creation and before any provider run.

```yaml
validation:
  max_task_bytes: 262144
  max_task_lines: 5000
  max_line_bytes: 8192
  max_control_ratio: 0.01
  quarantine_folder: "./.worc/tasks/rejected"
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `max_task_bytes` | integer | `262144` | Maximum task file size. |
| `max_task_lines` | integer | `5000` | Maximum number of lines. |
| `max_line_bytes` | integer | `8192` | Maximum UTF-8 byte length for one line. |
| `max_control_ratio` | number | `0.01` | Maximum share of disallowed control characters. |
| `quarantine_folder` | string | `"./.worc/tasks/rejected"` | Destination for structurally rejected task files (under the gitignored `.worc/` home, so rejects are never swept into the audit commit). |

**Two things here are deliberately not operator-configurable.** The required front matter (`id`, a non-blank `title`, and a non-empty `Description` section) and the fail-closed deny on an unrecognized front-matter key are hard-coded in the task gate, checked against the canonical `ALLOWED_TASK_KEYS` set. The `required_fields` and `reject_unknown_fields` keys that once pretended otherwise were parsed, stored, and written by `install`, yet read by nothing — they were **removed in `schema_version` 35**, because a config able to drop `id` from the required set, or to open the unknown-key gate, would weaken invariants the branch names, run directories, and state store all depend on. Both are tolerated (ignored) on load, since every config `install` ever wrote carries them; `upgrade-config` strips them.

Current task front matter fields are:

```text
id, title, task_type, branch_name, branch_mode, branch_ref, publish, trust_level, commit_type, auto_merge, prompt_audit, decomposition, contacts, references, depends_on, priority, queue, subtasks, nodes
```

A task is deliberately "clean" (PRE.3): it carries only identity/dispatch fields plus the sanctioned exceptions — the per-node `nodes.<node-id>` block and `auto_merge` (task-wins). The **flow node** still declares the provider/`model`/`reasoning` defaults; a task may overlay them per run via `nodes.<node-id>.{model,reasoning,provider}` (best-effort — an invalid override is warned and skipped at run time, never fatal), but it never patches the graph. `decompose` was removed (the flow decides splitting); refinement-skip is deterministic (completeness classification, no `refined` flag). Inside a `nodes.<node-id>` block the valid sub-keys are `enabled`, `model`, `reasoning`, and `provider`.

`branch_name` is an operational override for the whole task branch name. It is validated as a safe Git branch ref before branch creation and must not equal `repo.base_branch`.

A structurally rejected task is terminal `failed`, gets a `validation_report.json`, and never creates a branch or calls a provider.

## `checks`

Configures the Check Runner — the `testing`-node quality gate — as **operator-authored, diff-selected command sets**. The Check Runner runs each selected command as a bounded external process (argv list, no shell, allowlisted env) and records redacted logs under the task artifact directory. There is no auto-detection: the operator writes the commands. A **launch failure** of a _required_ toolchain (a non-`skip_if_unavailable` set whose binary cannot start) leaves the gate incomplete — the task goes to **manual** (the agent cannot install host toolchains, so a fix loop cannot help), distinct from a quality failure (a launched check that exits non-zero), which enters `fixing`.

```yaml
checks:
  timeout_seconds: 7200 # global per-command default
  command_sets:
    repo:
      paths: [] # no paths ⇒ always runs (on a non-empty diff)
      commands:
        - { name: lint, argv: ["ruff", "check", "."] }
        - { name: types, argv: ["mypy", "src"] }
        - { name: tests, argv: [".venv/bin/python", "-m", "pytest"] }
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `timeout_seconds` | integer | `7200` | Global per-command default timeout. A set's own `timeout_seconds` overrides it for that set's commands. |
| `command_sets` | mapping | `{}` | Named sets of checks. **Empty (`{}`) means no gate** — every task passes the checks node vacuously. `command_sets` is operator-authored; it is never auto-generated. |

> **The single-root "one catch-all set" recommendation is incomplete once a flow produces documents.** A catch-all with no `paths` runs on _any_ non-empty diff — including a Markdown-only one. So the moment the deployment also runs a document-producing flow (a `deep_research`-style flow committing Markdown, or the packaged content/blog flows), that research run pays for the whole code gate; and if the catch-all contains a command that **rewrites** files rather than checking them, the run parks on the green-but-dirtying guard. Either scope the catch-all's `paths` to code, or keep it and add a documents set (e.g. `paths: ["**/*.md"]` running a Markdown format **check**, never a formatter).

Each entry in `command_sets` is `name → set`, where a set is:

| Set field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `paths` | list of globs | `[]` | Which diff paths select this set. **No `paths` ⇒ the set always runs** (whenever the diff is non-empty). Globs support subtree (`backend/**`) and extension-anywhere (`**/*.md`). |
| `timeout_seconds` | integer or null | `null` (inherit) | Per-command timeout for this set; overrides the global `checks.timeout_seconds`. |
| `skip_if_unavailable` | boolean | `false` | When `true`, if the set's toolchain binary is absent on the host the set is **skipped** (recorded loudly, never counted as passed) instead of failing the gate. Default `false` = fail-closed. **Not an escape hatch** — see below. |
| `commands` | list | (required) | The checks in the set. Each is `{name, argv: [...], cwd?}`. |

Each command is:

| Command field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | (required) | Label used in logs and the `check_runs` record. |
| `argv` | list of strings | (required) | The command as an **argv list** (no shell — shell metacharacters are not interpreted). |
| `cwd` | string or null | `null` (clone root) | Repo-relative working directory for the command. Validated against `..`/absolute traversal; it must stay inside the clone. |

### Diff-based selection

The runner runs the **union** of sets whose `paths` glob match the task diff (the working-tree changes), then aggregates:

- A set with **no `paths`** always runs (on a non-empty diff).
- An **empty diff** (nothing changed) selects nothing → the checks node **passes vacuously**.
- A changed path claimed by **no** set runs no set on its account (it does **not** trigger a full run). Cover shared/root files with a no-`paths` catch-all set or by listing them in a set's `paths`.

### Run-all, then aggregate

All selected checks **run** (no fail-fast) so the human sees every failure at once, then the verdict is computed:

- A **required toolchain absent** (a non-`skip_if_unavailable` set whose binary cannot launch) **or** every selected check skipped ⇒ the gate is **incomplete** → the task goes to **manual** even if some checks passed (the agent cannot install host toolchains, so a fix loop cannot help).
- Otherwise any **quality failure** (a launched check that exited non-zero) → `fixing`.
- Otherwise the gate **passes**.

### `skip_if_unavailable` and auto-merge

A set opted into `skip_if_unavailable: true` whose toolchain binary is missing is recorded as **"skipped (toolchain absent)"** in `check_runs` and surfaced to the human — never counted as passed. A partial skip can still pass the checks node, **but any skip blocks `git.auto_merge`**: an incomplete gate is never auto-merged, and the PR is left open for a human.

> **`skip_if_unavailable` is not an escape hatch for a missing toolchain.** It converts a launch failure into a loud skip, nothing more. The gate is fail-closed on an _incomplete_ run, so a set that was the **only** one the diff selected and is then skipped leaves the gate with nothing run — and parks the task at `manual_action_required`, exactly where the launch failure would have. Use the flag only for genuinely optional toolchains alongside others that do run (iOS checks on a Linux host, say). When you actually want a gate not to run, **disable the node per task** — `nodes.<checks-node-id>.enabled: false` — do not skip your way there.

### Single-root and monorepo examples

A single-root repo with one set and no `paths` (it always runs) is the common case — see the example above.

A polyglot monorepo selects per-subtree, plus a Markdown-only linter that runs only when `.md` files change:

```yaml
checks:
  command_sets:
    backend:
      paths: ["backend/**"]
      commands:
        - { name: lint, argv: ["ruff", "check", "."], cwd: "backend" }
        - { name: tests, argv: ["pytest"], cwd: "backend" }
    mobile:
      paths: ["mobile/**"]
      commands:
        - { name: test, argv: ["npm", "test"], cwd: "mobile" }
    docs:
      paths: ["**/*.md"] # extension-anywhere: runs only when Markdown changes
      commands:
        - { name: prose, argv: ["npx", "prettier@3", "--check", "**/*.md"] }
    ios:
      paths: ["ios/**"]
      skip_if_unavailable: true # xcodebuild may be absent off-macOS; skip rather than block
      commands:
        - { name: build, argv: ["xcodebuild", "build"], cwd: "ios" }
```

A stale `discovery` or flat `commands` key from an older config is **tolerated (ignored) on load**; `upgrade-config` strips it (config `schema_version` **15**). See [operations → command-set diagnostics](operations-preflight.md#command-set-diagnostics) for the `preflight`/`status` command-set summary.

## `git`

Controls PR creation, the optional auto-merge bypass, and the audit-trail policy.

```yaml
git:
  create_pull_request: true
  pr_base: "main"
  auto_merge: false # DANGER: merge every published PR without human review
  auto_merge_strategy: squash # merge | squash | rebase
  auto_merge_wait_for_checks: false # true: arm GitHub-native auto-merge (--auto)
  merge_flow: merge # flow `worc merge-task` runs to resolve base-merge conflicts
  footprint:
    audit_commit_message: "chore(worc): audit trail for {task_id}"
    audit_on_branch: task
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `create_pull_request` | boolean | `true` | Whether publishing creates a PR. |
| `pr_base` | string | `"main"` | Base branch for PR creation. Usually matches `repo.base_branch`. |
| `auto_merge` | boolean | `false` | **DANGER:** when `true`, every successfully published PR is merged to `pr_base` — this removes the human review gate. A per-task `auto_merge` wins outright over this default. |
| `auto_merge_strategy` | `merge`, `squash`, `rebase` | `squash` | Strategy passed to `gh pr merge` when a merge fires (also the default `--strategy` for `worc merge-task`). |
| `auto_merge_wait_for_checks` | boolean | `false` | When `true`, arm GitHub-native auto-merge (`gh pr merge --auto`): GitHub merges only after required checks pass. When `false`, merge immediately. (Also the default for `worc merge-task --wait-for-checks`.) |
| `merge_flow` | string | `"merge"` | Name of the flow `worc merge-task` runs to resolve a conflicting base-merge (`<name>.yaml` under `.worc/flows/`; `install` seeds the built-in `merge` flow there). A clean base-merge is mechanical (no flow, no agent); only a conflict launches it. Its nodes are handed the conflict inventory as `{conflicts_path}`, resolve every conflicted path and run the checks, then the orchestrator commits the merge and merges the PR — unless a conflicted path still shows no decision, in which case the merge is refused (see [operations → Merging a reviewed PR](operations-running.md#merging-a-reviewed-pr-prs--merge-task)). |

`create_pull_request: false` skips only `gh pr create`. The successful publishing path still makes the orchestrator-owned commit and pushes the task branch. Use a disposable fork/test remote for a first self-hosting run; a no-push dry-run mode is not implemented.

### When the task branch already exists on the remote

Publishing does not assume the branch on `origin` got there by us, and it never reads an existing branch or an open pull request as evidence of foreign ownership. What the remote holds decides what happens:

| The remote branch… | What publishing does |
| --- | --- |
| **matches our commit** | nothing is sent; the operation is recorded as done |
| **is behind us** | an ordinary push |
| **diverged, and is exactly the commit we recorded pushing** | a lease-guarded force-push replaces our own stale push |
| **diverged from something we never pushed** | those commits are merged in **locally**, the quality gate re-runs over the combination, and only a pass reaches `origin` |

An open pull request on the task head is adopted, retitled, and appended to. A push is refused outright when the destination of `origin` changed during the task. The full behaviour — what a failing gate or a merge conflict does, where the adoption is reported, and how the destination baseline is taken — is in [operations → When the branch on `origin` already holds something](operations-publishing.md#when-the-branch-on-origin-already-holds-something).

Auto-merge is **off by default** and only affects the publish step — the mid-pipeline [dangerous-diff approval](configuration-agents.md#trust_level-approval-policy) still fires, the orchestrator never passes `--admin` or force-pushes, and a blocked merge ends the task `manual_action_required` with the PR left open (never `failed`). Enable it only when protected branches and required CI checks already enforce your quality gate. See [operations → auto-merge](operations-publishing.md#auto-merge-to-the-base-branch-danger-bypasses-human-review) for the full behavior, the per-task override, and the audit record.

### The canonical layout

There is one canonical layout — there are no footprint modes to choose. Everything the orchestrator generates or installs lives under a single gitignored `<repo>/.worc/` home: `config.yaml` (plus the commented `config.example.yaml` reference), `guide/`, `flows/` (editable flow + role-prompt copies), `tools/` (delivered `tool`-node executables), `state.db` (+ `-wal`/`-shm`), `orchestrator.pid`, `logs/` (plan, diffs, stage logs, `summary.json`, validation reports), `memory/`, `security-reports/`, `workspace/`, `runs/`, and the `tasks/rejected` quarantine. A `night-runs/<stamp>/` directory appears only if you run the packaged `worc-night-run` skill: the orchestrator never writes it and no command prunes it (see [operations → Unattended night runs](operations-running.md#unattended-night-runs-the-worc-night-run-skill)). `install` appends two lines — `.worc/` and the sibling `.worc-io/` exchange (the redacted, agent-facing current-task surface, WRI-001) — to the repo's tracked `.gitignore`, and, unless you asked it to track task files, a third **anchored** `/tasks/` line for the lifecycle tree (anchored so a `src/tasks/` package of your own is untouched; see [`paths.tasks_dir`](configuration-runtime.md#paths)).

`security-reports/` and `docs/research/` are only the **default** report bases: a flow-level `report_dir` moves either (never into `.worc/`, `.worc-io/`, `.git/` or the [`paths.tasks_dir`](configuration-runtime.md#paths) tree), and a private base outside `.worc/` is yours to gitignore — see [`report_dir` in the flow-authoring guide](flow-authoring.md#report_dir--moving-the-deliverables-home).

**`runs/` is the one parent of every per-task runtime root:** `control-bundles/` (the frozen control snapshot), `instruction-bundles/` (the canonical task packet + the root repository instruction files under one manifest digest), `exchange-seals/` (the checksum-verified terminal snapshot of the exchange, written at _every_ terminal, success included), and `exchange-quarantine/` (a mutation-flagged exchange kept as tainted evidence). They are grouped rather than scattered beside the operator's own `config.yaml` / `flows/` / `guide/` because they share one property: private state keyed by task id, written by one run, never agent-readable. Grouping also gives the internal read-deny set a single named entry and retention a single root — see [`logging.clean_runs_on_success`](configuration-runtime.md#logging) and `worc runs clean`.

The only things **not** under `.worc/` are the `tasks/` lifecycle dirs (`preparing`/`pending`/`done`/`failed`; the name is [`paths.tasks_dir`](configuration-runtime.md#paths)) at the repo root — gitignored by default, git-tracked if you asked for that at install. Tracked, the moved task file plus its `<id>.summary.md` in `done/` or `failed/` are the committed audit trail, staged **only for that task** (never `git add -- tasks/` wholesale). A finished task's own file that git restores to the queue folder on the return to base is recognised by content and left alone — see [the recovery playbook](operations-diagnostics.md#7-recovery-playbook--manual_action_required).

The code commit stages changes with an explicit scoped pathspec that never includes a path under `.worc/`, `.worc-io/`, the configured tasks dir (it rides the separate audit commit), or a private report directory a flow's `report_dir` placed outside `.worc/`.

### `git.footprint`

The remaining footprint policy is just the audit commit, which happens only while the lifecycle tree is tracked in git. With the tree gitignored and nothing tracked under it (`install`'s default) both keys below are inert — no branch is touched and the run's record lives in `state.db`, `logs/completed.jsonl` and the `<id>.summary.md` on disk. The mixed state (tree ignored, some task files already committed) still commits the tracked half of each move; see [operations → Git footprint](operations-publishing.md#5-git-footprint-and-the-audit-commit).

| Field | Values | Default | Meaning |
| --- | --- | --- | --- |
| `audit_commit_message` | string | `"chore(worc): audit trail for {task_id}"` | Commit message for the orchestrator's task+summary audit commit (`{task_id}` is substituted). |
| `audit_on_branch` | `task`, `sibling` | `task` | Where the audit commit lands: `task` — on the task branch alongside the code; `sibling` — on a separate `<branch>-audit` branch. |

This is not a second code commit: the audit commit is the task's paper trail in Git — which task the branch was working on, which lifecycle folder it ended in, and the handoff in `<id>.summary.md` — so a branch carries a **code commit** with the source changes and an **audit commit** with `tasks/<state>/<id>.md` plus `<id>.summary.md`.
