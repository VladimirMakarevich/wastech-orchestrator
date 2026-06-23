# Operations guide

How to install, authorize, run, and diagnose **wastech-orchestrator** in production. The orchestrator drives coding agents (OpenAI Codex CLI, Anthropic Claude Code CLI) through a deterministic pipeline and publishes the result to a Pull Request. It **owns** git (commit/push/PR); the agents only edit files in a dedicated clone. This guide is for the operator who runs it; the architecture reference is the [Functional Map](functional/index.md) and the security policy is [security.md](../.agents/rules/security.md).

> The orchestrator never installs or authorizes the CLIs and never stores credentials. Authorization for git and for each agent is configured **outside** the orchestrator (see below).

Related guides:

- [cookbook.md](cookbook.md) - practical recipes from workspace setup to recovery.
- [configuration.md](configuration.md) - every `config.yaml` field, default, and validation rule.
- [task-authoring.md](task-authoring.md) - task front matter, examples, and validation behavior.

---

## 1. Installation

Prerequisites: **Python 3.12+**, **git**, the **GitHub CLI** (`gh`) if you want PRs opened automatically, and the agent CLIs you intend to route to (`codex` and/or `claude`) on `PATH`.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell  (source .venv/bin/activate on Unix)
pip install -e ".[dev]"             # or: pip install wastech-orchestrator
```

`install` is the single setup command. It sets up `<repo>/.worc/` in the current repository — a single gitignored home for everything the orchestrator generates: `config.yaml`, a `guide/` (the agent task-authoring docs), `state.db`, `logs/`, and `workspace/`. There is no sibling workspace and no separate clone requirement — the orchestrator branches/commits/pushes in the repo you run it in.

### Bind the repository (`install`)

Install the CLI once, then run `install .` in the repo (the same commands work on Windows and macOS):

```powershell
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
cd C:\projects\my-repo
wastech-orchestrator install .          # interactive wizard
```

The wizard detects the Git root, `origin`, base branch, and cleanliness; finds `codex`/`claude`/`gh`; proposes checks from the repo's ecosystem (`pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod`); writes a validated `config.yaml` into `<repo>/.worc/`; and copies the packaged `worc/` guide to `.worc/guide/`. It appends a single line `.worc/` to the repo's tracked `.gitignore` so the whole runtime home is ignored (`tasks/` is intentionally left tracked). (`install --reconfigure` refreshes the `.worc/guide/` docs to the packaged version.) It never installs or authorizes the CLIs; it reports what is missing and auto-runs `preflight` at the end (a failed preflight keeps the config but exits non-zero with instructions).

Subsequent commands need no `--config` — they walk up from the current directory to the Git root and use `<root>/.worc/config.yaml`, run from anywhere inside the repo:

```text
wastech-orchestrator preflight
wastech-orchestrator watch
wastech-orchestrator status
```

Config discovery order: explicit `--config` > `<repo-root>/.worc/config.yaml` (walk up to the Git root) > a hint to run `install .`. Re-running `install` is idempotent; `--reconfigure` writes a timestamped backup and atomically replaces the config. For automation: `wastech-orchestrator install . --non-interactive --provider codex --no-create-pr` (`--non-interactive` replaces scripted setup). `--no-create-pr` disables the PR but not commit/push.

---

## Upgrading the orchestrator

The orchestrator is a CLI, not a daemon — "updating the implementation" means upgrading the package and restarting any `watch` loop:

```bash
pipx upgrade wastech-orchestrator        # or: pipx install --force "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
wastech-orchestrator --version           # confirm the new version
```

Do it **between tasks**, not mid-run: an in-flight task holds the single processing slot and a live working branch, and its state lives in `state.db`. Wait until `status` shows no active task, then upgrade and re-run `preflight` / `watch`.

The persisted state survives an upgrade — back it up first so you can roll back. That is everything under `<repo>/.worc/` (`config.yaml` and `state.db` live there), plus the git-tracked `tasks/` lifecycle dirs at the repo root. Copy at least `.worc/config.yaml` + `.worc/state.db`. The orchestrator **fail-closes on a backward-incompatible workspace**: if the `config.yaml` `schema_version` or the `state.db` schema is **newer** than the installed version understands, the command prints a clear `error:` and exits non-zero (2) instead of running against a format it cannot read. To recover, upgrade the package to a version that supports it (or, for a throwaway setup, start a fresh workspace via `install --reconfigure`).

The current schema versions are `state.db` **v12** and `config.yaml` `schema_version` **15**, and the two are handled differently because the orchestrator is **greenfield** (no production data to preserve):

- **`config.yaml`** does **not** auto-migrate, and an **older or absent** `schema_version` is accepted as-is (a new release adds keys with safe defaults, so an older config still runs). To materialize the new keys in your file run **`upgrade-config`** (below).
- **`state.db`** does **not** migrate across versions. A brand-new (or pre-versioning) database is created at the current shape; a database stamped an **older** version (`1 ≤ v < 12`) is **refused fail-closed** — several past bumps dropped/renamed tables, and the store only ever adds columns, so an old shape cannot be reshaped in place. Recovery is to delete the local `state.db` and start a fresh workspace (greenfield: there is nothing to preserve). Do this **between tasks**, never with a task in flight.

To bring the rest of your deployment current after a package upgrade, run **`upgrade-config`** (and **`upgrade-docs`**):

```bash
wastech-orchestrator upgrade-config              # uses the discovered/bound config
wastech-orchestrator --config path/to/config.yaml upgrade-config --dry-run   # preview only
```

It adds any keys the current format introduced (from the packaged template's defaults), **keeps every existing value**, stamps the current `schema_version`, and backs up the original to `config.yaml.bak-<UTC>` before writing. It is idempotent (an already-current config is left untouched) and fail-closed (it refuses a config that is unparsable or already newer than this version, and never writes a config that would fail validation). **Caveat:** when it does rewrite the file it re-emits via YAML and drops inline comments — see `config.example.yaml` / [configuration.md](configuration.md) for field docs. `--dry-run` lists what would be added without writing.

The agent task-authoring docs also ship with the package (packaged source dir `worc/`, copied to `.worc/guide/`), so an upgrade brings newer docs than your already-installed copy. Refresh the installed copy (under `.worc/guide/`) with **`upgrade-docs`**:

```bash
wastech-orchestrator upgrade-docs                # uses the discovered/bound config location
wastech-orchestrator upgrade-docs --dry-run      # preview added/updated/removed files only
```

Unlike `config.yaml`, the `worc/` docs are generated content with **no operator edits to preserve**, so this is a straight overwrite to the packaged version: it writes missing or changed files, removes files no longer shipped, and makes no backup. It is idempotent (an already-current copy is a no-op), `--dry-run` writes nothing, and it fails closed (exit 2 with the same hint as `upgrade-config`) when no install location can be resolved.

After a package upgrade, run **`upgrade-config`** and **`upgrade-docs`** to bring your deployment fully current. (A single umbrella `upgrade` that does both is tracked in [follow-ups](backlog/follow_ups.md).) There is no prompt-template delivery step: a flow node's prompt is its `role_file`, shipped with the flow (packaged flows) or kept under `.worc/flows/roles/` (operator flows) — edit the role file to customize a node's prompt.

To install or pin a specific published (pre)release, append its tag to the `pipx`/`pip` source — e.g. `pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@v0.1.1a1"`. Releases are tag-driven and pre-releases (`aN`/`bN`/`rcN` tags) are marked as such on GitHub; maintainers cut them by pushing a `v*` tag, which runs the [release workflow](../.github/workflows/release.yml).

