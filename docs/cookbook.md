# Cookbook

This cookbook shows common ways to use **wastech-orchestrator**. It is written for operators who
run the orchestrator and for developers who want a practical path from "empty workspace" to "task
processed into a Pull Request".

The canonical product contract remains [orchestrator_final_plan.md](orchestrator_final_plan.md).
Where this guide mentions planned v1 behavior, it is labeled explicitly. The CLI surface described
here (`init`, `preflight`, `run`, and `watch`) exists in the current codebase.

## 1. Initialize A Workspace

Install the package in an environment that has Python 3.12+, git, and the provider CLIs you plan to
use (`codex`, `claude`) on `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Create the runtime layout:

```bash
python -m wastech_orchestrator init .
```

This creates `config.yaml`, `tasks/`, `logs/`, `workspace/`, and editable copies of task and prompt
templates under `templates/`. The command is idempotent: a second run skips existing files and never
overwrites `config.yaml`.

Choose a footprint mode at initialization when you already know where artifacts should live:

```bash
python -m wastech_orchestrator init . --git-mode external
python -m wastech_orchestrator init . --git-mode in_repo_exclude
python -m wastech_orchestrator init . --git-mode in_repo_commit
```

Use `--dry-run` to inspect the created/skipped plan without writing files.

## 2. Configure A Target Repository

Edit `config.yaml` and point the `repo` block at the repository the orchestrator should modify:

```yaml
repo:
  url: "git@github.com:OWNER/REPO.git"
  local_path: "./workspace/repo"
  base_branch: "main"
  branch_prefix: "agent"
```

The branch name produced by the Git Manager follows:

```text
agent/<task-id>-<slug>
```

Credentials are configured outside the orchestrator:

- authenticate git and `gh` in the shell environment that runs the orchestrator;
- authenticate Codex through the Codex CLI;
- authenticate Claude Code through the Claude CLI;
- do not put token values in `config.yaml`, task files, or `extra_args`.

For all configuration fields, see [configuration.md](configuration.md).

## 3. Run Preflight

Before processing tasks, run:

```bash
python -m wastech_orchestrator preflight
```

Preflight checks each allowed provider and the configured isolation policy. A healthy run looks like:

```text
claude: OK - claude 1.2.3 available (version=1.2.3, authenticated=True)
codex: OK - codex 0.9.0 available (version=0.9.0, authenticated=True)
isolation: OK (enforced)
preflight: ready
```

If preflight fails, fix the reported environment or configuration problem before running tasks. Do
not work around failures by weakening sandbox or approval settings; the validator rejects known
unsafe `extra_args`.

## 4. Create A First Task

Create `tasks/pending/task-001.md`:

```markdown
---
id: task-001
title: "Add login form validation"
refined: false
decompose: false
---

## Description

Add client-side validation to the login form. Show a validation error when the email is missing,
when the email format is invalid, or when the password is empty.

## Acceptance criteria

- [ ] Empty email shows a validation error.
- [ ] Invalid email format shows a validation error.
- [ ] Empty password shows a validation error.
- [ ] Existing login behavior still works for valid input.

## Constraints

- Do not touch billing code.
- Add or update focused tests for the login form.
```

The validation gate requires front matter, a valid `id`, a non-empty `title`, and a non-empty
Description section. Acceptance criteria are not a structural reject, but in the current
implementation they make the task complete enough to skip autonomous refinement when
`refined: true` is not set. Constraints are still strongly recommended because they keep the
implementation scope clear.

Task authoring details are in [task-authoring.md](task-authoring.md).

## 5. Run One Task

Process a single task end to end:

```bash
python -m wastech_orchestrator run tasks/pending/task-001.md
```

Useful variants:

```bash
python -m wastech_orchestrator --config ./config.yaml run tasks/pending/task-001.md
python -m wastech_orchestrator --log-level debug run tasks/pending/task-001.md
```

Exit codes:

| Exit | Meaning |
|---:|---|
| `0` | The task reached `done`. |
| `1` | The task reached `failed`. |
| `2` | The task reached `manual_action_required`. |

Planned v1 behavior: a successful task runs through validation, preparation, optional refinement,
planning, implementation, checks, review, fixing if needed, summary, commit, push, PR creation, and
terminal cleanup back to `repo.base_branch`.

## 6. Use `watch`

`watch` resumes an interrupted task first, then processes pending task files:

```bash
python -m wastech_orchestrator watch
```

By default, `orchestrator.auto_mode.enabled` is `false`, so `watch` processes or resumes one task and
then stops:

```yaml
orchestrator:
  auto_mode:
    enabled: false
