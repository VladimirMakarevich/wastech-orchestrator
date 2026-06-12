# Operations guide

How to install, authorize, run, and diagnose **wastech-orchestrator** in production. The orchestrator drives coding agents (OpenAI Codex CLI, Anthropic Claude Code CLI) through a deterministic pipeline and publishes the result to a Pull Request. It **owns** git (commit/push/PR); the agents only edit files in a dedicated clone. This guide is for the operator who runs it; the build spec is [orchestrator_final_plan.md](orchestrator_final_plan.md) and the security
policy is [security.md](rules/security.md).

> The orchestrator never installs or authorizes the CLIs and never stores credentials. Authorization
> for git and for each agent is configured **outside** the orchestrator (see below).

Related guides:

- [cookbook.md](cookbook.md) - practical recipes from workspace setup to recovery.
- [configuration.md](configuration.md) - every `config.yaml` field, default, and validation rule.
- [task-authoring.md](task-authoring.md) - task front matter, examples, and validation behavior.

---

## 1. Installation

Prerequisites: **Python 3.12+**, **git**, the **GitHub CLI** (`gh`) if you want PRs opened
automatically, and the agent CLIs you intend to route to (`codex` and/or `claude`) on `PATH`.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell  (source .venv/bin/activate on Unix)
pip install -e ".[dev]"             # or: pip install wastech-orchestrator
```

Scaffold a project layout (folders + `config.yaml` + editable templates). `init` is idempotent — a
second run skips everything and never overwrites `config.yaml`:

```bash
python -m wastech_orchestrator init .                            # in-repo audit footprint (default)
python -m wastech_orchestrator init . --git-mode in_repo_exclude # artifacts in the clone, git-ignored
python -m wastech_orchestrator init . --git-mode external        # artifacts outside the clone (zero footprint)
```

Then copy/adjust `config.yaml` (it mirrors §11 of the spec) and point `repo.url` /
`repo.local_path` at the target repository clone.

For self-hosting with `init`, the clone in `repo.local_path` must be separate from the checkout used
to run the orchestrator. Keep the known-good control process, SQLite state, tasks, and logs outside
the target clone. This prevents the IDE, the coding agent, and terminal cleanup from competing over
one working tree. (The `install` flow below intentionally binds the checkout you run it in and keeps
only the control plane in a sibling workspace — see its repo-cleanliness check.)

### Bind an existing repository (`install`)

`install` is the two-step flow for a repository you already have checked out. Install the CLI once,
then bind the repo (the same commands work on Windows and macOS):

```powershell
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
cd C:\projects\my-repo
wastech-orchestrator install .          # interactive wizard
```

The wizard detects the Git root, `origin`, base branch, and cleanliness; proposes a sibling control
workspace `<repo-name>-orchestrator`; finds `codex`/`claude`/`gh`; proposes checks from the repo's
ecosystem (`pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod`); and writes a validated
`config.yaml` into the workspace. It **binds the current checkout** as `repo.local_path` (so the
orchestrator branches/commits/pushes there) and keeps `config.yaml`, `tasks/`, `logs/`, and the
SQLite state **only in the sibling workspace** — the installer never touches the target repo's
tracked files. It never installs or authorizes the CLIs; it reports what is missing and auto-runs
`preflight` at the end (a failed preflight keeps the config but exits non-zero with instructions).

The binding (`repo-root -> config.yaml`) is stored in a per-user directory via `platformdirs`
(`%LOCALAPPDATA%` on Windows, `~/Library/Application Support` on macOS, the XDG config dir on Linux;
override with `WASTECH_ORCHESTRATOR_HOME`). Subsequent commands then need no `--config` and no WSL
paths, run from anywhere inside the repo:

```text
wastech-orchestrator preflight
wastech-orchestrator watch
wastech-orchestrator status
```

Config discovery order: explicit `--config` > `./config.yaml` > the current repo's binding > a hint
to run `install .`. Re-running `install` is idempotent; `--reconfigure` writes a timestamped backup
and atomically replaces the config; a workspace bound to another repo is never overwritten. For
automation: `wastech-orchestrator install . --non-interactive --provider codex --no-create-pr`. As
with `init`, `--no-create-pr` disables the PR but not commit/push.

---

## Upgrading the orchestrator

The orchestrator is a CLI, not a daemon — "updating the implementation" means upgrading the package
and restarting any `watch` loop:

```bash
pipx upgrade wastech-orchestrator        # or: pipx install --force "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
wastech-orchestrator --version           # confirm the new version
```

Do it **between tasks**, not mid-run: an in-flight task holds the single processing slot and a live
working branch, and its state lives in `state.db`. Wait until `status` shows no active task, then
upgrade and re-run `preflight` / `watch`.

The persisted state survives an upgrade — back it up first so you can roll back. Under the default
in-repo footprint that is `config.yaml` (in the control workspace) plus `state.db` and `tasks/`/`logs/`
(in the bound repo); under the `external` footprint they all live in the control workspace. Copy at
least `config.yaml` + `state.db`. The orchestrator **fail-closes on a backward-incompatible workspace**: if the
`config.yaml` `schema_version` or the `state.db` schema is **newer** than the installed version
understands, the command prints a clear `error:` and exits non-zero (2) instead of running against a
format it cannot read. To recover, upgrade the package to a version that supports it (or, for a
throwaway setup, start a fresh workspace via `install --reconfigure`). Older or absent versions are
accepted as-is. Per-release changes are listed in [CHANGELOG.md](../CHANGELOG.md).

To install or pin a specific published (pre)release, append its tag to the `pipx`/`pip` source — e.g.
`pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@v0.1.1a1"`. Releases
are tag-driven and pre-releases (`aN`/`bN`/`rcN` tags) are marked as such on GitHub; maintainers cut
them per [RELEASING.md](RELEASING.md).

