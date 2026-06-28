# Project Glossary

This glossary is the project-wide vocabulary for wastech-orchestrator. It is organized by the parts of the system that an operator or task author can touch. Terms keep the same names used in code and docs; legacy or removed names are marked explicitly.

Use this file as the canonical reading aid for commands, task files, config keys, flow and provider concepts, checks, Git behavior, runtime artifacts, notifications, and recovered state.

## Contents

- [Entry points](#entry-points)
- [Task language](#task-language)
- [Configuration](#configuration)
- [Flow vocabulary](#flow-vocabulary)
- [Providers](#providers)
- [Checks and security](#checks-and-security)
- [Git, artifacts, and recovery](#git-artifacts-and-recovery)
- [Notifications and observability](#notifications-and-observability)
- [Legacy and renamed terms](#legacy-and-renamed-terms)

## Entry points

- **`wastech-orchestrator` / `worc`** - The CLI entry point. Both names expose the same commands, so the short alias is just a convenience, not a different interface.
- **`install`** - Binds the current repository into `<repo>/.worc/`, writes or refreshes `config.yaml`, seeds the packaged guide and flow copies, and can run preflight after setup.
- **`run`** - Processes one task file end to end through the orchestrator.
- **`watch`** - Watches the task queue, resumes in-flight work first, then picks pending tasks one at a time. `--queue NAME` serves only that queue.
- **`stop`** - Stops a running `watch` daemon via the **stop ladder**: idle stops with no prompt; a busy daemon is refused unless confirmed (`YES`) or forced — `--force` (soft: finish the current step) or `--force-full` (hard: kill the agent's process group now; POSIX only, Windows degrades to soft).
- **`restart`** - Stops the running watcher (same stop ladder) and starts a fresh one with new flags.
- **`status`** - Shows the active or latest persisted task state without starting work.
- **`top`** - Live, read-only monitor (a client over the daemon, not an engine host): the active task + flow node, a parked/gate-pending marker, the queue-filtered priority-sorted pending queue, recent terminal tasks, and a tail of the daemon `--log-file`; polls `state.db` read-only and quits on `q`. Stdlib-only — no extra needed.
- **`shell`** - Interactive operator console (the `[shell]` extra): a prompt_toolkit REPL that spawns or attaches to the `watch` daemon, streams its log above the prompt, and dispatches `enqueue`/`ps`/`status`/`logs`/`prs`/`merge-task`/`finalize`/`rerun`/`up`/`down`/`restart`/`cancel` onto the existing commands. It never hosts the engine itself.
- **`list`** - Read-only enumeration of the active task, the `tasks/pending` queue, and recent terminal tasks; `--format ids` (filtered by `--scope`) is the machine-readable source that backs completion.
- **`completion`** - Prints a `bash`/`zsh` completion script (sourced once) that completes subcommands and flags statically and task ids dynamically via `worc list --format ids`.
- **`preflight`** - Runs read-only readiness checks for providers, isolation, and configured check sets.
- **`telegram-test`** - Sends one correlated Telegram prompt and waits for a reply as a smoke test.
- **`upgrade-config`** - Updates an older `config.yaml` to the current schema shape without changing user values.
- **`upgrade-docs`** - Refreshes the installed packaged guide copy under `.worc/guide/`.
- **`rerun`** - Re-attempts a terminal task, either from scratch or by resuming from the saved checkpoint.
- **`finalize`** - Records a task that was completed or abandoned out of band, without running the pipeline.
- **`prs`** - Read-only list of orchestrator PRs that are open and awaiting merge; `--check` adds live GitHub state, `--sync` reconciles PRs merged externally (dry-run unless `--yes`).
- **`merge-task`** - Operator go-ahead to merge a reviewed PR: pulls `base_branch` into the task branch, resolves any conflicts via the merge flow, then merges. The human-in-the-loop counterpart to `git.auto_merge`.
- **`tasks`** - Read-only list of every known task with its status and branch; `--status` filters.
- **`--config`** - Explicit path to `config.yaml`; it overrides automatic discovery.
- **`--env-file`** - Explicit path to an environment file; if omitted, the orchestrator auto-loads `<repo>/.worc/.env` when present.
- **`--version`** - Prints the CLI version and exits.
- **`operator`** - Typically the person who runs and manages the orchestrator.

## Task language

- **Task file** - The input contract for a task. Markdown is the normal format, and JSON is also accepted.
- **Front matter** - The YAML block at the top of a Markdown task file. It carries the task metadata that the validation gate allows.
- **`id`** - Stable normalized task identifier. It is used in task storage, branch naming, artifacts, and reports.
- **`title`** - Human-readable task name. It is used in branch slugs, PR titles, and summaries.
- **`task_type`** - Dispatch key that selects the flow. Built-ins are `implementation`, `deep_research`, and `security_audit`; operator flows can add more.
- **`branch_name`** - Full task-branch override. When omitted, the orchestrator derives a branch from `repo.branch_prefix`, `id`, and a slug of `title`.
- **`slug`** - Lowercase branch fragment derived from the task title. It is part of the default branch name, not a separate task field.
- **`auto_merge`** - Per-task publish override. `true` requests auto-merge, `false` opts out, and omission falls back to the config default.
- **`prompt_audit`** - Per-task prompt logging override. It wins over the global config value in both directions.
- **`decomposition`** (task field) - Per-task gate override for whether decomposition is permitted. It wins over `agents.decomposition.enabled` in both directions; it only flips the gate, never forces a split.
- **`contacts`** - Plain-text mentions that appear in Telegram notifications and HITL prompts. They are not access control.
- **`depends_on`** - List of other task ids that must be merged before this task can start.
- **`subtasks`** - Operator-authored decomposition list. It names ordered subtask spec files that the orchestrator will run as one branch and one PR.
- **`nodes`** - Per-node overrides keyed by flow node id: `enabled: false` skips that node for the task, and `model` / `reasoning` / `provider` overlay that node's executor for this run (best-effort — an invalid value is warned and skipped at run time, never fatal).
- **`## Description`** - The body section used to describe the requested work. A Markdown task is expected to have a non-empty description body.
- **`Acceptance criteria`** - Testable statements that define what done looks like. They help a vague task become complete enough to skip refinement.
- **`refinement`** - The automatic enrichment pass for vague tasks. It adds missing context instead of asking the user to infer it later.
- **`decomposition`** (concept) - A sequential split of one task into multiple subtasks. It is flow- and planning-controlled; the per-task `decomposition` field only flips whether a split is _permitted_ (default `agents.decomposition.enabled`), never forces one.
- **Task statuses** - `new` means the task has been parsed but not yet validated; `validated` means it passed the gate; `preparing` means the orchestrator owns the slot and is setting up execution; `running` means the flow engine is active; `done`, `failed`, and `manual_action_required` are terminal; `pending` is the queue waiting state before the slot is acquired.
- **State machine** - The finite set of task statuses and allowed transitions between them.
- **Single processing slot** - The invariant that only one task can be active at a time.
- **Task lifecycle folders** - `tasks/pending` holds queued tasks, `tasks/processing` holds active work, `tasks/done` and `tasks/failed` hold terminal tasks, and `.worc/tasks/rejected` is the quarantine folder for invalid tasks. The `tasks` root is the default; it is configurable via `paths.tasks_dir` (the subfolder names are fixed).
- **`summary.md`** - The plain-language handoff written at task close. In the default publish path it becomes the PR body; when synthesis cannot run, the orchestrator falls back to a deterministic minimal summary.
- **`validation_report.json`** - The structured report written by the validation gate. Rejected tasks use it to explain the failure, and accepted tasks may also persist it for resume/audit.
- **`task.normalized.json`** - The normalized resume-safe copy of the parsed task metadata written under `logs/<task-id>/` and loaded back on recovery.
- **`rejected`** - The quarantine outcome for a structurally invalid task. Rejected tasks do not create a branch.

## Configuration

- **`config.yaml`** - The operator config file for repository binding, provider selection, security policy, check sets, Git publishing, and notifications.
- **`schema_version`** - The config format version. The loader refuses a newer schema than it understands.
- **`orchestrator`** - The outer queue behavior block. It controls automatic continuation and the watch poll interval.
- **`orchestrator.auto_mode.enabled`** - When `true`, `watch` may pick the next pending task after terminal cleanup succeeds.
- **`orchestrator.poll_interval_seconds`** - Watch loop interval. `0` makes `watch` a single pass.
- **`repo`** - The target repository block. It names the remote URL, local clone path, base branch, and branch prefix.
- **`repo.url`** - Remote repository URL for the target clone.
- **`repo.local_path`** - Dedicated clone or workspace path used for agent runs.
- **`repo.base_branch`** - The branch checked out before task work and restored after terminal cleanup.
- **`repo.branch_prefix`** - Prefix for the default task branch name.
- **`paths`** - Optional block locating the task lifecycle on disk.
- **`paths.tasks_dir`** - Repo-relative directory holding the `pending`/`processing`/`done`/`failed` lifecycle subfolders (default `tasks`). Validated repo-relative (no `..`/absolute) and rejected if it lives under the gitignored `.worc/` home; a subpath such as `config/tasks` is allowed.
- **`agents`** - Provider availability, retry budgets, decomposition, and provider-specific settings.
- **`agents.allowed`** - The provider ids the router may use.
- **`agents.max_stage_attempts`** - Maximum attempts for one stage, including fallback.
- **`agents.max_fix_cycles`** - Maximum repeated fix cycles for one local failing loop.
- **`agents.max_total_fix_iterations`** - Global cap across the whole task, including subtasks.
- **`agents.decomposition.enabled`** - Enables or disables decomposition as a planning option.
- **`agents.decomposition.max_subtasks`** - Maximum accepted split size for decomposition.
- **`agents.providers.<id>`** - Provider-specific config for `codex` or `claude`. It holds the executable command, model, reasoning level, timeout, permission profile, extra args, and the `primary` marker; Codex also uses `sandbox`, and Claude also uses `max_turns`.
- **`security`** - Isolation, environment allowlist, denied paths, denied commands, and the fail-closed security ceiling.
- **`security.strict_isolation`** - Controls whether full-access provider modes are rejected at preflight.
- **`security.allowed_environment`** - Environment variable names that are allowed to reach child processes.
- **`security.denied_read_paths`** - Paths that the orchestrator will not read as secrets or task context.
- **`security.denied_commands`** - Commands that are refused even if they are otherwise present in config or flow arguments.
- **`validation`** - The task input hardening block. It rejects malformed or suspicious task files before branch creation.
- **`validation.max_task_bytes`**, **`validation.max_task_lines`**, **`validation.max_line_bytes`**, **`validation.max_control_ratio`** - Size and content ceilings for task input.
- **`validation.required_fields`** - Required task metadata fields.
- **`validation.reject_unknown_fields`** - Whether extra task front matter keys are refused.
- **`validation.quarantine_folder`** - Where invalid tasks are moved.
- **`checks`** - The operator-authored quality gate block.
- **`checks.command_sets`** - Named groups of checks selected by diff paths. An empty mapping means no quality gate.
- **`checks.timeout_seconds`** - Default per-command timeout for the check runner.
- **`CheckCommandSpec`** - One structured check command: a logical name, an explicit argv list, and an optional repo-relative `cwd`.
- **`CommandSet`** - A named bundle under `checks.command_sets`: a diff-selected group of `CheckCommandSpec`s with optional `paths`, `timeout_seconds`, and `skip_if_unavailable`.
- **`paths`** - Diff selectors inside a check set. They decide when that set runs.
- **`skip_if_unavailable`** - A check-set flag that allows a missing toolchain to be skipped loudly instead of failing the task.
- **`git`** - Publish and PR behavior.
- **`git.create_pull_request`** - Controls whether the orchestrator opens a PR after push.
- **`git.pr_base`** - Target base branch for the PR.
- **`git.auto_merge`** - Instance-level auto-merge default.
- **`git.auto_merge_strategy`** - Merge strategy used when auto-merge runs (and the default for `merge-task --strategy`).
- **`git.auto_merge_wait_for_checks`** - Whether GitHub-native auto-merge waits for required checks (and the default for `merge-task --wait-for-checks`).
- **`git.merge_flow`** - Name of the flow `merge-task` runs to resolve a conflicting base-merge (default `merge`). A clean base-merge is mechanical (no flow); only a conflict launches it.
- **`git.footprint.audit_commit_message`** - Commit message template for the audit trail commit.
- **`git.footprint.audit_on_branch`** - Chooses whether the audit trail is committed on the task branch or on a sibling branch.
- **`telegram`** - Optional Telegram human-in-the-loop and notification config.
- **`telegram.enabled`** - Enables or disables the Telegram transport.
- **`telegram.bot_token_env`** - Environment variable name that holds the bot token.
- **`telegram.chat_id_env`** - Environment variable name that holds the chat id.
- **`telegram.ask_timeout_s`** - Timeout for blocking HITL waits.
- **`skills`** - Repo skill selection: whole-repo discovery + operator pins + supervisor proposal.
- **`skills.dynamic`** - Whether the supervisor proposes a `node → skills` map once per task (skipped when the repo ships no skills).
- **`skills.strict`** - Whether an unresolved operator skill pin stops the task (`true`) or is warned + skipped (`false`).
- **`supervisor`** - The constant advisory layer above any flow.
- **`supervisor.role_file`** - Role prompt file used by the supervisor.
- **`supervisor.model`** - Provider model used by the supervisor, when set.
- **`supervisor.reasoning`** - Reasoning level used by the supervisor, when set.
- **`prompt_audit`** - Global default for prompt recording. A per-task value can override it.

## Flow vocabulary

- **Flow** - A validated YAML graph of typed nodes selected by `task_type`. The pipeline is data, not a hardcoded stage loop.
- **Packaged flow** - A built-in flow shipped with the package.
- **Operator flow** - A repository-local override under `.worc/flows/<task_type>.yaml`.
- **`implementation`** - The default coding flow that produces a reviewed Pull Request.
- **`deep_research`** - The research flow that produces a documentation-oriented output.
- **`security_audit`** - The audit flow that produces a private control-workspace report.
- **`merge`** - The conflict-resolution flow `worc merge-task` runs (only) when pulling `base_branch` into a task branch conflicts: `conflict_resolution` (agent) → `testing` (checks) with a bounded fix loop, terminating at a no-op `publish` (`policy: none`). It performs no git itself — the orchestrator commits the merge and merges the PR after the flow returns a clean, green tree. Not dispatched for incoming tasks; selected by `git.merge_flow`.
- **Flow node kinds** - `agent` runs an editing or authoring step, `evaluator` reads an artifact and returns a verdict, `checks` runs the quality gate, `hitl` asks the human in the loop, and `publish` performs the orchestrator-owned publish step.
- **Run vocabulary** - `RunKind` is the top-level run discriminator (`stage` or `evaluator`); `EvaluatorRole` names the shipped evaluator roles (`review`, `critic`, `verifier`, `test_quality`).
- **Typed node output** - `OutputContract` selects the strict structured-output parser for agent nodes (`none`, `human_input`, `planning`); `HumanInputSignal` is the validated question/approval payload; `TypedStageOutput` is the parsed structured result.
- **Route result** - `RouteSource` says whether a node's provider came from config or an explicit flow-node override; `ResolvedRoute`, `ProviderAttempt`, and `StageOutcome` are the router's resolved pair, per-attempt record, and final result bundle.
- **`NodeInfraError`** - The node-run exception used when a provider could not complete a node because of infrastructure trouble; it may lead to fallback or a terminal stop.
- **`NodeManualRequired`** - The node-run exception that forces the task into `manual_action_required`.
- **Execution identity** - `ExecutionUnit` is the `(task_id, subtask_order)` identity for a root task or one decomposed subtask; `flow_fingerprint` is the hash of the resolved flow snapshot used on resume.
- **Node bookkeeping** - `current_node` is the in-flight node inside `running`, `active_subtask` is the 1-based subtask counter on the task row, and `best_effort` lets an agent node continue when no provider can complete it.
- **`output_artifact`** - A named slot that stores a node result and threads it downstream. Common slots include `enriched_spec` (`task.enriched.md`), `plan` (`plan.md`), and `summary` (`summary.md`).
- **Implementation flow nodes** - `refinement` enriches a vague task, `planning` builds a plan and may propose decomposition, `implementation` edits files, `testing` runs checks, `review` performs read-only evaluation, `fixing` loops on quality findings, `documentation` writes the handoff material, and `publish` finalizes Git publication.
- **Deep-research flow nodes** - `repository_analysis` reads the repo, `external_research` fetches outside sources, `architecture_design` shapes the report, `synthesis` writes the deliverable, `citation_check` validates `sources.json`, `fact_verification` and `critical_review` rework or accept, and `publish` creates the documentation PR.
- **Security-audit flow nodes** - `scope` frames the audit, `repository_analysis` inspects the repo, `dependency_scan` runs evidence scanners, `threat_analysis` and `finding_verification` refine findings, `report` writes the private report, and `private_storage` records it without git.
- **`when`** - A deterministic skip predicate on a node. It enables or disables execution based on a resolved fact.
- **`permission_ceiling`** - The highest permission profile allowed by a flow.
- **`output_policy`** - The write-containment policy for a flow. It says where deliverables may be written and what files must exist.
- **`publishing`** - The terminal publication policy for a flow, such as PR publishing, documentation PRs, local artifacts, or no publish.
- **`network_policy`** - The flow-wide network grant. Absence means no network by default.
- **`session_scope`** - The provider session intent for a node, such as fresh disposable, editing lineage, or resumable own lineage.
- **`FlowEngine`** - The execution spine that traverses the graph, routes outcomes, owns budgets, and updates checkpoints.
- **`budget`** and **`loop`** - The repeat counters on rework edges. They bound fix cycles and other repeated regions.
- **`sub_flow`** - The decomposition region that is repeated for each accepted subtask.
- **`decomposition`** - The flow feature that fans one task into sequential subtasks when planning accepts a split.
- **`supervisor`** - The constant advisory layer above every flow. It observes completed steps read-only and writes the final summary, but it is not a node and not a status.
- **`task_type` dispatch** - The mapping from task type to flow. It is the entry point for choosing which graph runs.

## Providers

- **`codex`** and **`claude`** - The only supported provider ids.
- **`ProviderId`** - The canonical enum for supported providers.
- **`global primary`** - The single configured provider with `primary: true`. It runs any node without an explicit provider and is the only fallback target.
- **`ResolvedRoute`** - The chosen primary and fallback provider pair for one node.
- **`fallback`** - A second provider run used only for infrastructure errors and only when policy allows it.
- **`ProviderHealth`** - The preflight result for one provider, including executable availability and capability diagnostics.
- **`AgentProvider`** - The provider adapter contract. It exposes preflight and one stage run, and it does not own fallback or state changes.
- **`AgentRunRequest`** - The request object sent to a provider. It carries the task id, node id, working directory, prompt, timeout, model, extra args, and artifact paths.
- **`AgentRunResult`** - The normalized provider result, including status, timestamps, optional structured output, and artifact paths.
- **`ProviderError`** - The normalized provider exception. It carries an error class that the router uses for fallback decisions.
- **`RouteSource`** - Where a node's provider came from: config default or an explicit flow-node override.
- **`ResolvedRoute`** - The chosen primary and fallback provider pair for one node.
- **`ProviderAttempt`** - One provider invocation within a node run.
- **`StageOutcome`** - The router result bundle for one node run, including attempts, result, terminal error, and partial change metadata.
- **`ErrorClass`** - The provider failure taxonomy: `binary_not_found`, `unsupported_version`, `authentication_failed`, `authorization_failed`, `rate_limited`, `network_unavailable`, `provider_unavailable`, `timeout`, `process_crashed`, `invalid_output`, `permission_denied`, `configuration_error`, `task_failure`, and `session_unavailable`.
- **Infrastructure error** - A provider failure that can be retried or fallen back from, such as a missing binary, auth failure, timeout, or crash.
- **Quality failure** - A successful provider run that produced a failing result. It goes to fixing, not fallback.
- **`session_unavailable`** - The provider could not resume the requested session. The router retries the same provider once with a fresh session.
- **`permission_profile`** - The orchestrator permission profile passed to the provider adapter.
- **`read-only`** - A permission profile that forbids writes.
- **`workspace-write`** - A permission profile that allows editing inside the workspace.
- **`sandbox`** - The Codex isolation mode field.
- **`danger-full-access`** - Codex full-access mode. It is operator-selectable but rejected when `strict_isolation` is enforced.
- **`bypassPermissions`** - The Claude full-access mode. It is operator-selectable but rejected when `strict_isolation` is enforced.
- **`model`** - The provider model name or account default selector.
- **`reasoning`** - The provider reasoning-effort setting.
- **`timeout_seconds`** - The provider run timeout.
- **`max_turns`** - The Claude turn cap.
- **`extra_args`** - Additional provider CLI arguments, appended after the orchestrator's own safety checks.

## Checks and security

- **`checks.command_sets`** - The operator-authored quality gate. The orchestrator does not auto-discover commands.
- **`command_profile`** - The current `checks`-node mode that diff-selects operator-authored `checks.command_sets`, runs them all through the Check Runner, and maps the aggregate to pass/fail/incomplete.
- **`citation`** - The deterministic citation-manifest checker used by `deep_research`; it fails when a cited source is broken.
- **`dependency_scan`** - The core-owned argv scanner used by `security_audit`; it always reports pass and lets the flow decide what that means.
- **Command set** - A named group of check commands selected by diff paths and run together.
- **`ResolvedCheck`** - The normalized internal form of one check command. It is always an argv list, never a shell string.
- **`ResolvedCheckSet`** - The normalized internal form of one command set.
- **`cwd`** - The repo-relative working directory for one check command.
- **`skip_if_unavailable`** - A check-set setting that allows a missing binary to be skipped loudly.
- **Quality gate** - The check-run phase that can pass, fail, or be incomplete.
- **Incomplete gate** - The gate state where a required toolchain is missing or every selected check was skipped. It sends the task to `manual_action_required`.
- **Check Runner** - The component that launches configured checks and aggregates their outcome.
- **`task validation gate`** - The pre-branch input validator that rejects malformed or unsafe tasks.
- **`validation_report.json`** - The validation-gate report written under `logs/<task-id>/`; rejected tasks use it to explain the failure, and accepted tasks may also persist it.
- **`quarantine_folder`** - The folder where invalid tasks are isolated.
- **`strict_isolation`** - The policy switch that decides whether full-access provider modes are rejected before a run starts.
- **`allowed_environment`** - The only environment variables that may be passed to child processes.
- **`denied_read_paths`** - A denylist of secret or sensitive paths that cannot be read by the orchestrator in agent context.
- **`denied_commands`** - A denylist of commands that cannot be launched, even if they appear in args or flow config.
- **Forbidden args** - Flags that disable approvals, sandboxing, or hook trust wholesale. They are rejected unconditionally.
- **Dangerous diff** - A tracked-file deletion or dependency manifest or lock change that requires explicit approval before tests continue. An operator may exempt specific deletions/renames from the approval via `security.deletion_approval_exempt_paths` (a repo-relative glob allowlist; dependency manifests are never exemptable).
- **`approval`** - The HITL decision shape used for dangerous-diff gating.
- **`network_policy`** - The security ceiling that decides whether a flow may reach the network.
- **`output_policy`** - The containment policy that keeps writing nodes inside the expected report or workspace area.

## Git, artifacts, and recovery

- **`Git Manager`** - The only component that commits, pushes, or opens a PR.
- **`base_branch`** - The branch that the orchestrator returns to after a task is finished.
- **`branch_prefix`** - The prefix used for the default task branch name.
- **`branch_name`** - A full branch override supplied by the task author.
- **`scoped staging`** - Explicit pathspec staging that excludes orchestrator runtime artifacts and task lifecycle folders.
- **`footprint`** - The audit trail policy for how task files and summaries are committed.
- **`audit commit`** - The separate orchestrator-made commit that captures the task file and summary trail.
- **`audit_on_branch`** - Whether the audit commit is attached to the task branch or a sibling branch.
- **`Pull Request` / `PR`** - The publish artifact created by the Git Manager.
- **`publish`** - The terminal Git step that turns the reviewed result into a PR, and optionally auto-merges it when configured.
- **`publish idempotency`** - The guarantee that a rerun does not create duplicate commits, pushes, or PRs.
- **`terminal cleanup`** - The safe return of the working tree to `base_branch` after a task reaches a terminal status.
- **`workspace/repo`** - The dedicated clone or workspace used for agent edits.
- **`<repo>/.worc/`** - The orchestrator runtime home. It holds config, logs, checks, workspace state, and other generated files.
- **State Store** - The SQLite-backed persistence layer around `state.db` that stores task state and run records.
- **Ledger** - The append-only completed-task record in `completed.jsonl`.
- **`state.db`** - The SQLite state store that keeps task progress and run bookkeeping.
- **`logs/<task-id>/`** - The per-task artifact root.
- **`summary.md`** - The human-readable handoff artifact written at task close; it doubles as the PR body in the default publish flow.
- **`summary.json`** - The local-only machine-readable companion to `summary.md`.
- **`failure_report.json`** - The structured report written when a task ends in failure or becomes stuck.
- **`stuck.md`** - The human-readable stuck-state note for exhausted fix budgets.
- **`completed.jsonl`** - The append-only ledger of terminal tasks.
- **`node_runs`** - The persisted per-node execution records.
- **`provider_attempts`** - The persisted per-attempt records for provider routing and fallback.
- **`check_runs`** - The persisted records for quality-gate execution.
- **`evaluations`** - The immutable advisory verdict table written by evaluators and the supervisor.
- **`publish_operations`** - The idempotency ledger for commit / push / PR operations, so reruns do not duplicate them.
- **`editing_lineage`** - The durable per-execution-unit session table for workspace-writing agent nodes.
- **`node_lineage`** - The durable own-session table for evaluators and the supervisor.
- **`flow_fingerprint`** - The hash of the resolved flow snapshot used to detect resume drift.
- **`ExecutionUnit`** - The `(task_id, subtask_order)` identity for a root task or decomposed subtask.
- **`active_subtask`** - The current 1-based subtask number on the task row when a task is decomposed, paired with `subtask_count` as the total number of accepted subtasks.
- **`skill_map.json`** - The persisted per-node skill map (operator pins ∪ accepted supervisor proposal), restored on resume without re-proposing.
- **`rerun`** - The command or workflow that re-attempts a terminal task.
- **`resume`** - The crash-recovery path that continues an in-flight task from persisted state.
- **`finalize`** - The workflow that records a manually handled terminal outcome.
- **`current.diff`** - The redacted diff artifact used during fallback and review.

## Notifications and observability

- **`Notifier`** - The narrow interface for terminal notifications and two-phase HITL interactions.
- **`Telegram`** - The optional transport implementation used for human-in-the-loop prompts and notifications.
- **HITL** - Human in the loop. It means the orchestrator asks a real person for a question or approval before continuing.
- **`AskHandle`** - The durable, secret-free handle stored before waiting for a human response.
- **`AskResult`** - The structured result returned when a human interaction resolves.
- **`question`** - A free-form human prompt that expects text input.
- **`approval`** - A yes/no human prompt that expects a decision.
- **`OutputContract`** - The per-node typed-output selector for agent nodes: `none`, `human_input`, or `planning`.
- **`HumanInputSignal`** - The typed question/approval payload parsed out of structured output.
- **`TypedStageOutput`** - The validated structured result for an agent node, including `content` and optional `human_input`.
- **`contacts`** - Plain-text mentions appended to messages sent through the notifier.
- **`prompt_audit`** - The per-step record of what prompt went to which actor.
- **`heartbeat`** - A progress pulse emitted during long provider, check, or Git operations.
- **Structured logging** - Secret-free operator logs that include task, stage, attempt, and provider context.
- **Secret redaction** - The scrub layer that removes secrets before logs, artifacts, or summaries are written.
- **Environment-derived secret** - A secret loaded from the orchestrator's own environment or from `<repo>/.worc/.env`, not from task files or config values.
- **`human_input_path`** - The artifact path that carries a human answer or approval back into the flow.
- **`manual_action_required`** - The terminal state used when the orchestrator must stop and ask the operator to intervene.

## Legacy and renamed terms TODO: Need to remove from the solution and any mentions

- **`Stage` enum** - A legacy vocabulary for task lifecycle identity. The current execution identity is the flow node id, not the old stage enum.
- **`agents.routing`** - A removed stage-keyed provider-routing block. Routing is now node-based.
- **`agents.skip_stages`** - A removed global skip list.
- **`agents.allow_review_skip`** - A removed review-specific skip gate.
- **`prompts` block** - A removed config block for prompt overrides.
- **`decompose`** - A removed task flag for decomposition.
- **`footprint.location`**, **`footprint.tracking`**, **`footprint.external_root`** - Removed footprint keys from older config shapes.
- **`min_size_signal`** and **`commit_per_subtask`** - Removed decorative decomposition keys that are no longer read.
- **`summary stage`** - The old name for the idea that is now handled by the constant supervisor layer and the final `summary.md` artifact.