---

## 2. Authorization (configured outside the orchestrator)

The orchestrator passes child processes **only** the allowlisted environment variables (`security.allowed_environment`) and never reads or stores credentials. Set authorization up yourself, once, in the environment the orchestrator runs in:

- **git / GitHub** — configure push access for `repo.url` (SSH key or credential helper) and authenticate `gh` (`gh auth login`) so `gh pr create` works. The orchestrator never embeds tokens.
- **Codex** — sign in with the Codex CLI as usual (e.g. `codex login`); its config lives under `CODEX_HOME`, which is on the default allowlist.
- **Claude Code** — sign in with the Claude CLI (e.g. `claude login` or an API key in its own config); its config dir `CLAUDE_CONFIG_DIR` is on the default allowlist.

Install only the providers you intend to route to. When Claude Code is unavailable, remove it from `agents.allowed` and route every agent-driven stage to Codex. GitHub CLI is required only when `git.create_pull_request: true`; disabling PR creation does not disable commit or push. When PR creation is enabled, `run`, `watch`, and `rerun` **pre-flight `gh` at startup** and exit `2` with an actionable message if it is not on `PATH`, rather than failing later inside the publish stage. On top of that hard gate there is a **non-blocking auth advisory**: if `gh` is present but not logged in, startup logs a `WARNING` ("gh present but not logged in — run `gh auth login`") and continues — it never blocks the run (a valid `GH_TOKEN`/`GITHUB_TOKEN` in the environment, or a transient probe failure, is honored, and the real `gh pr create` failure still degrades to `manual_action_required` safely). The advisory emits a fixed message only — never the raw `gh auth status` output, which would carry the account login and token scopes.

