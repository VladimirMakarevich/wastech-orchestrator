# Final plan for the multi-agent Git orchestrator

Date: 2026-06-11

## 1. Goal

Refine the architecture of the console Git orchestrator so that it can execute task stages through two interchangeable CLI agents:

- OpenAI Codex CLI;
- Anthropic Claude Code CLI.

The orchestrator remains the owner of the process: it accepts the task, manages the Git branch, selects an agent for each stage, runs checks, persists state, performs commit/push, and creates a Pull Request when needed.

The agents work only with the repository contents. They do not manage the Git task lifecycle and do not make decisions about publishing the result.

## 2. Scope of the first version

The first version includes:

- running Codex and Claude Code as child CLI processes;
- selecting a primary and fallback provider separately for each stage;
- overriding the route at the task level through an allowlist;
- a unified format for provider input, result, and errors;
- a sequential pipeline `planning -> implementation -> testing -> review -> fixing`;
- bounded retry/fix cycles;
- recovery after a restart;
- auditing of runs, commands, and artifacts;
- publishing the result only after successful checks.

The first version does not include:

- the Claude Agent SDK or the OpenAI API;
- concurrent operation of several agents in a single working copy;
- automatic merging of the Pull Request;
- a Web UI;
- dynamic agent selection by the model;
- automatic installation or authorization of the CLIs;
- transferring an active vendor session between Codex and Claude Code;
- human-in-the-loop: answering agents' clarifying questions and approving specific actions (deferred; see §18.2);
- per-task reasoning/complexity levels (deferred; see §18.2);
- Telegram integration for results and prompts (deferred; see §18.2).

## 3. Core principles

1. The orchestrator core must not know the syntax of any specific CLI.
2. Each stage is an independent run and receives all the context it needs through files and a prompt.
3. Fallback is allowed only for infrastructure errors of a provider.
4. Implementation errors, test failures, and review findings are handled through the `fixing` stage.
5. Commit, push, and PR creation are performed only by the orchestrator.
6. Every automatic cycle has a configurable limit.
7. All routing and provider-switching decisions are persisted to the state store and the logs.
8. The next development stage begins only after the DoD of the previous one has been met.

## 4. Components

```text
Task Source
    |
    v
Orchestrator Core
    |-- Task Parser
    |-- State Machine
    |-- Agent Router
    |     |-- CodexProvider
    |     `-- ClaudeCodeProvider
    |-- Git Manager
    |-- Check Runner
    |-- Artifact Store
    `-- State Store
```

### 4.1. Orchestrator Core

Manages the sequence of stages, attempt limits, state transitions, and publishing conditions. The Core invokes only the `AgentProvider` interface and does not build provider-specific commands.

### 4.2. Agent Router

For each stage it determines:

- the primary provider;
- the fallback provider;
- the route source: global config or task override;
- provider availability per the allowlist;
- the next permitted attempt.

### 4.3. AgentProvider

A common contract for Codex and Claude Code:

```text
AgentProvider
  id
  preflight() -> ProviderHealth
  run(AgentRunRequest) -> AgentRunResult
```

`ProviderHealth` contains:

- presence of the executable;
- the detected version;
- the authorization status;
- support for the required CLI capabilities;
- a diagnostic message without secrets.

`AgentRunRequest` contains:

- `task_id`;
- `stage`;
- `working_directory`;
- `prompt`;
- paths to the task, plan, diff, check, and review artifacts;
- `permission_profile`;
- `timeout_seconds`;
- `attempt`;
- `output_schema`;
- a provider-specific model and safe additional parameters.

`AgentRunResult` contains:

- `status`: `succeeded` or `failed`;
- `provider`;
- `stage`;
- `attempt`;
- `exit_code`;
- `started_at` and `finished_at`;
- `final_message`;
- `structured_output`;
- `usage`, if the CLI reported it;
- `session_id`, for auditing only;
- paths to stdout/stderr/raw event log;
- the normalized error.

### 4.4. Provider adapters

`CodexProvider` is responsible for:

- preflight through the available Codex CLI commands;
- building a safe `codex exec` invocation;
- JSONL/structured output;
- mapping the sandbox and permission profile;
- normalizing the exit code and events.

`ClaudeCodeProvider` is responsible for:

