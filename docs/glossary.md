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
- **`run`** - Processes one task file end to end through the orchestrator. Takes the **path** to the task file (e.g. `tasks/pending/my-task.md`), not a task id — it starts a new task from the file. (Lifecycle commands like `rerun`/`status`/`finalize` take the task **id** instead.)
- **`promote`** - Moves a staged task file from `tasks/preparing/` into `tasks/pending/` (atomic rename). Takes a task **id** or file name, or `--all`; a decomposition root pulls its subtask specs along with it. The watcher never scans `tasks/preparing/`, so a draft composed there is never picked up mid-write.
- **`watch`** - Watches the task queue, resumes in-flight work first, then picks pending tasks one at a time. `--queue NAME` serves only that queue.
- **`stop`** - Stops a running `watch` daemon via the **stop ladder**: idle stops with no prompt; a busy daemon is refused unless confirmed (`YES`) or forced — `--force` (soft: finish the current flow node, park at the next checkpoint, then exit) or `--force-full` (hard: kill the daemon and agent now; POSIX groups / Windows tree). A soft stop never hard-kills: on timeout it stays a pending graceful stop (every handle retained, the daemon exits at its next node boundary), and `--force-full` is the only hard rung. `--non-interactive` skips the `YES` prompt and refuses a busy daemon unless a force flag is given.
- **`restart`** - Stops the running watcher (same stop ladder, same `--force`/`--force-full`/`--non-interactive` flags) and starts a fresh one with new flags.
- **`status`** - Shows the active or latest persisted task state without starting work.
- **`top`** - Live, read-only monitor (a client over the daemon, not an engine host): the active task + flow node, a parked/gate-pending marker, the queue-filtered priority-sorted pending queue, recent terminal tasks, and a tail of the daemon `--log-file`; polls `state.db` read-only and quits on `q`. Stdlib-only — no extra needed.
- **`shell`** - Interactive operator console (the `[shell]` extra): a prompt_toolkit REPL. Entry is **passive** — it attaches to a live `watch` daemon or opens idle (the queue is not served on entry; it never auto-spawns or auto-claims). `up`/`watch` starts serving by spawning a daemon and verifying it came up (surfacing the captured startup error on failure); `enqueue`/`promote`/`ps`/`status`/`logs`/`prs`/`merge-task`/`finalize`/`rerun`/`down`/`restart`/`cancel`/`clear` dispatch onto the existing commands (`clear` wipes the console screen). `quit` **detaches** (the daemon and any in-flight task keep running; reopen to reattach) — and now warns and asks for confirmation first when the daemon is still serving the queue. It never hosts the engine itself.
- **`clear`** - Clears the terminal screen and its scrollback (the ANSI screen-wipe `Ctrl+L`/Unix `clear` emits). A visual wipe only — no logs or files are deleted (that is `logs clean`). Available both as a standalone command and as a `worc shell` verb.
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
- **`task_type`** - Dispatch key that selects the flow. Built-ins are `implementation`, `deep_research`, `security_audit`, `merge`, and the content-authoring flows `content_chapter` / `content_translate` / `blog_article` / `blog_article_revise`; operator flows can add more.
- **`branch_name`** - Full task-branch override (only in `new` branch mode). When omitted, the orchestrator derives a branch from `repo.branch_prefix`, `id`, and a slug of `title`.
- **`branch_mode`** (task field) - Where the task's git operations point: `new` (fork a fresh branch, the owned default), `existing` (work in `branch_ref`), or `current` (work in the working tree's current branch as-is). Wins over `repo.branch_mode`. A branch is orchestrator-owned only in `new`.
- **`branch_ref`** - The existing branch a task works in; required iff `branch_mode` is `existing` (a validation error otherwise) and must already exist locally or on the remote.
- **`publish`** (task field) - Downgrade-only cap on the `publish` node: `commit` (stop after commits), `push` (stop before the PR), or `pull_request` (full). Effective scope is `min(flow_policy, publish)`; a no-op on a flow with no PR-publishing node.
- **`trust_level`** (task field) - Per-task override of `security.trust_level`, the approval threshold for the dangerous-diff gate: `strict` gates every deletion/dependency-manifest edit, `auto` gates only a `protected_paths` match. Task value wins; never lowers the hard ceiling and cannot touch `protected_paths`.
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
- **Task lifecycle folders** - `tasks/preparing` is the staging folder the watcher never scans (compose a task there, then `promote`), `tasks/pending` holds queued tasks, `tasks/done` and `tasks/failed` hold terminal tasks, and `.worc/tasks/rejected` is the quarantine folder for invalid tasks. A running task keeps its file in `tasks/pending` — "currently active" is tracked by its `state.db` status, not a folder. The `tasks` root is the default; it is configurable via `paths.tasks_dir` (the subfolder names are fixed).
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
- **`repo.base_branch`** - The branch checked out before task work and, by default, restored after terminal cleanup (see `repo.checkout_base_on_cleanup`).
- **`repo.branch_prefix`** - Prefix for the default task branch name.
- **`repo.branch_mode`** - Instance default for where task git operations point (`new`/`existing`/`current`, default `new`); a per-task `branch_mode` overrides it.
- **`repo.checkout_base_on_cleanup`** - Tri-state (`true`/`false`/unset) gating whether terminal cleanup returns to `base_branch`; unset defers to the branch mode (`new` returns; `existing`/`current` stay). Instance-only.
- **`paths`** - Optional block locating the task lifecycle on disk.
- **`paths.tasks_dir`** - Repo-relative directory holding the `pending`/`done`/`failed` lifecycle subfolders (default `tasks`). Validated repo-relative (no `..`/absolute) and rejected if it lives under the gitignored `.worc/` home; a subpath such as `config/tasks` is allowed.
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
- **`security.trust_level`** - Approval policy for the mid-task dangerous-diff gate: `strict` gates every deletion/dependency-manifest edit, `auto` (fresh-install default) gates only a `protected_paths` match. A per-task `trust_level` overrides it; it never lowers the hard security ceiling. Replaced the removed `deletion_approval_exempt_paths` (config v25).
- **`security.protected_paths`** - Repo-relative globs (same dialect as `checks.command_sets[].paths`) whose files always require approval on any change regardless of `trust_level` — the always-ask floor no level can lower. `config.yaml`-only; default `[]`.
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
- **`supervisor.provider`** - Provider the supervisor layer runs on (`codex`/`claude`); absent → the global primary. Validated ∈ `agents.allowed`, symmetric with flow nodes.
- **`prompt_audit`** - Global default for prompt recording. A per-task value can override it.
- **`tools.default_timeout_seconds`** - Flow-wide default wall-clock timeout for a `tool` node whose own `timeout_seconds` is unset (default `3600`). Optional block; absent → the same default.