**Where the values live.** The orchestrator reads secrets from its **own** process environment. Provide them either by `export`ing them in the shell/service that launches `worc`, or by putting them in `<repo>/.worc/.env`, which the orchestrator auto-loads at startup (an exported variable always wins over the file). `.worc/` is gitignored so the file is never committed; `install` writes a `.worc/.env.example` template to copy. Point at a file elsewhere with the global `--env-file PATH` (a missing explicit `--env-file` fails closed with exit 2; a missing auto-discovered `.worc/.env` is a silent no-op). Loading `.env` only populates the orchestrator's own environment — it does **not** widen what child processes receive.

If a credential must reach a child process, add **only its variable name** to `security.allowed_environment`. Never place a secret value in `config.yaml`, a task file, or `extra_args`.

---

## 3. Preflight (both CLIs)

Before processing tasks, verify the environment with the read-only diagnostics command. It runs each allowed provider's `preflight()` (`<cli> --version` — no task is processed) and the deterministic `strict_isolation` policy check, then prints a secret-free verdict:

```bash
python -m wastech_orchestrator preflight
```

```text
claude: OK — claude 1.2.3 available (version=1.2.3, authenticated=True)
codex: OK — codex 0.9.0 available (version=0.9.0, authenticated=True)
isolation: OK (enforced)
checks: 2 command sets configured
  - repo: 3 commands (always runs)
  - ios: 1 command (paths: ios/**, skip_if_unavailable)
preflight: ready
```