- preflight through the available Claude Code CLI commands;
- building a safe `claude -p` invocation;
- `stream-json`/structured output;
- mapping the permission mode, allowed/denied tools, and sandbox;
- normalizing the exit code and events.

Provider adapters do not perform fallback and do not modify the state machine.

## 5. Stages and routing

The supported stages are:

```text
planning
implementation
testing
review
fixing
publishing
```

`testing` is performed by the Check Runner, and `publishing` is performed by the Git Manager. For the agent stages, the default route is:

| Stage | Primary | Fallback |
|---|---|---|
| `planning` | Claude Code | Codex |
| `implementation` | Claude Code | Codex |
| `review` | Codex | Claude Code |
| `fixing` | Claude Code | Codex |

A route may be overridden in a task only:

- for known stages;
- by a provider from `agents.allowed`;
- without changing the security policy, command path, or credentials;
- after the task has been fully validated and before the branch is created.

Example YAML front matter:

```yaml
---
id: task-001
agents:
  planning: claude
  implementation: codex
  review: claude
  fixing: codex
---
```

For a JSON task, an `agents` object with the same keys is used.

## 6. Context between stages

The vendor session is not the source of truth. Each new run receives its context from artifacts:

- the original task;
- the normalized task manifest;
- the approved plan;
- the current `git diff`;
- the results of tests and linters;
- the findings of the previous review;
- a description of the previous error or partially completed attempt.

This makes it possible to execute the next stage with a different provider and to recover the pipeline after a restart.

## 7. Fallback and errors

### 7.1. Error classes

`ProviderError` is normalized into one of the following classes:

- `binary_not_found`;
- `unsupported_version`;
- `authentication_failed`;
- `authorization_failed`;
- `rate_limited`;
- `network_unavailable`;
- `provider_unavailable`;
- `timeout`;
- `process_crashed`;
- `invalid_output`;
- `permission_denied`;
- `configuration_error`;
- `task_failure`.

### 7.2. Errors that permit fallback

Fallback is allowed for:

- `binary_not_found`;
- `unsupported_version`;
- `authentication_failed`;
- `rate_limited`;
- `network_unavailable`;
- `provider_unavailable`;
- `timeout`;
- `process_crashed`;
- `invalid_output`.

`authorization_failed` and `permission_denied` permit fallback only when the denial applies to a specific provider and the fallback provider operates in the same or a stricter permission profile. Relaxing the security policy is prohibited.

### 7.3. Errors without fallback

Fallback does not apply to:

- failed tests or linters;
- code review findings;
- incomplete fulfillment of the task requirements despite a successful CLI exit;
- Git errors;
- an invalid task or configuration;
- exhaustion of the fix cycles;
- a violation of the security policy.

Such cases are routed to `fixing`, `failed`, or require manual intervention depending on the state machine.

### 7.4. Partial changes

Before an agent run the following are saved:

- the current commit SHA;
- `git status --porcelain`;
- a checksum of the diff;
- the list of existing artifacts.

If the primary provider failed with an infrastructure error after files had been changed:

1. The orchestrator does not roll back the changes automatically.
2. A post-attempt snapshot and diff are saved.
3. The fallback receives the current diff and a message about the partially completed attempt.
4. The fallback result goes through the full set of checks.
5. Both runs remain in the audit.

## 8. State machine

Task statuses:

```text
new
validated
preparing
planning
implementing
testing
reviewing
fixing
ready_to_publish
committing
pushing
creating_pr
done
failed
manual_action_required
```

Main transitions:

```text
new -> validated -> preparing -> planning -> implementing
implementing -> testing
testing(success) -> reviewing
testing(failure) -> fixing -> testing
reviewing(success) -> ready_to_publish
reviewing(blocking findings) -> fixing -> testing
ready_to_publish -> committing -> pushing -> creating_pr -> done
any active stage -> failed
any active stage -> manual_action_required
```

Conditions:

- a transition is performed transactionally;
- a re-run does not create a second commit, push, or PR;
- after a restart the unfinished stage is resumed or its result is safely reconciled;
- publishing is allowed only when checks succeed and there are no blocking findings;
- the number of agent attempts and fix cycles is bounded by the configuration.

## 9. State Store

SQLite remains sufficient for the first version. In addition to `tasks`, the following entities are needed:

