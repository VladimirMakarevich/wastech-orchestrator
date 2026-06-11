# Lean orchestrator architecture for coding agents (Codex / Claude) + Git

Date: 2026-06-11 (updated)
Goal: describe the architecture of a console application that runs on Windows/macOS/Linux, watches a task folder, runs a task through a deterministic stage pipeline using external coding agents (Codex CLI and/or Claude Code CLI), and publishes the result to a dedicated Git branch.

The document was reworked after studying [crewAI](https://github.com/crewAIInc/crewAI). Decision: **write our own lean orchestrator** (with no framework dependency), but borrow 5 proven patterns from crewAI. Exactly where is marked with the `[← crewAI]` marker.

---

## 1. The idea in one paragraph

The application does not replace the coding agent and Git. It acts as an **orchestrator**: it watches the task folder, parses the task, updates the repository, creates a dedicated branch, and runs the task through a deterministic stage pipeline (plan → implementation → review → tests → fixes → commit → push). The heavy lifting on the code is done by an **external coding agent** — Codex CLI or Claude Code CLI — behind an abstraction that allows globally enabling/disabling a provider and falling back to the remaining one. On top of the stages sits a thin **supervisor** that plans, routes, and, when needed, asks the human clarifying questions (via Telegram). Git access uses ordinary means (SSH key, GitHub token, `gh auth login`). The ChatGPT/Codex or Claude subscription is used for access to the agent, not as a Git authentication mechanism.

---

## 2. Key architectural principles

1. **A deterministic Flow, not emergent behavior.** The stage pipeline is defined explicitly (like a crewAI Flow); agents do not freely "negotiate" among themselves. Predictability matters more than autonomy.
2. **A thin supervisor on top of the stages** `[← crewAI hierarchical]`. The supervisor plans and routes, but does not replace the coding agent.
3. **The coding agent behind an abstraction.** `Codex CLI` and `Claude Code CLI` are interchangeable implementations of a single interface with fallback.
4. **Checkpoints at every stage** `[← crewAI @persist]`. After each stage, the state is written atomically to SQLite so it can survive a crash and continue from where it stopped.
5. **Guardrails in two layers** `[← crewAI guardrails]`. A ban on actions (sandbox/approval) + output validation (diff) before commit.
6. **Fresh context for each task** `[← crewAI kickoff]`. The supervisor and agent context are reassembled from YAML templates for each new task — no shared state between tasks.
7. **Human-in-the-loop via Telegram** `[← crewAI AskQuestion/human_input]`. Clarifying questions and approval of dangerous actions go to the human and block the stage until there is an answer.

---

## 3. Overall diagram

```text
┌────────────────────┐
│  tasks/pending/    │
│  task-001.md       │
└─────────┬──────────┘
          │ new task
          ▼
┌─────────────────────────────────────────┐
│            ORCHESTRATOR                   │
│  watcher → parser → supervisor            │
└─────────┬─────────────────────────────────┘
          │ recreate supervisor + context per task
          ▼
   ┌──────────────────── Stage Flow ────────────────────┐
   │  git pull/fetch → checkout -b codex/task-001         │
   │  ├─ STAGE plan       (CodingAgent.run, no edits)      │
   │  ├─ STAGE implement  (CodingAgent.run, edits)         │
   │  ├─ STAGE review     (CodingAgent.run, no edits)      │
   │  ├─ STAGE test       (Test Runner)                    │
   │  ├─ STAGE fix        (retry loop, attempt limit)       │
   │  ├─ GUARDRAILS       (action blacklist + diff-checks)  │
   │  ├─ git commit                                        │
   │  └─ git push / gh pr create                           │
   └───────────────────────────────────────────────────────┘
          │            ▲                       │
          │            │ clarifying question /  │ result
          ▼            │ action approval        ▼
   ┌─────────────┐  ┌──┴──────────┐      ┌──────────────┐
   │ State Store │  │  Telegram   │      │ tasks/done/  │
   │  (SQLite,   │  │  (HITL +    │      │ tasks/failed/│
   │ checkpoints)│  │ notifications)│    └──────────────┘
   └─────────────┘  └─────────────┘

   CodingAgent (abstraction) ──┬── CodexCLI   (enable/disable)
                              └── ClaudeCLI  (enable/disable)  ← fallback
```

---

## 4. Core components

### 4.1. File Watcher + Task Parser `(#9)`

Watches the task folder:

```text
tasks/
  pending/    task-001.md
  processing/
  done/
  failed/
```

When a new `.md`/`.json` file appears, it is moved to `processing/` and parsed. The parser extracts structured fields from the task — from the YAML frontmatter and/or from the heading:

```markdown
---
id: task-001
title: "Add login validation"
repo: my-service              # binding to the repo/project (#8)
reasoning: high               # reasoning level (#7)
complexity: medium            # affects model choice and limits
provider: auto                # auto | codex | claude
contacts: ["@team-lead"]      # who to ping on Telegram
commands:                     # additional commands/hints for the agent
  - "do not touch the billing module"
---

## Description
Add validation for the login form ...
```

The parsed fields are substituted into the YAML prompt templates as `{variables}` `[← crewAI inputs]`.

### 4.2. Git Manager `(#8)`

The binding to a specific project/repo is set in `config.yaml` (see §5) and/or in the task's `repo` field. Before the task:

```bash
git fetch origin
git checkout main
git pull origin main
git checkout -b codex/task-001-add-login-validation
```

After:

```bash
git add .
git commit -m "Implement task-001: add login validation"
git push origin codex/task-001-add-login-validation
gh pr create --title "Task 001" --body-file logs/task-001/pr.md --base main --head codex/task-001-add-login-validation
```

Parallel tasks — via `git worktree` (v2), so as not to mix them in one clone.

### 4.3. CodingAgent — provider abstraction + fallback `(#1)`

A key piece that **does not exist in crewAI** (there is no native failover there) — we design it ourselves.

```python
class CodingAgent(Protocol):
    name: str
    enabled: bool                     # global enable/disable (#1)
    def run(self, prompt: str, cwd: str, stage: str,
            reasoning: str, allow_edits: bool) -> Result: ...

class CodexCLI(CodingAgent):  ...     # wrapper over `codex exec`
class ClaudeCLI(CodingAgent): ...     # wrapper over the `claude` CLI

def run_with_fallback(prompt, **kw):
    providers = [p for p in (CodexCLI(), ClaudeCLI()) if p.enabled]
    if not providers:
        raise NoProviderEnabled()
    last_err = None
    for p in providers:               # order = priority; auto → the whole list
        try:
            return p.run(prompt, **kw)
        except ProviderError as e:
            last_err = e               # fallback to the next enabled one
            log.warning(f"{p.name} failed, fallback: {e}")
    raise AllProvidersFailed(last_err)
```

- Globally disable a provider — a flag in `config.yaml` (`providers.codex.enabled: false`).
- In a task you can pin `provider: claude` (no fallback) or `provider: auto` (with fallback).

### 4.4. Supervisor `(#4, #5)` `[← crewAI hierarchical manager]`

A thin layer over the stages. It is responsible for:

- planning: which stages are needed for the task (for example, a simple fix does not need the full chain);
- routing: which provider/model/reasoning at each stage;
- escalation: when to ask the human a clarifying question (the `AskQuestion` contract) or request action approval;
- the go/no-go decision after guardrails and tests.

**Recreation for each task `(#5)`:** the supervisor and agent context are assembled anew from YAML templates for the specific parsed task. State is not shared between tasks — this eliminates "leaked" context. (In crewAI this is `kickoff()` with a fresh Crew; here it is simply constructing an object per task.)

Protection against delegation loops `[← crewAI]`: only the supervisor has the right to escalate/route; the agent's stage calls are "leaf" calls and cannot forward further.

### 4.5. Stage Pipeline (Flow) `[← crewAI Flow]`

The stages are fixed and deterministic:

```python
STAGES = ["plan", "implement", "review", "test", "fix", "guardrails", "commit", "push"]
```

Each stage is a separate function with input from the state and an atomic checkpoint on output (see §6). `fix` runs in a retry loop with an attempt limit.

### 4.6. Guardrails — action blacklist + output validation `(#3)` `[← crewAI guardrails]`

Two layers (in crewAI, guardrails validate only the output — we extend it to actions):

**Layer 1 — action ban (enforce, before execution):**
- run the agent only in `workspace/repo`, sandbox `workspace-write`, approval `on-request`;
- global blacklist of dangerous commands (`rm -rf`, `git push --force`, access to `~`, to secrets);
- ban on pushing to `main` directly.

**Layer 2 — output validation (before commit):**

```python
GUARDRAILS = [no_secrets_in_diff, only_allowed_paths, no_unexpected_files, no_push_to_main]

def validate(diff):
    result = diff
    for g in GUARDRAILS:               # a chain, as in crewAI
        ok, out = g(result)
        if not ok:
            return False, out          # the error goes back to the agent to fix
        result = out
    return True, result
```

On failure — the error is returned to the `fix` stage, retrying up to `guardrail_max_retries` (3 by default).

### 4.7. Human-in-the-Loop via Telegram `(#2, #10)` `[← crewAI AskQuestion / human_input]`

A single mechanism for clarifying questions and action approval. It blocks the current stage until there is an answer (or a timeout).

```python
def ask_human(question: str, context: str, task_id: str,
              kind: str = "question", timeout=...) -> str:
    # kind: "question" (free-form answer) | "approval" (yes/no on an action)
    send_telegram(task_id, question, context, contacts=task.contacts)
    return wait_for_reply(task_id, timeout)   # the answer is returned to the stage context
```

- An agent's clarifying question → `kind="question"`.
- A dangerous/irreversible action (push, deletion, new dependency) → `kind="approval"`.
- Telegram is also used for final result notifications (`done`/`failed` + link to the PR).

### 4.8. Test Runner

Commands from the config, not hardcoded:

```yaml
checks:
  commands:
    - "npm test"
    - "npm run lint"
```

On failure, the test output is passed to the `fix` stage.

### 4.9. State Store with checkpoints `[← crewAI @persist]`

SQLite. The task status = the marker of the last successfully completed stage → resume after a crash.

```sql
CREATE TABLE tasks (
  id           TEXT PRIMARY KEY,
  file_path    TEXT NOT NULL,
  repo         TEXT,
  branch_name  TEXT,
  stage        TEXT NOT NULL,     -- last completed stage (checkpoint)
  status       TEXT NOT NULL,     -- running | done | failed | waiting_human
  provider     TEXT,
  reasoning    TEXT,
  attempts     INTEGER DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  last_error   TEXT
);
```

```python
def run_task(task):
    state = load_or_init(task.id)                 # resume: which stage to continue from
    for stage in STAGES[state.stage_index:]:
        checkpoint(task.id, stage, "running")     # before the step
        run_stage(stage, task)
        checkpoint(task.id, stage, "done")        # after — atomically
```

### 4.10. Reasoning / Complexity `(#7)` `[← crewAI reasoning_effort]`

The `reasoning` and `complexity` fields from the task map onto:
- the coding agent's model flags/budget (`reasoning_effort` low/med/high, thinking-budget for Claude);
- model choice, number of fix iterations, and timeouts based on complexity.

The `plan` stage = the analog of crewAI `reasoning=True` (reflect and draw up a plan before edits).

### 4.11. AGENTS / CLAUDE / SKILLS stubs `(#6)` `[← crewAI agents.yaml/tasks.yaml]`

Stage prompts, roles, and rules are stored in YAML templates with `{variable}` substitution from the task. The stubs for the coding agents, placed into the repository before the run, also go here:

```text
templates/
  AGENTS.md            # instructions for Codex (placed into the repo)
  CLAUDE.md            # instructions for Claude Code (placed into the repo)
  skills/              # reusable skills/procedures
  prompts/
    plan.md            # prompt template for the plan stage with {variables}
    implement.md
    review.md
    fix.md
```

---

## 5. Example `config.yaml`

```yaml
repos:                                # binding to projects/repos (#8)
  my-service:
    url: "git@github.com:OWNER/my-service.git"
    local_path: "./workspace/my-service"
    base_branch: "main"
    branch_prefix: "codex"

tasks:
  pending_folder:    "./tasks/pending"
  processing_folder: "./tasks/processing"
  done_folder:       "./tasks/done"
  failed_folder:     "./tasks/failed"

providers:                            # global enable/disable + fallback (#1)
  default_order: ["codex", "claude"]  # priority for provider: auto
  codex:
    enabled: true
    working_dir: "./workspace/repo"
    approval_mode: "on-request"
    sandbox: "workspace-write"
  claude:
    enabled: true
    sandbox: "workspace-write"

reasoning:                            # complexity levels (#7)
  default: "medium"
  map:                                # complexity → limits
    small:  { fix_attempts: 1, timeout_s: 600 }
    medium: { fix_attempts: 2, timeout_s: 1200 }
    large:  { fix_attempts: 3, timeout_s: 2400 }

guardrails:                           # blacklist (#3)
  forbidden_commands: ["rm -rf", "git push --force", "sudo"]
  forbidden_paths:    ["~", ".env", "secrets/"]
  block_push_to_main: true
  max_retries: 3

checks:
  commands:
    - "npm test"
    - "npm run lint"

git:
  create_pull_request: true
  pr_base: "main"

telegram:                             # HITL + notifications (#2, #10)
  enabled: true
  bot_token_env: "TELEGRAM_BOT_TOKEN"
  chat_id_env:   "TELEGRAM_CHAT_ID"
  ask_timeout_s: 1800
```

---

## 6. Task processing flow

```text
1.  Watcher found a new task in tasks/pending/ → moved it to processing/
2.  Parser parsed the frontmatter/heading (repo, reasoning, provider, contacts...) (#9)
3.  Supervisor recreated for the task from YAML templates (#5)
4.  Check the repo's availability; git fetch/pull (#8)
5.  Create the branch codex/<task-id>
6.  Place the AGENTS/CLAUDE/SKILLS stubs into the repo (#6)
7.  STAGE plan      → CodingAgent.run (no edits) → logs/<id>/plan.md   [checkpoint]
8.  STAGE implement → CodingAgent.run (edits)                          [checkpoint]
9.  STAGE review    → CodingAgent.run (no edits)                       [checkpoint]
10. STAGE test      → Test Runner
11. STAGE fix       → if the tests failed: output → CodingAgent → repeat,
                      limit = reasoning.map[complexity].fix_attempts
12. At any stage: the agent asked a clarifying question
                      → ask_human(question) via Telegram, wait for the answer (#2, #10)
13. STAGE guardrails → action blacklist + diff check;
                       failure → return to fix (up to max_retries) (#3)
14. A dangerous action (push/deletion/new dependency)
                      → ask_human(approval) (#2)
15. STAGE commit    → git add / commit                                [checkpoint]
16. STAGE push      → git push; optionally gh pr create               [checkpoint]
17. Move the task to done/ (or failed/), notify via Telegram (#10)
18. (resume) on a crash at any step — continue from the next incomplete stage
```

The provider fallback `(#1)` triggers transparently inside any `CodingAgent.run`.

---

## 7. State Machine + checkpoints

```text
new → planning → implementing → reviewing → testing
        │                                      │
        │                                 (fail) ├──→ fixing ──┐
        │                                      │   (loop ≤ N)  │
        │                                      ▼               │
        └────────────────────────→ guardrails ◄───────────────┘
                                        │ (fail → fixing)
                                        ▼
                                    committing → pushing → done
                                        │
                          (any step)    ▼
                                     failed
                          (question/approval) → waiting_human → (answer) → continue
```

Each transition = an atomic write of `stage`+`status` to SQLite. On restart, the orchestrator loads the last `stage=done` and continues from the next stage.

---

## 8. Security and blacklist `(#3)`

1. The coding agent runs only inside `workspace/repo`, sandbox `workspace-write`, approval `on-request`.
2. Do not use a full bypass of sandbox/approvals on the main machine.
3. Global blacklist of commands and paths (see `config.yaml: guardrails`).
4. Do not grant access to the home folder and secrets.
5. Ban on direct push to `main`; only via PR.
6. Minimal GitHub token privileges; preferably Docker/VM/a separate OS user.
7. Log all commands and the agent's output.
8. Irreversible actions require human approval (`ask_human(approval)`).

---

## 9. MVP version

```text
Python CLI
  ├─ watchdog            — file watching (#9)
  ├─ subprocess          — git / codex / claude / tests
  ├─ sqlite3             — statuses + checkpoints
  ├─ PyYAML              — config and templates
  ├─ python-telegram-bot — HITL + notifications (#2, #10)
  └─ logging             — per-task logs
```

Minimal logic:

```python
while True:
    task = find_new_task()
    if not task:
        sleep(5); continue

    parse_task(task)                 # (#9)
    supervisor = build_supervisor(task)   # recreation per task (#5)
    create_branch(task)
    seed_agent_templates(task)       # AGENTS/CLAUDE/SKILLS (#6)

    run_stage("plan", task)
    run_stage("implement", task)
    run_stage("review", task)
    if run_tests(task).failed:
        for _ in range(fix_attempts(task)):
            run_stage("fix", task)
            if not run_tests(task).failed:
                break

    if guardrails_ok(task):          # (#3)
        commit_and_push(task)        # with provider fallback (#1)
    notify_telegram(task)            # (#10)
```

---

## 10. Example stage prompts

### Plan
```text
You are working inside a repository. Read the task file.
Draw up a brief implementation plan:
1. which files to change; 2. which functions/modules are affected;
3. which tests to add/update; 4. what the risks are.
Do not change code. If something is ambiguous — ask a clarifying question.
```

### Implementation
```text
Implement the task according to the plan. Constraints:
- change only the necessary files; - do not commit;
- do not add unnecessary dependencies without approval;
- when changing behavior — add/update tests.
```

### Review
```text
Review the changes: alignment with the task, bugs, edge cases,
style, test coverage, absence of extra/accidental files.
```

### Fixing after tests
```text
The tests failed. The output is below. Analyze the error, fix the code,
briefly explain what was changed.
<INSERT TEST OUTPUT>
```

---

## 11. How the 10 original requirements are addressed

| # | Point | Where implemented |
|---|-------|-----------------|
| 1 | Globally enable/disable Codex/Claude + fallback | §4.3 CodingAgent + `run_with_fallback`, §5 `providers` |
| 2 | Answers to clarifying questions + action approval | §4.7 `ask_human(question/approval)`, §6 steps 12/14 |
| 3 | Global blacklist of forbidden items | §4.6 guardrails (2 layers), §5 `guardrails`, §8 |
| 4 | Supervisor manages the agents | §4.4 Supervisor (planning/routing/escalation) |
| 5 | Recreate supervisor+agents for a new task | §4.4 + §6 step 3 + §9 `build_supervisor(task)` |
| 6 | AGENTS/CLAUDE/SKILLS stubs | §4.11 `templates/`, §6 step 6 |
| 7 | Reasoning/complexity level | §4.10 + §5 `reasoning`, task fields |
| 8 | Binding to a project/repo | §4.2 Git Manager + §5 `repos`, the task's `repo` field |
| 9 | Task parsing (headings, extra info, commands) | §4.1 Task Parser, frontmatter schema |
| 10 | Telegram integration | §4.7 + §5 `telegram` (HITL + notifications) |

---

## 12. What to add in the second version

- Parallel tasks via `git worktree`.
- A queue with priorities.
- A Web UI for tasks and logs.
- A PR with a template, integration with GitHub Issues.
- Auto-retry on network errors, a per-task budget limit.
- Auto-closing of stale tasks, a dry-run without push.
- (Optional) lightweight memory between tasks per project — only if needed (crewAI's vector-memory is overkill for this case).

---

## 13. Development order

1. A CLI for a single task file: `python -m orchestrator run tasks/pending/task-001.md`.
2. Task parser + branch creation + a single stage via `CodingAgent` (one provider).
3. The full stage pipeline + tests + the `fix` retry loop.
4. The `CodingAgent` abstraction with fallback (Codex + Claude).
5. Guardrails (action blacklist + diff check).
6. Telegram HITL + notifications.
7. Folder watcher.
8. SQLite checkpoints + resume after a crash.
9. Push + PR.

---

## 14. Sources

- crewAI: https://github.com/crewAIInc/crewAI — the concepts of Flows, hierarchical process, guardrails, reasoning, memory, persistence (source of the borrowed patterns).
- OpenAI Codex CLI Reference: https://developers.openai.com/codex/cli/reference
- OpenAI Codex Agent Approvals & Security: https://developers.openai.com/codex/agent-approvals-security
- OpenAI Codex GitHub Action: https://developers.openai.com/codex/github-action
- GitHub CLI `gh pr create`: https://cli.github.com/manual/gh_pr_create

---

## 15. Short conclusion

Conceptually, the orchestrator = a **crewAI Flow** (a deterministic stage pipeline) + a thin **supervisor** in the role of a manager, but **with no framework dependency**. Five patterns are borrowed from crewAI: checkpoint-state, the delegation/clarification contract, guardrails, reasoning levels, and YAML templates with substitution. Three things crewAI does not cover and they are designed independently: provider fallback (#1), repo binding (#8), and the task watcher+parser (#9). The coding agent does the work on the code, Git stores the changes, CI/PR remain the control layer, the human is brought in via Telegram, and the orchestrator ties everything together into a repeatable, crash-resilient process.