```

Enable sequential queue processing only when you want the orchestrator to pick the next pending task
after a successful terminal cleanup:

```yaml
orchestrator:
  auto_mode:
    enabled: true
```

Auto mode does not introduce concurrency. Planned v1 behavior keeps a single active task slot and
requires checkout back to `repo.base_branch` before the next task can start.

## 7. Override Providers Per Stage

Global routing lives in `config.yaml` under `agents.routing`. A task may override only the provider
for known agent stages, and only to a provider listed in `agents.allowed`.

```yaml
---
id: task-002
title: "Update API pagination"
agents:
  planning: codex
  implementation: claude
  review: codex
---
```

Allowed stage keys are:

```text
refinement, planning, implementation, review, fixing, summary
```

`testing` and `publishing` are not provider-routed stages. They are executed by the Check Runner and
Git Manager.

Task overrides cannot change provider commands, credentials, sandbox settings, `extra_args`, or any
security policy.

## 8. Configure Checks

Checks are configured, not hardcoded:

```yaml
checks:
  commands:
    - "pytest"
    - "ruff check ."
  timeout_seconds: 1800
```

The Check Runner runs each command with a timeout and records output as artifacts. Planned v1
behavior sends failing check output to the `fixing` stage. Check failures are quality failures, so
they do not trigger provider fallback.

Use the target repository's real quality gate here: unit tests, linting, type checks, or a focused
project command.

## 9. Choose A Git Footprint Mode

The footprint controls where `tasks/` and `logs/` live relative to the target clone.

| Mode | Config | Best for |
|---|---|---|
| `external` | `location: external`, `tracking: none` | Zero artifact footprint in the target repo. |
| `in_repo_exclude` | `location: in_repo`, `tracking: exclude_local` | Artifacts beside the code, excluded through `.git/info/exclude`. |
| `in_repo_commit` | `location: in_repo`, `tracking: commit` | A separate orchestrator-made audit commit. |

Examples:

```yaml
git:
  footprint:
    location: external
    tracking: none
    external_root: "./"
```

```yaml
git:
  footprint:
    location: in_repo
    tracking: exclude_local
```

In every mode, planned v1 behavior uses scoped staging for code changes and excludes
`tasks/`/`logs/`/`workspace/` from the code commit.

## 10. Inspect Logs And Artifacts

Start with:

```bash
ls logs
```

The most useful files are:

```text
logs/
  completed.jsonl
  <task-id>/
    validation_report.json
    task.normalized.json
    task.enriched.md
    plan.md
    current.diff
    review/findings.json
    checks/
    stages/
    summary.md
    failure_report.json
    stuck.md
    publish/
```

Use `completed.jsonl` as the index of terminal tasks. Use `stuck.md` and `failure_report.json` when
the result is `manual_action_required`. Provider stdout/stderr/event files are redacted; the full
process environment and secrets must not be stored.

## 11. Recover From `manual_action_required`

`manual_action_required` means the orchestrator stopped safely and needs a human decision. Common
causes:

| Cause | Action |
|---|---|
| Fix budget exhausted | Read `stuck.md`, inspect the final diff, and decide whether to fix manually or refine the task. |
| Terminal cleanup unsafe | Inspect `repo.local_path` with `git status`, reconcile the branch, and return to `repo.base_branch`. |
| More than one active task on restart | Decide which task is authoritative, then repair state before rerunning. |
| Footprint conflict | Remove or rename tracked `tasks/`/`logs/` paths, or choose another footprint mode. |

After resolving the problem, run:

```bash
python -m wastech_orchestrator watch
```

Planned v1 behavior is idempotent: reruns reconcile state and do not duplicate commit, push, or PR
operations.

## 12. Common Setups

Codex-only:

```yaml
agents:
  allowed:
    - codex
  providers:
    codex:
      command: "codex"
      model: ""
      timeout_seconds: 1800
      sandbox: "workspace-write"
      permission_profile: "workspace-write"
      extra_args: []
```

No automatic PR creation:

```yaml
git:
  create_pull_request: false
  pr_base: "main"
```

Long-running checks:

```yaml
checks:
  commands:
    - "npm test"
    - "npm run lint"
    - "npm run typecheck"
  timeout_seconds: 3600
```

Smaller fix budget for conservative runs:

```yaml
agents:
  max_stage_attempts: 2
  max_fix_cycles: 2
  max_total_fix_iterations: 2
```