```text
tasks
stage_runs
provider_attempts
check_runs
artifacts
publish_operations
```

At a minimum the following are stored:

- the identifiers of the task, stage, and attempt;
- the selected primary/fallback and the provider actually used;
- the status and error class;
- timestamps and the exit code;
- the commit SHA before and after the stage;
- paths to artifacts;
- the fingerprint of the commit/push/PR operation;
- the retries and fix cycle counters.

Secrets, access tokens, and the full process environment are not stored in SQLite.

## 10. Artifacts and logs

```text
logs/
  <task-id>/
    task.normalized.json
    plan.md
    current.diff
    checks/
      <run-id>.log
    review/
      findings.json
      summary.md
    stages/
      <stage>/
        <attempt>-<provider>/
          request.json
          stdout.log
          stderr.log
          events.jsonl
          result.json
          before.diff
          after.diff
    publish/
      commit.json
      push.json
      pull-request.json
```

Rules:

- all paths are relative to the task artifact directory;
- logs are not overwritten;
- the request artifact stores a redacted representation of the run;
- the machine-readable result is separated from the human-readable summary;
- artifacts are registered in SQLite with a checksum.

## 11. Configuration

Target structure:

```yaml
repo:
  url: "git@github.com:OWNER/REPO.git"
  local_path: "./workspace/repo"
  base_branch: "main"
  branch_prefix: "agent"

agents:
  allowed:
    - claude
    - codex

  max_stage_attempts: 2
  max_fix_cycles: 3

  routing:
    planning:
      primary: claude
      fallback: codex
    implementation:
      primary: claude
      fallback: codex
    review:
      primary: codex
      fallback: claude
    fixing:
      primary: claude
      fallback: codex

  providers:
    claude:
      command: "claude"
      model: ""
      timeout_seconds: 1800
      max_turns: 50
      max_budget_usd: null
      permission_profile: "workspace-write"
      extra_args: []
    codex:
      command: "codex"
      model: ""
      timeout_seconds: 1800
      sandbox: "workspace-write"
      permission_profile: "workspace-write"
      extra_args: []

security:
  strict_isolation: true
  allowed_environment:
    - "PATH"
    - "HOME"
    - "USERPROFILE"
    - "CODEX_HOME"
    - "CLAUDE_CONFIG_DIR"
  denied_read_paths:
    - ".env"
    - "secrets/**"
  denied_commands:
    - "git commit"
    - "git push"
    - "gh pr create"

checks:
  commands:
    - "npm test"
    - "npm run lint"

git:
  create_pull_request: true
  pr_base: "main"
```

Configuration requirements:

- unknown route keys are treated as an error;
- the primary and fallback cannot reference a forbidden provider;
- a task override cannot change the provider command, extra args, or security;
- `extra_args` are validated against the provider allowlist and must not allow disabling the sandbox/permissions;
- a legacy Codex-only config is migrated to a Codex route for all agent stages with a warning.

## 12. Security model

1. The working area is confined to a separate clone/worktree.
2. Agents are forbidden to commit, push, merge, and create PRs.
3. The orchestrator passes only allowlisted environment variables.
4. Secret files are excluded from reading and logging.
5. CLIs are launched without shell interpolation of user strings.
6. The task ID, branch name, and paths undergo strict normalization.
7. Options that bypass the sandbox/permissions are forbidden by the configuration validator.
8. With `strict_isolation: true`, an inability to enable the required isolation fails preflight with an error.
9. Git credentials and the agents' credentials are configured outside the orchestrator.
10. The Pull Request and CI remain a mandatory control layer.

## 13. Recovery and idempotency

On startup the orchestrator:

1. Finds tasks in active statuses.
2. Reconciles SQLite, the task files, the working branch, and the artifacts.
3. Checks whether the external process has finished and whether a valid result artifact exists.
4. Repeats only the unfinished idempotent operation.
5. For commit/push/PR it uses the saved fingerprint and checks the remote state.
6. In an ambiguous state it moves the task to `manual_action_required`.

The following must not be done automatically:

- republishing an unknown commit;
- deleting partial changes;
- changing the provider route retroactively for a stage that has already started;
- continuing after an inconsistent branch state has been detected.

## 14. Checks and acceptance

### Unit