---

## 2. Authorization (configured outside the orchestrator)

The orchestrator passes child processes **only** the allowlisted environment variables
(`security.allowed_environment`) and never reads or stores credentials. Set authorization up
yourself, once, in the environment the orchestrator runs in:

- **git / GitHub** — configure push access for `repo.url` (SSH key or credential helper) and
  authenticate `gh` (`gh auth login`) so `gh pr create` works. The orchestrator never embeds tokens.
- **Codex** — sign in with the Codex CLI as usual (e.g. `codex login`); its config lives under
  `CODEX_HOME`, which is on the default allowlist.
- **Claude Code** — sign in with the Claude CLI (e.g. `claude login` or an API key in its own
  config); its config dir `CLAUDE_CONFIG_DIR` is on the default allowlist.

Install only the providers you intend to route to. When Claude Code is unavailable, remove it from
`agents.allowed` and route every agent-driven stage to Codex. GitHub CLI is required only when
`git.create_pull_request: true`; disabling PR creation does not disable commit or push.

If a credential must reach a child process, add **only its variable name** to
`security.allowed_environment`. Never place a secret value in `config.yaml`, a task file, or
`extra_args`.

---

## 3. Preflight (both CLIs)

Before processing tasks, verify the environment with the read-only diagnostics command. It runs each
allowed provider's `preflight()` (`<cli> --version` — no task is processed) and the deterministic
`strict_isolation` policy check, then prints a secret-free verdict:

```bash
python -m wastech_orchestrator preflight
```

```text
claude: OK — claude 1.2.3 available (version=1.2.3, authenticated=True)
codex: OK — codex 0.9.0 available (version=0.9.0, authenticated=True)
isolation: OK (enforced)
preflight: ready
```

- Exit `0` when every allowed provider is healthy **and** the required isolation can be enabled;
  non-zero otherwise.