## Flow vocabulary

- **Flow** - A validated YAML graph of typed nodes selected by `task_type`. The pipeline is data, not a hardcoded stage loop.
- **Packaged flow** - A built-in flow shipped with the package under `packaged/flows/`. Delivery-only: `install` copies it into `.worc/flows/`, and the orchestrator resolves flows only from there — never from the packaged tree at run time.
- **Operator flow** - A repository-local override under `.worc/flows/<task_type>.yaml`.
- **`implementation`** - The default coding flow that produces a reviewed Pull Request.
- **`deep_research`** - The research flow that produces a documentation-oriented output.
- **`security_audit`** - The audit flow that produces a private control-workspace report.
- **`merge`** - The conflict-resolution flow `worc merge-task` runs (only) when pulling `base_branch` into a task branch conflicts: `conflict_resolution` (agent) → `testing` (checks) with a bounded fix loop, terminating at a no-op `publish` (`policy: none`). It performs no git itself — the orchestrator commits the merge and merges the PR after the flow returns a clean, green tree. Not dispatched for incoming tasks; selected by `git.merge_flow`.
- **`content_chapter`** - A long-form book chapter editor: a read-only scout → editor → the deterministic `check_chapter` prose gate (structure only) → a blocking story critic → a style pass → publish, all rework routed through `fixing`.
- **`content_translate`** - Adapts an approved source-language chapter into an English production file, gated by `check_chapter` (structure plus per-page length limits set in the flow's `args`) and an adaptation critic.
- **`blog_article`** - Writes one new authorial blog article from scratch: a read-only scout → a networked researcher → the writer → the deterministic `check_length` minimum-size floor → a blocking tone/style critic → a polish pass → publish, all rework routed through `fixing`.
- **`blog_article_revise`** - Sibling of `blog_article` that revises an existing article in place instead of writing a new one, with the same shape and gates.
- **Flow node kinds** - `agent` runs an editing or authoring step, `evaluator` reads an artifact and returns a verdict, `checks` runs the quality gate, `tool` runs an operator executable from `.worc/tools/` out-of-process (exit-code / optional-JSON gate), `hitl` asks the human in the loop, and `publish` performs the orchestrator-owned publish step.
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
- **`output_policy`** - The write-containment policy for a flow: a closed set of three — `code_change` (the diff anywhere in the repo is the deliverable), `repository_document` (a `docs/research/<task_id>/` report bundle that must produce `report.md` + `sources.json`), and `private_control_workspace_report` (a `.worc/security-reports/<task_id>/` report that never enters git). The name is a contract, not a description; see [flow-authoring.md → Output policy](flow-authoring.md#output-policy).
- **`publishing`** - The terminal publication policy for a flow, such as PR publishing, documentation PRs, local artifacts, or no publish.
- **`network_policy`** - The flow-wide network grant. Absence means no network by default.
- **`session_scope`** - The provider session intent for a node, such as fresh disposable, editing lineage, or resumable own lineage.
- **`lineage_affinity`** - An `editing_lineage` node's declaration that it joins another node's editing session instead of owning its own. The lineage key is `lineage_affinity or <node id>`, so an affinity-less node owns a lineage named after itself and a node with `lineage_affinity: X` shares lineage `X`. Chains are forbidden — the target must be a lineage owner.
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
- **`ErrorClass`** - The provider failure taxonomy: `binary_not_found`, `unsupported_version`, `invalid_invocation` (the CLI rejected our argv with an argparse/usage error — a bad-argv bug on our side, **not** fallback-eligible, so it surfaces instead of masquerading as an unsupported version), `model_request_invalid` (the provider rejected our model request with a model/schema HTTP 400 — bad request / unsupported schema; **not** fallback-eligible, so it surfaces loudly instead of being misread as a generic `process_crashed` and silently burning the fallback provider), `authentication_failed`, `authorization_failed`, `rate_limited`, `network_unavailable`, `provider_unavailable`, `timeout`, `process_crashed`, `invalid_output`, `permission_denied`, `configuration_error`, `task_failure`, `session_unavailable`, `capability_unavailable` (WRI-002/003 — a required host isolation capability is missing **pre-model**: no supported Claude Bash sandbox on native Windows for a workspace-write node, missing Linux/WSL2 `bubblewrap`+`socat`, or a Codex `codex sandbox` that cannot run / cannot demonstrate the generated profile on this host; a deterministic pre-model infrastructure result that may fall over **only** to a same-or-stricter, self-isolating provider, else `manual_action_required` — never a silent downgrade), and `containment_unverified` (WRI-012 — the provider process tree could not be proven quiescent after the attempt, so an unknown background/reparented descendant may still be writing; a **security / manual-action** condition, deliberately **not** fallback-eligible and **not** park-eligible, routed to `manual_action_required`).
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
- **Dangerous diff** - A tracked-file deletion or dependency manifest or lock change that requires explicit approval before tests continue. `security.trust_level` sets which of those changes actually ask: `strict` gates all of them, `auto` (default) gates none of them (only a `protected_paths` match asks). `security.protected_paths` is the inverse always-ask floor — repo-relative globs whose files require approval on any change at any level.
- **`approval`** - The HITL decision shape used for dangerous-diff gating.
- **`network_policy`** - The security ceiling that decides whether a flow may reach the network.
- **`output_policy`** - The containment policy that keeps writing nodes inside the expected report or workspace area.

## Git, artifacts, and recovery

- **`Git Manager`** - The only component that commits, pushes, or opens a PR.
- **`base_branch`** - The branch that the orchestrator returns to after a task is finished (by default in `new` mode; `repo.checkout_base_on_cleanup` and the branch mode decide whether it returns at all).
- **`branch_prefix`** - The prefix used for the default task branch name.
- **`branch_name`** - A full branch override supplied by the task author.
- **`scoped staging`** - Explicit pathspec staging that excludes orchestrator runtime artifacts and task lifecycle folders.
- **`footprint`** - The audit trail policy for how task files and summaries are committed.
- **`audit commit`** - The separate orchestrator-made commit that captures the task file and summary trail.
- **`audit_on_branch`** - Whether the audit commit is attached to the task branch or a sibling branch.
- **`Pull Request` / `PR`** - The publish artifact created by the Git Manager.
- **`publish`** - The terminal Git step that turns the reviewed result into a PR, and optionally auto-merges it when configured.
- **`publish idempotency`** - The guarantee that a rerun does not create duplicate commits, pushes, or PRs.
- **`terminal cleanup`** - Freeing the single processing slot after a task reaches a terminal status: it returns the working tree to `base_branch` (safely, or fails closed) when the branch mode / `repo.checkout_base_on_cleanup` calls for it, otherwise it leaves HEAD on the working branch.
- **`workspace/repo`** - The dedicated clone or workspace used for agent edits.
- **`<repo>/.worc/`** - The orchestrator runtime home. It holds config, the seeded editable `flows/` (and their `roles/`), the installed `guide/`, logs (check logs included, under `logs/<task-id>/checks/`), workspace state, and other generated files. The typed layout (WRI-004) names three surfaces under it: `control_home` (the operator-editable control plane — `config.yaml`, `flows/`, `tools/`, `guide/`), `private_home` (private runtime state the agent must never read — `state.db`, `logs/`, memory, secrets, process-control files), and `exchange_root` (`<repo>/.worc-io`, the redacted agent-facing exchange). All three resolve under `.worc`/`.worc-io` today.
- **Frozen control bundle** (WRI-010) - A per-task, immutable snapshot of the exact control inputs a task's flow references (the flow YAML, every node `role_file`, the supervisor prompts, and each `tool` node's resolved executable), written to `<private_home>/control-bundles/<task-id>/` at task start. The orchestrator binds the flow runners, supervisor, and tool-node registry to it so no later node reopens live `.worc`, preventing an agent from rewriting a later prompt or tool mid-run. A live control edit during any attempt is detected (after the WRI-012 quiescence barrier) and routed to `manual_action_required` — never provider fallback. `continue`/resume reuses the original bundle (verified against `tasks.control_bundle_digest`); a fresh `rerun`/restart re-freezes from the operator's current control plane.
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
- **`editing_lineage`** - The durable session table for workspace-writing agent nodes, keyed `(task_id, subtask_order, lineage_key)` so one execution unit can hold more than one editing session (one per lineage).
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

## Legacy and renamed terms

- **`Stage` enum** - A legacy vocabulary for task lifecycle identity. The current execution identity is the flow node id, not the old stage enum.
- **`agents.routing`** - A removed stage-keyed provider-routing block. Routing is now node-based.
- **`agents.skip_stages`** - A removed global skip list.
- **`agents.allow_review_skip`** - A removed review-specific skip gate.
- **`prompts` block** - A removed config block for prompt overrides.
- **`decompose`** - A removed task flag for decomposition.
- **`footprint.location`**, **`footprint.tracking`**, **`footprint.external_root`** - Removed footprint keys from older config shapes.
- **`min_size_signal`** and **`commit_per_subtask`** - Removed decorative decomposition keys that are no longer read.
- **`security.deletion_approval_exempt_paths`** - Removed skip-list (config v25). Replaced by the inverse model — `security.trust_level` (which deletions ask at all) plus `security.protected_paths` (the always-ask floor). `upgrade-config` strips the old key; there is no automatic conversion.
- **`summary stage`** - The old name for the idea that is now handled by the constant supervisor layer and the final `summary.md` artifact.