- validation of the configuration and task overrides;
- route resolution and the allowlist;
- the command builder of both providers;
- parsing of structured output;
- error classification;
- state machine transitions;
- redaction and path normalization;
- retry/fallback limits.

### Integration

Fake CLI executables are used for the scenarios:

- a successful run;
- a missing binary;
- a failed authorization;
- a rate limit;
- a timeout;
- a process crash;
- malformed output;
- an infrastructure error after files were changed;
- a successful fallback;
- denial of fallback for a quality failure.

### End-to-end

On a temporary Git repository, verify that:

- Claude Code performs planning and implementation;
- Codex performs review;
- failed checks trigger fixing;
- a successful result leads to a single commit, push, and PR;
- a restart does not duplicate publishing;
- exhaustion of attempts moves the task to `failed`.

## 15. Implementation stages

The stages are executed strictly in sequence:

1. [Contracts and configuration](implementation_stages/01_contracts_and_config.md)
2. [Provider layer and the Codex adapter](implementation_stages/02_provider_layer.md)
3. [Claude Code adapter](implementation_stages/03_claude_code_adapter.md)
4. [Routing and fallback](implementation_stages/04_routing_and_fallback.md)
5. [Pipeline and recovery](implementation_stages/05_pipeline_and_recovery.md)
6. [Security and observability](implementation_stages/06_security_and_observability.md)

Advancing to the next stage is allowed only after every item of the current stage's DoD has been documented as complete.

## 16. Final Definition of Done

The project is considered complete when:

- Codex and Claude Code are accessible only through the common `AgentProvider`;
- the Claude route for planning/implementation/fixing and the Codex route for review work by default;
- task-level overrides are limited by the allowlist;
- infrastructure fallback works and is fully audited;
- quality failures do not cause a provider switch;
- the state machine recovers after a controlled restart;
- commit, push, and PR are performed only by the orchestrator and are not duplicated;
- the security policy cannot be relaxed through a task or `extra_args`;
- the unit, integration, and end-to-end tests pass;
- the operations documentation describes the installation, preflight, authorization, and diagnostics of both CLIs.

## 17. Official reference materials

- [OpenAI Codex CLI Reference](https://developers.openai.com/codex/cli/reference)
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code Settings](https://code.claude.com/docs/en/settings)
- [Claude Code Security](https://code.claude.com/docs/en/security)
- [GitHub CLI: `gh pr create`](https://cli.github.com/manual/gh_pr_create)

## 18. Scope decisions and deferred work (v2)

This section records the design decisions and the ideas that are intentionally out of scope for the first version, so that nothing from the original requirements is silently dropped. The fuller designs for the deferred items live in [codex_git_orchestrator_architecture.md](codex_git_orchestrator_architecture.md).

### 18.1. Design decisions

- **Deterministic Core instead of an LLM "supervisor".** The original idea of a supervisor agent that launches and manages the other agents is intentionally not adopted in the first version. Orchestration is handled by a deterministic Orchestrator Core plus the Agent Router (see §4.1–§4.2): predictability and auditability take priority over emergent autonomy. A supervisor-style planning layer may be revisited in v2 on top of this deterministic base.
- **Stateless stage runs cover "recreate agents per task".** The requirement to refresh agent state/context for each new task is satisfied structurally: every stage is an independent run whose context comes only from artifacts (see §3 and §6); there is no shared in-memory agent state to reset.

### 18.2. Deferred to v2

- **Human-in-the-loop: clarifying questions and action approval.** A mechanism for answering agents' clarifying questions and granting approval for specific (especially irreversible) actions. Designed in architecture.md §4.7. In v1, ambiguous or unsafe situations resolve to the `fixing` / `manual_action_required` states instead of an interactive prompt.
- **Reasoning / complexity levels per task.** Per-task `reasoning` and `complexity` fields that map to provider model flags and to limits (attempts, timeouts). Designed in architecture.md §4.10. In v1, the model and limits are set globally in the configuration.
- **Telegram integration.** Sending results and human-in-the-loop prompts to a Telegram bot. Designed in architecture.md §4.7. In v1, results are observed through logs and artifacts. It is important to have a separate Telegram chat for each project and repository.
- **Richer task parsing.** Beyond the per-stage routing override (§5), extracting additional fields such as contacts and free-form commands/hints from the task. Designed in architecture.md §4.1.

These items are the canonical backlog.