- A `FAIL` line names the problem without leaking secrets (e.g. `codex executable not found`).
- `isolation: FAIL` lists the offending provider/setting. With `security.strict_isolation: true`
  (the default) a run would **fail preflight** before any branch is created rather than silently
  downgrading isolation — fix the config (don't weaken the sandbox/permission profile) and re-run.

### Verify the executable seen by the runtime

Check versions from the same shell and environment that will launch the orchestrator:

```bash
command -v codex
codex --version
codex exec --help
command -v claude
command -v gh
```

On Windows use `where <command>`. WSL, PowerShell, a global npm install, and the Codex IDE extension
may expose different binaries. Different reported versions therefore do not necessarily indicate a
broken installation; they usually mean different `PATH` resolution. Set a specific provider
`command` only to an executable that can run inside the orchestrator's OS environment.

---

## 4. Running

```bash
python -m wastech_orchestrator run tasks/pending/task-001.md   # one task, end to end
python -m wastech_orchestrator watch                           # resume + process pending tasks
python -m wastech_orchestrator --log-level debug run <task>    # more verbose operator trace
python -m wastech_orchestrator status                          # active/latest persisted task
```

`watch` respects `orchestrator.auto_mode.enabled`: off (default) it processes/resumes one task and
returns the working copy to `repo.base_branch`; on, it processes pending tasks sequentially, checking
out the base branch between them. A `manual_action_required` outcome always blocks automatic
continuation. Exit code: `0` done, `1` failed, `2` manual_action_required.

By default `watch` is a **long-running loop** (`orchestrator.poll_interval_seconds: 300`, overridable
with `--poll-seconds N`): each tick it runs `git fetch` + `pull --ff-only` on `base_branch` then
re-scans, so a task committed and pushed to git after `watch` started is picked up without a manual
pull (discovery is not limited to the local filesystem). Stop it with Ctrl-C. Set
`poll_interval_seconds: 0` (or `--poll-seconds 0`) for a single pass — e.g. when an external scheduler
re-invokes `watch`.

### Structured logs

The pipeline emits a secret-free **logfmt** trace on stderr (keyed by `task_id` / `stage` /
`attempt` / `provider`): operation start/end/failure and duration, route source,
fallback/skip/decompose decisions, fix-loop counters, and the terminal outcome. Secrets are never
logged (and a redaction filter scrubs records as a safety net).

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

These are global CLI options and must come before the subcommand. The file rotates at 10 MB and
keeps five backups. Supported formats are `logfmt` and newline-delimited `json`.
`--heartbeat-seconds 0` disables heartbeat records.

### Telegram HITL and notifications

Set `telegram.enabled: true`, then export the variables named by `telegram.bot_token_env` and
`telegram.chat_id_env`. The values themselves must not be placed in `config.yaml`.

After the completed-task ledger record is written, the orchestrator sends one best-effort message
for `done`, `failed`, or `manual_action_required`, including the task id, final status, and PR URL
when present. A Telegram or network failure is logged with credentials redacted and does not change
the terminal outcome.

The notifier also exposes `ask_human` for a free-form question or yes/no approval. It posts the
question and blocks only the current stage until a matching-chat reply arrives or
`telegram.ask_timeout_s` expires. Per-stage agent prompt wiring is not enabled yet; ambiguous or
dangerous stage actions continue to follow the existing deterministic failure/manual-action paths.

Monitor from another terminal:

```bash
tail -f logs/orchestrator.jsonl
python -m wastech_orchestrator --config ./config.yaml status
python -m wastech_orchestrator --config ./config.yaml status task-001
```

`status` opens the configured artifact root's `state.db` read-only. Without an id it reports active
tasks, or the latest task when none is active. It does not invoke providers, checks, or Git. The
displayed provider is the route's configured primary; it does not claim that a currently running
subprocess has already succeeded.

---

## 5. Git footprint modes (§21) — when to use each

Two axes under `git.footprint` control where `tasks/` and `logs/` live relative to the target clone:

| Mode (`--git-mode`) | `location` / `tracking` | Use when |
|---|---|---|
| **in_repo_commit** (default) | `in_repo` / `commit` | You want the **task and its summary stored in git, in the same repo as the code**. The orchestrator (never an agent) makes a separate `tasks/` commit (the task moved to `done/`/`failed/` + `<id>.summary.md`) after the code commit; `logs/` (plan, review, diffs, `summary.json`) stays local, never committed. |
| **in_repo_exclude** | `in_repo` / `exclude_local` | You want artifacts beside the code for convenience but **never committed**. They are appended to `.git/info/exclude` (per-clone, not tracked). |
| **external** | `external` / `none` | You want **zero footprint** in the customer repo. `tasks/` and `logs/` live under `external_root`, outside the clone. Nothing to ignore, nothing committed. |

In every mode the *code* commit is **scoped** (an explicit pathspec that excludes
`tasks/`/`logs/`/`workspace/`, plus — under in-repo — the root runtime files `state.db`/`config.yaml`)
— there is never a `git add .`. The validator rejects the illegal pairings
(`external`+`exclude_local|commit`, `in_repo`+`none`); under `external`, `external_root` must resolve
outside `repo.local_path`.

---

## 6. Diagnostics — reading what a run produced

All artifacts live under `logs/<task-id>/` (`external_root/logs/...` in external mode). SQLite
(`state.db`) is the authoritative state; the artifacts and ledger are the human-facing index.

```text
logs/
  completed.jsonl                 # append-only ledger: one record per terminal task
  <task-id>/
    task.normalized.json          # the parsed task
    validation_report.json        # §19 gate verdict (pass or reject reason)
    task.enriched.md              # refinement output (if it ran)
    plan.md                       # planning output
    current.diff                  # working-tree diff at the last checkpoint (redacted)
    summary.md / summary.json     # the what/how/integration/why handoff → PR body
    failure_report.json / stuck.md# written iff the task ended manual_action_required
    review/findings.json          # review findings (severity → blocking)
    checks/<NNN>.log              # each check command's output (redacted)
    stages/<stage>/run-<stage-run-id>/<attempt>-<provider>/
      request.json                # redacted request (argv, no secrets)
      stdout.log / stderr.log     # redacted process output
      events.jsonl                # redacted provider event stream
      result.json                 # normalized AgentRunResult
    publish/{commit,push,pull-request,terminal-cleanup}.json
```

- **The ledger** (`completed.jsonl`): grep here first — id, title, branch, `pr_url`, `final_status`,
  `fix_iterations`, terminal cleanup status, and a pointer to `failure_report.json` when stuck.
- **Why a task is stuck**: open `stuck.md` (human-readable) / `failure_report.json` (machine). They
  record which fix loop and which limit was exhausted, all counter values, the last failing check
  output, the last blocking review findings, and the final diff — plus, for a decomposed task, the
  failing subtask `k` of `n` and the SHAs already committed.
- **Audit completeness**: SQLite records every `stage_runs` and `provider_attempts` row (primary
  **and** any fallback), each artifact is registered with a **sha256 checksum**, and every
  commit/push/PR carries an idempotency fingerprint so a restart never double-publishes.
- **Stage run vs. attempt**: `run-<stage-run-id>` is reserved in SQLite before the provider starts
  and changes for every repeated stage invocation, including each fixing cycle and recovery run.
  `<attempt>` starts at `1` inside that run and increments only for provider fallback.
- **No secrets anywhere**: `request.json`, the stdout/stderr/events logs, diffs, SQLite rows, the
  ledger, and the failure report are all redacted; `denied_read_paths` (`.env`, `secrets/**`) are
  excluded from agent reads and their values are scrubbed from any sink.

Use the operator log for live monitoring. Provider `stdout.log` and `stderr.log` are finalized and
redacted after the subprocess exits, so do not tail them while an attempt is still running.

---

## 7. Recovery playbook — `manual_action_required`

A task ends in `manual_action_required` (exit `2`) when the orchestrator stops safely and needs a
human. It is **not** `failed` (which is for unrecoverable invalid-task/config/security/git errors).
The task file is **left in place** (not moved to `tasks/done` or `tasks/failed`) and automatic
continuation is blocked until you resolve it.

Common causes and what to do:

| Cause (from `stuck.md` / logs / `cleanup_last_error`) | Action |
|---|---|
| **Fix budget exhausted** — `max_fix_cycles` or the global `max_total_fix_iterations` hit. | Read `stuck.md`: the last failing check / blocking findings and the final diff. Fix manually on the task branch, or refine the task, then re-submit. |
| **Terminal cleanup unsafe** — base-branch checkout would lose uncommitted work or the branch state is ambiguous (§8.3). | Inspect the clone (`git status`); reconcile by hand, commit/stash or discard intentionally, return to `base_branch`, then re-run `watch`. |
| **Repo already tracks `tasks/`/`logs/`** (footprint preflight, §21.4). | Only under `external`/`exclude_local`: remove/rename the colliding tracked paths. Under the default `in_repo_commit` this is expected (those paths are the audit trail) and the preflight is skipped. |
| **More than one active task on restart** (inconsistent state, §13). | Only one task may be active. Decide which to keep, mark the others resolved, then re-run. |

A §19-**rejected** task is different: it is terminal `failed`, quarantined to `tasks/rejected/` with
a `validation_report.json` (and a `validation_reason` in the ledger), and never gets a branch. Fix
the task file (e.g. add a Description, a valid `id`, remove injection-shaped front-matter) and
re-submit from `tasks/pending/`.

Recovery is idempotent: re-running `watch`/`run` resumes the single in-flight task, reuses the
existing branch, continues from its persisted status (`planning`, `testing`, `reviewing`, `fixing`,
and so on), and never re-commits/re-pushes a completed operation. A fixing entry also persists
`fixing-context.json`, so the resumed agent receives the same failed-check or review-findings path
without incrementing the fix counters a second time.

Under `external`/`exclude_local`, a tracked `tasks/` or `logs/` path is rejected by the footprint
preflight (a name collision `.git/info/exclude` cannot untrack); keep task examples under
`docs/examples/` or `templates/`. Under the default `in_repo_commit`, tracked `tasks/`/`logs/` are
the **expected** audit trail — that is where live task files belong, and the preflight is skipped.