- Exit `0` when every allowed provider is healthy and the required isolation can be enabled; non-zero otherwise. The command-set summary is informational — an empty `command_sets` (`checks: no command sets configured (no gate)`) is valid and does not fail preflight.
- A `FAIL` line names the problem without leaking secrets (e.g. `codex executable not found`).
- `isolation: OK (enforced)` — every provider in `agents.allowed` passed the offline isolation check (Codex sandbox, Claude permission mode) **and** `security.strict_isolation: true` (the default) is active, meaning a failed check would abort the run rather than silently downgrade isolation.
- `isolation: OK (strict_isolation=false)` — the check still passed, but the operator has set `security.strict_isolation: false`, opting in to full-access provider modes (e.g. `danger-full-access` or `bypassPermissions`). The operator owns the risk; the gate will not abort a run if isolation cannot be enforced.
- `isolation: FAIL` lists the offending provider/setting. With `security.strict_isolation: true` (the default) a run would **fail preflight** before any branch is created rather than silently downgrading isolation — fix the config (don't weaken the sandbox/permission profile) and re-run.

### Command-set diagnostics

`preflight` and `status` print a static summary of the configured `checks.command_sets` — the operator-authored gate. There is no resolution, probing, caching, or readiness verdict: the commands are exactly what the operator wrote (see [configuration.md](configuration.md#checks)), and the orchestrator does not auto-detect them. Each line shows a set's command count, its selecting `paths` (or "always runs" when it has none), and a `skip_if_unavailable` marker:

```text
checks: 2 command sets configured
  - repo: 3 commands (always runs)
  - ios: 1 command (paths: ios/**, skip_if_unavailable)
```

- `status` prints the same read-only summary (it never runs anything). An empty `command_sets` shows `checks: no command sets configured (no gate)` — a valid configuration in which every task passes the checks node.
- At task time the runner runs the **union** of the sets whose `paths` match the task diff (a set with no `paths` always runs; an empty diff runs nothing; a changed path claimed by no set is the fail-safe that runs **all** sets). All selected checks run and the verdict is aggregated: a **required toolchain absent** (a non-`skip_if_unavailable` set whose binary cannot launch) or every check skipped leaves the gate **incomplete** → the task goes to **manual** (the agent cannot install host toolchains); otherwise a quality failure → `fixing`, else pass. A skipped `skip_if_unavailable` set is recorded loudly in `check_runs` and **blocks `git.auto_merge`** even when the node passes.

### Verify the executable seen by the runtime

Check versions from the same shell and environment that will launch the orchestrator:

```bash
command -v codex
codex --version
codex exec --help
command -v claude
command -v gh
```

On Windows use `where <command>`. WSL, PowerShell, a global npm install, and the Codex IDE extension may expose different binaries. Different reported versions therefore do not necessarily indicate a broken installation; they usually mean different `PATH` resolution. Set a specific provider `command` only to an executable that can run inside the orchestrator's OS environment.

---

## 4. Running

```bash
python -m wastech_orchestrator run tasks/pending/task-001.md   # one task, end to end
python -m wastech_orchestrator watch                           # resume + process pending tasks
python -m wastech_orchestrator --log-level debug run <task>    # more verbose operator trace
python -m wastech_orchestrator status                          # active/latest persisted task
```

`watch` respects `orchestrator.auto_mode.enabled`: off (default) it processes/resumes one task and returns the working copy to `repo.base_branch`; on, it processes pending tasks sequentially, checking out the base branch between them. A `manual_action_required` outcome always blocks automatic continuation. Exit code: `0` done, `1` failed, `2` manual_action_required.

### Re-attempting a terminal task (`rerun`)

A task that ended `failed` or `manual_action_required` is terminal — `watch`/`resume` never pick it up again. `rerun` re-attempts it without hand-editing `state.db`, the ledger, or git. It needs an **idle slot** (no other active task) and the **watch daemon stopped**, since it drives the pipeline in the shared clone; it records a new ledger entry linked to the prior attempt (`attempt`, `rerun_of`).

```bash
worc rerun task-001 --dry-run        # show the planned reconciliation; write nothing
worc rerun task-001 --yes            # fresh attempt from the current base_branch
worc rerun task-001 --continue --yes # fix-and-continue: reuse the branch, re-enter at the failed stage
```

- **Fresh** (default) — for a quality failure, a clean redo, or when `base_branch` has moved on: the agent branch is reset to the current base (the stale local branch is deleted and recreated), the per-attempt state is cleared, and the prior attempt's `logs/<id>/` is archived to `logs/<id>/attempt-<N>/`. If a prior **remote branch or open PR** exists it refuses and points you at the `finalize` command; pass `--force-reset-remote` to delete that remote branch (which closes its PR) instead.
- **`--continue`** — for an **infrastructure** failure you fixed by hand (a missing tool, `PATH`, a dropped Telegram approval): it keeps the existing branch and the work already done and re-enters the pipeline at the stage it failed, reusing the same resume engine as crash recovery. Only available when the failed run recorded a recoverable stage.

### Finalize a task you handled by hand (`finalize`)

When you resolve a `failed`/`manual_action_required` task **out-of-band** — merged the PR yourself, fixed it locally, or decided to drop it — `finalize` reconciles the orchestrator's bookkeeping to match. It **only records and tidies**: it never runs the pipeline and never commits/pushes/PRs. Like `rerun` it needs the `watch` daemon stopped (it checks `base_branch` out in the shared clone).

```bash
worc finalize task-001 --as failed --note "superseded by task-014"
worc finalize task-001 --as done --pr-url https://github.com/o/r/pull/42   # records the human merge
worc finalize task-001 --as abandoned --note "obsolete"                    # deliberately dropped
```

It sets the declared terminal status, runs terminal cleanup (back to `base_branch`; **fail-closed** on an unaccounted-dirty tree — it reports, never discards), moves the task file to the matching folder, closes any waiting HITL prompt, and appends a `manual` ledger record. For `--as done` the PR URL is taken from `--pr-url`, else the URL a crashed run already recorded; with neither it still finalizes but **warns and asks for confirmation**. It also runs a read-only `gh pr view` merge check by default (`--no-verify-pr` to skip; warns+asks if the PR isn't merged). `--as abandoned` is recorded as `manual_action_required` with an `outcome: abandoned` ledger marker (distinct from a plain failure). The agent branch is kept unless `--delete-branch`. `--dry-run` previews the reconciliation; re-finalizing an already-finalized task is refused.

By default `watch` is a **long-running loop** (`orchestrator.poll_interval_seconds: 300`, overridable with `--poll-seconds N`): each tick it runs `git fetch` + `pull --ff-only` on `base_branch` then re-scans, so a task committed and pushed to git after `watch` started is picked up without a manual pull (discovery is not limited to the local filesystem). Stop it with Ctrl-C. Set `poll_interval_seconds: 0` (or `--poll-seconds 0`) for a single pass — e.g. when an external scheduler re-invokes `watch`.

### Task dependencies (`depends_on`)

A task can declare other tasks it needs **merged** first via front-matter `depends_on: [<task-id>, …]` (see [task-authoring.md](task-authoring.md#depends_on)). Scheduling is **non-blocking and merge-gated**: under `watch`, a pending task is **eligible** only when every dependency has merged; while a dependency is unmerged the scheduler **skips** the dependent and runs other eligible pending tasks instead, so the single slot never idles on CI. The dependent is re-evaluated each tick (after the `fetch`/`pull`), and once its dependencies merge it branches from a `base_branch` that includes them. "Merged" is probed read-only (`gh pr view → state == MERGED`); a task that committed locally with no PR counts once it is terminal `DONE`. An open or armed PR (e.g. GitHub-native auto-merge still waiting on checks) is **not yet merged**.

**Wait-forever policy + operator intervention.** If a dependency is terminal-but-unmerged — it failed, went `manual_action_required`, or its PR was closed unmerged — the dependent is skipped **every pass, indefinitely**; the orchestrator never auto-fails it (an advisory log line `task <id> waiting: …` records each skip). To unblock, the operator either fixes/re-runs the dependency until it merges, or removes the `depends_on` entry (or the dependent task). A **broken** declaration — a cycle, a self-reference, or a `depends_on` id that matches no known task — is different: it is rejected fail-closed (terminal `failed`, `invalid_depends_on`, quarantined to `tasks/rejected/`), exactly like a malformed task. An explicit `worc run <file>` of a dependent whose dependencies are not merged is **refused** (non-zero exit) rather than skipped, so it never builds on a stale base.

**Merge-SHA backfill.** When the readiness probe observes a dependency's armed PR (`merge_outcome: "armed"`, from GitHub-native auto-merge) now `MERGED`, it backfills the real `mergeCommit.oid` into that task's recorded merge outcome in `state.db`. The append-only ledger (`completed.jsonl`) keeps its point-in-time `"armed"` record — at terminal time the merge genuinely was only armed. An armed PR that **no** task depends on is never probed, so its recorded outcome stays `"armed"`.

### Managing the daemon (`stop` / `restart`)

When `watch` runs as a background service (systemd, launchd, `nohup &`) you do not need to track its PID. A looping `watch` writes `<repo>/.worc/orchestrator.pid` on start and removes it on exit; two commands act on it from any shell in the same repo:

```bash
worc stop                  # SIGTERM the watcher; SIGKILL after --timeout (default 30s); idempotent
worc restart --poll-seconds 10   # stop the running watcher, then start a fresh loop with these flags
```

Shutdown is **graceful**: the SIGTERM is observed between ticks, so an in-flight task finishes its current stage rather than being interrupted mid-run (`--timeout` is the hard backstop). `stop` is idempotent — it prints a notice and exits `0` when nothing is running or the PID file is stale. Starting a second `watch` for the same artifact root is refused while one is already live.

> Every command is also available under the short alias **`worc`** (`worc watch`, `worc status`, …); `wastech-orchestrator` remains the canonical long form used throughout this guide.

### Structured logs

The pipeline emits a secret-free **logfmt** trace on stderr (keyed by `task_id` / `stage` / `attempt` / `provider`): operation start/end/failure and duration, route source, fallback/skip/decompose decisions, fix-loop counters, and the terminal outcome. Secrets are never logged (and a redaction filter scrubs records as a safety net).

```text
ts=… level=info task_id=task-001 stage=implementation primary=codex msg="stage started"
ts=… level=info task_id=task-001 stage=implementation provider=codex attempt=1 elapsed_seconds=30.0 msg="provider heartbeat"
ts=… level=info task_id=task-001 stage=implementation primary=codex duration_seconds=84.2 msg="stage completed"
ts=… level=info task_id=task-001 stage=review msg="falling back" from=codex to=claude error_class=rate_limited
ts=… level=warn task_id=task-001 msg="task stuck" limit=max_total_fix_iterations fix_iterations=30
ts=… level=info task_id=task-001 msg=terminal final_status=done pr_url=… cleanup_safe=True
```

For a durable live trace, enable the rotating file handler and heartbeat:

```bash
python -m wastech_orchestrator \
  --log-file ./logs/orchestrator.jsonl \
  --log-format json \
  --heartbeat-seconds 30 \
  watch
```

These are global CLI options and must come before the subcommand. The file rotates at 10 MB and keeps five backups. Supported formats are `logfmt` and newline-delimited `json`. `--heartbeat-seconds 0` disables heartbeat records.

### Telegram HITL and notifications

Set `telegram.enabled: true`, then export the variables named by `telegram.bot_token_env` and `telegram.chat_id_env`. The values themselves must not be placed in `config.yaml`. Use a dedicated project bot/chat and only one long-poll consumer; webhook mode is incompatible.

After the completed-task ledger record is written, the orchestrator sends one best-effort message for `done`, `failed`, or `manual_action_required`, including the task id, final status, and PR URL when present. A Telegram or network failure is logged with credentials redacted and does not change the already-determined terminal outcome. Task `contacts` are appended as plain-text mentions.

`refinement` and `planning` may emit one typed free-form question or yes/no approval. Questions use ForceReply; approvals use inline buttons. Only the configured chat and exact prompt/callback are accepted. The answer is persisted as redacted JSON and passed to the repeated stage through `human_input_path`, never CLI argv.

After `implementation` and `fixing`, tracked-file deletions and dependency manifest/lock changes require approval before tests unless an exact planning approval already covers the same risk and normalized paths. Ordinary diffs and routine commit/push/PR do not ask.

Timeout, transport failure, ambiguous approval, or a repeated stage request moves the task to `manual_action_required`. Waiting is stored in `logs/<task-id>/hitl/*.json`; restart resumes the existing Telegram message/deadline without adding a state-machine status.

Verify setup:

```bash
wastech-orchestrator --config ./config.yaml preflight
wastech-orchestrator --config ./config.yaml telegram-test --timeout-seconds 60
```

Full BotFather/chat-id setup and troubleshooting: [telegram.md](telegram.md).

Monitor from another terminal:

```bash
tail -f logs/orchestrator.jsonl
python -m wastech_orchestrator --config ./config.yaml status
python -m wastech_orchestrator --config ./config.yaml status task-001
```

`status` opens the configured artifact root's `state.db` read-only. Without an id it reports active tasks, or the latest task when none is active. It does not invoke providers, checks, or Git. The displayed provider is the route's configured primary; it does not claim that a currently running subprocess has already succeeded.

---

## 5. Git footprint and the audit commit

There is one canonical layout. Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home — `config.yaml`, `guide/`, `state.db` (+ `-wal`/`-shm`), `orchestrator.pid`, `logs/` (plan, diffs, stage logs, `summary.json`, validation reports), `workspace/`, and the `tasks/rejected` quarantine. The **only** things not under `.worc/` are the `tasks/` lifecycle dirs (`pending`/`processing`/`done`/`failed`), which live at the **repo root** and are **git-tracked**: the task file and its `<id>.summary.md` (in `done/` or `failed/`) are the audit trail. `install` appends a single line `.worc/` to the repo's tracked `.gitignore`; `tasks/` is intentionally not ignored.

Two fields under `git.footprint` shape the audit commit:

| Key | Default | Effect |
| --- | --- | --- |
| `audit_commit_message` | (string) | The commit message the orchestrator uses for the audit commit. |
| `audit_on_branch` | `task` | `task` commits the audit onto the task branch; `sibling` commits it onto a `<branch>-audit` branch. |

The orchestrator (never an agent) makes the audit commit, staging **only the current task's own files** — `tasks/<state>/<id>.md` plus `<id>.summary.md` — never `git add -- tasks/` wholesale. The _code_ commit is likewise **scoped** via an explicit pathspec that excludes `.worc/` (gitignored) and `tasks/` (rides the audit commit) — there is never a `git add .`.

### Auto-merge to the base branch (DANGER: bypasses human review)

By default the orchestrator opens a PR and stops — a human reviews and merges it. **Auto-merge** (opt-in, off by default) makes the orchestrator merge the PR itself, removing the last line of defence against shipping a wrong or malicious agent diff. Enable it **only** when protected branches and required CI status checks are already enforcing the quality gate you need.

Configured under `git:` (all default to the safe value):

| Key | Default | Effect |
| --- | --- | --- |
| `auto_merge` | `false` | When true, every successfully published PR is merged to `pr_base`. |
| `auto_merge_strategy` | `squash` | `merge` \| `squash` \| `rebase` — passed to `gh pr merge`. |
| `auto_merge_wait_for_checks` | `false` | When true, arm GitHub-native auto-merge (`gh pr merge --auto`): GitHub merges only after required checks pass. When false, merge immediately. |

**Per-task override (task wins).** A task file may carry `auto_merge: true` / `false` in its front-matter, and that value **wins outright** over the global `git.auto_merge`:

- `auto_merge: false` **always** opts that task out, even when the global flag is on.
- `auto_merge: true` **always** opts that task in, even when the global flag is off.
- absent → the task follows the global `git.auto_merge`.

Resolution order: per-task `auto_merge` (if set) → global `git.auto_merge` → `false`.

> There is **no** separate `auto_merge_allow_per_task` operator gate (it was removed in `schema_version` 11). Auto-merge skips the human PR review, but the task author and the `config.yaml` owner are the **same trusted operator**, so letting a task set `auto_merge` is a publishing-policy choice, not a weakening of the agent sandbox or approvals ceiling — there is nothing to gate. (Contrast the security ceiling, which a task can never relax.)

**What auto-merge does _not_ weaken:**

- The mid-pipeline **dangerous-diff approval** (code deletions / dependency changes) still fires — auto-merge affects only the publish step, never the agent sandbox or earlier gates.
- An **incomplete checks gate is never auto-merged.** If any selected check was **skipped** (a `skip_if_unavailable` set whose toolchain binary was absent — see [configuration.md](configuration.md#checks)), the PR is left open for a human even when the checks node passed; only a fully-run, all-pass gate is eligible for auto-merge.
- It never passes `--admin`, never force-pushes, and tries exactly once. If the merge is **blocked** (branch protection, pending checks, conflict) the task ends `manual_action_required` with the PR **left open** for a human — never `failed`, never a forced merge. Re-running the task retries the merge idempotently (it never double-merges an already-merged PR).

**Audit.** Every auto-merge writes a `[AUTO-MERGE]` `WARNING` log line, records the merge in the append-only ledger (`auto_merged` + `merge_outcome` = the merge SHA, `"merged"`, or `"armed"`), and persists a `pr_merge` row in `state.db`. The terminal Telegram notification carries the PR URL. When `auto_merge_wait_for_checks` arms a PR (`merge_outcome: "armed"`), the real merge SHA is captured later only if another task `depends_on` it — the readiness probe backfills the `state.db` `pr_merge` row once it observes the PR merged (see [Task dependencies](#task-dependencies-depends_on)); the ledger row keeps its `"armed"` snapshot.

### Disabling flow nodes (per-task)

By default the pipeline (the packaged `implementation` flow) runs `refinement → planning → implementation → testing → review → fixing(loop) → publish`. The whole-task **summary** is not a graph node — the constant [supervisor layer](configuration.md#supervisor) writes it at task close (it becomes the PR body), so it cannot be disabled. A **task** can disable a node that adds no value for it — convenient for debugging/testing and quick one-off runs without authoring a separate flow.

Any node present in the task's resolved flow may be disabled, keyed by its **node id**. The ids `planning`, `testing`, `review`, `fixing` are the default `implementation` flow's; a custom flow exposes its own (e.g. `code_review`). `refinement` is skipped **deterministically** when the task is already complete (completeness classification `COMPLETE`), never via a task flag. **Which nodes are safe to disable is the operator's responsibility** — they author the flow and run the tasks; there is no fixed skippable allowlist and no `review`-special-case.

> The global `agents.skip_stages` list was **removed in config `schema_version` 10**, and the `agents.allow_review_skip` gate in **`schema_version` 13** (per-task disable is by flow node id; the operator owns which nodes are safe to disable). With fully configurable flows, "skip a node for every task" is redundant — to drop a node everywhere, remove it from the flow (or author an operator flow). Per-task disable below is the surviving, bounded mechanism.

**Per-task disable.** A task disables a node with `enabled: false` in its `nodes:` block (see [task-authoring.md](task-authoring.md#nodes)):

```yaml
nodes:
  planning: { enabled: false }
  testing: { enabled: false }
```

The validation gate checks shape only; node-id **existence** against the task's resolved flow is checked at flow resolution, before any branch/PR side effect. Naming an id absent from the flow (or a node whose skip cannot route to a forward edge) ends the task `failed` with a controlled message.

**What disabling the default-flow nodes does:** `planning` → a stub `plan.md` and a single implementation unit (no decomposition); `testing` → straight from implementation to review, the Check Runner never runs; `review` → commit with no agent review gate; `fixing` → the test/review fix loop runs as a no-op to its cap, then `manual_action_required` with a `stuck.md` report.

**Audit.** Every disable persists a `node_runs` row with `skipped = 1` and `skip_reason`, and lists the disabled nodes in a `## Pipeline nodes skipped` section of the PR body.

---

## 6. Diagnostics — reading what a run produced

All artifacts live under `<repo>/.worc/logs/<task-id>/`. SQLite (`<repo>/.worc/state.db`) is the authoritative state; the artifacts and ledger are the human-facing index.

```text
logs/
  completed.jsonl                 # append-only ledger: one record per terminal task
  <task-id>/
    task.normalized.json          # the parsed task
    validation_report.json        # §19 gate verdict (pass or reject reason)
    task.enriched.md              # refinement output (if it ran)
    plan.md                       # planning output
    current.diff                  # working-tree diff at the last checkpoint (redacted)
    hitl/*.json                   # durable redacted question/approval + recovery handles
    summary.md / summary.json     # the what/how/integration/why handoff → PR body
    failure_report.json / stuck.md# written iff the task ended manual_action_required
    review/findings.json          # review findings (severity → blocking)
    checks/<NNN>.log              # each check command's output (redacted)
    stages/<node-id>/rendered-prompt.md  # the exact node prompt sent (redacted; rendered from the node's role_file)
    stages/<node-id>/run-<node-run-id>/<attempt>-<provider>/
      request.json                # redacted request (argv, no secrets)
      stdout.log / stderr.log     # redacted process output
      events.jsonl                # redacted provider event stream
      result.json                 # normalized AgentRunResult
    publish/{commit,push,pull-request,terminal-cleanup}.json
```

- **The ledger** (`completed.jsonl`): grep here first — id, title, branch, `pr_url`, `final_status`, `fix_iterations`, terminal cleanup status, and a pointer to `failure_report.json` when stuck.
- **Why a task is stuck**: open `stuck.md` (human-readable) / `failure_report.json` (machine). They record which fix loop and which limit was exhausted, all counter values, the last failing check output, the last blocking review findings, and the final diff — plus, for a decomposed task, the failing subtask `k` of `n` and the SHAs already committed.
- **Audit completeness**: SQLite records every `node_runs` and `provider_attempts` row (primary **and** any fallback), each artifact is registered with a **sha256 checksum**, and every commit/push/PR carries an idempotency fingerprint so a restart never double-publishes.
- **Node run vs. attempt**: `run-<node-run-id>` is reserved in SQLite before the provider starts and changes for every repeated node invocation, including each fixing cycle and recovery run. `<attempt>` starts at `1` inside that run and increments only for provider fallback.
- **No secrets anywhere**: `request.json`, the stdout/stderr/events logs, diffs, SQLite rows, the ledger, and the failure report are all redacted; `denied_read_paths` (`.env`, `secrets/**`) are excluded from agent reads and their values are scrubbed from any sink.

- **Rendered node prompts**: `stages/<node-id>/rendered-prompt.md` is the exact (redacted) instruction the agent received for that node — read it first to confirm a `role_file` edit took effect and rendered as intended.

Use the operator log for live monitoring. Provider `stdout.log` and `stderr.log` are finalized and redacted after the subprocess exits, so do not tail them while an attempt is still running.

### Troubleshooting node prompts

- **A `role_file` edit "did nothing"** — confirm you edited the role file the node actually uses (a packaged flow ships its role files beside the flow YAML; an operator flow keeps them under `.worc/flows/roles/`), and compare `rendered-prompt.md` against your file.
- **A `{placeholder}` printed literally** — only the allowlisted variables interpolate (see [configuration.md](configuration.md#prompt-templates-no-longer-a-config-block)); any other `{...}` is intentionally left verbatim so code/JSON braces survive. A path variable with no value for that node renders empty.

---

## 7. Recovery playbook — `manual_action_required`

A task ends in `manual_action_required` (exit `2`) when the orchestrator stops safely and needs a human. It is **not** `failed` (which is for unrecoverable invalid-task/config/security/git errors). The task file is **left in place** (not moved to `tasks/done` or `tasks/failed`) and automatic continuation is blocked until you resolve it.

Common causes and what to do:

| Cause (from `stuck.md` / logs / `cleanup_last_error`) | Action |
| --- | --- |
| **Fix budget exhausted** — `max_fix_cycles` or the global `max_total_fix_iterations` hit. | Read `stuck.md`: the last failing check / blocking findings and the final diff. Fix manually on the task branch, or refine the task, then re-submit. |
| **Terminal cleanup unsafe** — base-branch checkout would lose uncommitted work or the branch state is ambiguous (§8.3). | Inspect the repo (`git status`); reconcile by hand, commit/stash or discard intentionally, return to `base_branch`, then re-run `watch`. |
| **More than one active task on restart** (inconsistent state, §13). | Only one task may be active. Decide which to keep, mark the others resolved, then re-run. |

A §19-**rejected** task is different: it is terminal `failed`, quarantined to `tasks/rejected/` with a `validation_report.json` (and a `validation_reason` in the ledger), and never gets a branch. Fix the task file (e.g. add a Description, a valid `id`, remove injection-shaped front-matter) and re-submit from `tasks/pending/`.

Recovery is idempotent: re-running `watch`/`run` resumes the single in-flight task, reuses the existing branch, continues from its persisted status (`planning`, `testing`, `reviewing`, `fixing`, and so on), and never re-commits/re-pushes a completed operation. A fixing entry also persists `fixing-context.json`, so the resumed agent receives the same failed-check or review-findings path without incrementing the fix counters a second time.

Tracked `tasks/` at the repo root is the **expected**, git-tracked audit trail — that is where live task files belong. The runtime home `.worc/` is gitignored by `install` (a single `.worc/` line appended to `.gitignore`), so its contents never ride a commit.
