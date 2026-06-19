# Functional Block Registry

Statuses: `discovered` — identified, not yet investigated; `in-progress` — under analysis; `documented` — investigated and documented; `needs-review` — behavior cannot be unambiguously reconstructed; `excluded` — reviewed, but not a standalone block.

All 27 functional blocks (B01–B27) have been investigated and carry the status `documented`. Each block is described in a dedicated file under `blocks/` and is confirmed with references to executable code and tests.

> In addition to blocks, there is an **execution flows** layer — documents `S01`–`S08` (one per pipeline stage) and an overview in [flows/coding/index.md](./flows/coding/index.md). These describe "what happens at a step" and reference the blocks. Other flows (`flows/deep_research/`, etc.) will be added alongside them in the future.

---

## Interface and Launch Control

### B01 — CLI and Operator Commands

- **Purpose:** argument parsing, subcommand dispatch, return codes; thin command drivers for `run`, `status`, `preflight`, `telegram-test`, `rerun`, `finalize`.
- **Entry points:** [cli.py main](../../src/wastech_orchestrator/cli.py#L1497), [build_parser](../../src/wastech_orchestrator/cli.py#L114), `cmd_run`/`cmd_status`/`cmd_preflight`/`cmd_telegram_test`/`cmd_rerun`/`cmd_finalize`.
- **Dependencies:** B06 (orchestrator), B05/B04 (config loading and discovery), B07 (read-only `status`), B25 (isolation in preflight), B26 (telegram-test/preflight).
- **Status:** `documented` · [file](./blocks/B01-cli-and-operator-commands.md)

### B02 — Watch Daemon and Task Scheduling

- **Purpose:** periodic discovery of pending tasks and submission to the orchestrator one at a time; daemonization with a PID file, graceful shutdown on `SIGTERM`, protection against a second daemon instance.
- **Entry points:** [cmd_watch](../../src/wastech_orchestrator/cli.py#L1160), `cmd_stop`, `cmd_restart`, [watch_loop](../../src/wastech_orchestrator/cli.py#L807)/[watch_once](../../src/wastech_orchestrator/cli.py#L778); [process_control.py](../../src/wastech_orchestrator/process_control.py).
- **Dependencies:** B06 (`resume`, `acquire_slot`, `run_task`, `refresh_repo`), B05.
- **Status:** `documented` · [file](./blocks/B02-watch-daemon-and-scheduling.md)

### B03 — Installer and Project Scaffolding

- **Purpose:** `install` (wizard → generate valid `config.yaml`, scaffold `<repo>/.worc/` + repo-root `tasks/`, gitignore `.worc/`), `upgrade-config`/`upgrade-docs`.
- **Entry points:** [cmd_install](../../src/wastech_orchestrator/cli.py#L1298), [install/wizard.run_wizard](../../src/wastech_orchestrator/install/wizard.py#L68), [install/config_writer.build_and_validate](../../src/wastech_orchestrator/install/config_writer.py#L182), [install/detect.py](../../src/wastech_orchestrator/install/detect.py).
- **Dependencies:** B05 (validation of generated config), B04 (config discovery for upgrades), B19 (git probes), B22 (`append_runtime_excludes`), B25 (denied commands in defaults).
- **Status:** `documented` · [file](./blocks/B03-installer-and-scaffolding.md)

### B04 — Config Discovery

- **Purpose:** resolve the config path by priority (`--config` → `<repo-root>/.worc/config.yaml`, discovered by walking up to the Git root). No persistent registry.
- **Entry points:** [cli.resolve_config_path](../../src/wastech_orchestrator/cli.py#L411).
- **Dependencies:** B03 (`detect.git_info` in `resolve_config_path`).
- **Status:** `documented` · [file](./blocks/B04-install-registry-and-config-discovery.md)

### B05 — Configuration: Schema, Loading, Validation, Upgrade

- **Purpose:** typed configuration model, YAML parsing (fail-closed), semantic validation (§11/§21.4), key migration between schema versions.
- **Entry points:** [config/loader.load_config/loads_config](../../src/wastech_orchestrator/config/loader.py), [config/validation.validate_config](../../src/wastech_orchestrator/config/validation.py), [config/upgrade.py](../../src/wastech_orchestrator/config/upgrade.py), [config/schema.py](../../src/wastech_orchestrator/config/schema.py).
- **Dependencies:** B25 (`find_forbidden_args` in validation), B23 (`checks.model` predicates), PyYAML.
- **Status:** `documented` · [file](./blocks/B05-configuration.md)

---

## Orchestration Core

### B06 — Orchestrator Pipeline

- **Purpose:** deterministic driver for a single task from validation through publishing and terminal cleanup; calls only the Router, Check Runner, and Git Manager; context is passed to agents as paths only.
- **Entry points:** [Orchestrator](../../src/wastech_orchestrator/core/orchestrator.py#L294), [run_task](../../src/wastech_orchestrator/core/orchestrator.py#L350), `resume`, `rerun_task`/`continue_task`, `finalize_task`, factory [build_orchestrator](../../src/wastech_orchestrator/core/orchestrator.py#L2594).
- **Dependencies:** almost all core and execution blocks (see index).
- **Status:** `documented` · [file](./blocks/B06-orchestrator-pipeline.md)

### B07 — State Machine and State Store

- **Purpose:** canonical statuses and valid transitions (§8); persistent state in SQLite, single processing slot, transactions, DB schema versioning, read-only mode.
- **Entry points:** [core/state_machine.py](../../src/wastech_orchestrator/core/state_machine.py) (`Status`, `assert_transition`, `is_active`/`is_terminal`), [state_store.StateStore](../../src/wastech_orchestrator/state_store.py) (`open`/`open_readonly`, `transaction`, `set_status`, `find_active_tasks`, `update_task`, …).
- **Dependencies:** `sqlite3`; used by B06 and B22.
- **Status:** `documented` · [file](./blocks/B07-state-machine-and-store.md)

### B08 — Ledger and Failure Reports

- **Purpose:** append-only log of terminal outcomes (`completed.jsonl`); generation of `failure_report.json`/`stuck.md` and a compact `summary.{md,json}` when no agent is present.
- **Entry points:** [ledger.Ledger](../../src/wastech_orchestrator/ledger.py) (`append`/`records`/`has_task_id`), `write_failure_report`, `write_minimal_summary`.
- **Dependencies:** stdlib only (`json`); read by B16/B06 (id dedup), B06 (attempt counter).
- **Status:** `documented` · [file](./blocks/B08-ledger-and-failure-reports.md)

### B09 — Fix Loop Control

- **Purpose:** persisted per-task loop counters (`LoopCounters`); the fix-loop budgets (test/review fix attempts) are enforced generically by the FlowEngine over `FlowRunState.loop_counters`, not by a dedicated controller.
- **Entry points:** [core/loop_control.py](../../src/wastech_orchestrator/core/loop_control.py) (`LoopCounters`); engine budget bookkeeping in [core/flow/engine.py](../../src/wastech_orchestrator/core/flow/engine.py).
- **Dependencies:** `agents.*` limits from configuration; used by B06.
- **Status:** `documented` · [file](./blocks/B09-fix-loop-control.md)

### B10 — Recovery and Resume

- **Purpose:** reconciliation of persistent state on startup and deciding what to do with an unfinished task (do nothing / mark as manual / complete cleanup / resume from a stage).
- **Entry points:** [core/recovery.py](../../src/wastech_orchestrator/core/recovery.py) (`RecoveryReconciler.reconcile`, `RecoveryAction`, `RecoveryPlan`).
- **Dependencies:** B07 (state), B22 (git state); used by B06 (`resume`).
- **Status:** `documented` · [file](./blocks/B10-recovery-and-resume.md)

### B11 — Task Decomposition

- **Purpose:** decision to split a task into subtasks based on structured planning output; subtask specifications and their file artifacts; progress index.
- **Entry points:** [core/decomposition.py](../../src/wastech_orchestrator/core/decomposition.py) (`decide_decomposition`, `SubtaskSpec`, `write_subtask_artifacts`, `update_subtask_index`).
- **Dependencies:** B15/B12 (structured planning output), B07 (`subtasks`); used by B06.
- **Status:** `documented` · [file](./blocks/B11-task-decomposition.md)

### B12 — HITL and Typed Stage Output

- **Purpose:** durable human-in-the-loop interactions (persist/resume) and parsing/validation of typed stage output (including a signal requesting human input).
- **Entry points:** [core/hitl.py](../../src/wastech_orchestrator/core/hitl.py) (`write_waiting_interaction`, `load_interaction`, `parse_typed_stage_output`, `stage_output_schema`, `consume_pending_interactions`, …).
- **Dependencies:** B26 (transport), B20/B21 (artifacts, redaction); used by B06.
- **Status:** `documented` · [file](./blocks/B12-hitl-and-typed-output.md)

### B13 — Skill Inventory and Selection

- **Purpose:** read-only scanning of `SKILL.md` in the repository; resolving skills proposed by planning (an agent cannot select a path that the scan did not find), and deduplication against operator instructions.
- **Entry points:** [core/skills.py](../../src/wastech_orchestrator/core/skills.py) (`SkillInventoryScanner`, `resolve_planning_skills`, `compute_skill_dedup`).
- **Dependencies:** B25 (`denied_read_paths`); used by B06 (planning).
- **Status:** `documented` · [file](./blocks/B13-skill-selection.md)

### B14 — Dangerous Diff Classifier

- **Purpose:** pure change classifier (file deletions, dependency manifest/lock edits) → requires human approval.
- **Entry points:** [core/dangerous_diff.py](../../src/wastech_orchestrator/core/dangerous_diff.py) (`classify_dangerous_diff`, `DangerousDiff`).
- **Dependencies:** input — `changed_code_entries()` from B22; used by B06 (guardrail) together with B12.
- **Status:** `documented` · [file](./blocks/B14-dangerous-diff-guardrail.md)

### B15 — Prompt Templates and Rendering

- **Purpose:** a flow node's prompt template is the content of its `role_file`; this block is the safe renderer (allowlisted variables — metadata and artifact paths only) plus role-file resolution. No bundled-template store.
- **Entry points:** [core/prompts.py](../../src/wastech_orchestrator/core/prompts.py) (`render_prompt`, `ALLOWED_PROMPT_VARS`), [core/flow/prompt.py](../../src/wastech_orchestrator/core/flow/prompt.py) (`read_role_file`, `render_role_prompt`).
- **Dependencies:** used by B06.
- **Status:** `documented` · [file](./blocks/B15-prompt-templates.md)

---

## Task Ingestion

### B16 — Task Model, Parsing, and Validation Gate

- **Purpose:** `NormalizedTask` model; parsing of `.md`/`.json` (frontmatter + body, failing on duplicate keys); §19 gate (hard checks + completeness classification), quarantine on failure.
- **Entry points:** [task/parser.py](../../src/wastech_orchestrator/task/parser.py) (`read_task_source`, `load_normalized`, `write_normalized`, `slugify`), [task/validation_gate.ValidationGate](../../src/wastech_orchestrator/task/validation_gate.py), [task/model.py](../../src/wastech_orchestrator/task/model.py).
- **Dependencies:** B25 (`scan_frontmatter`), B05 (limits), B08+B07 (id dedup); used by B06.
- **Status:** `documented` · [file](./blocks/B16-task-parsing-and-validation-gate.md)

---

## Execution and Providers

### B17 — Agent Router and Fallback Policy

- **Purpose:** provider selection for a stage (config + validated task-override), launch with fallback only on infrastructure errors, attempt counting, partial diff handoff.
- **Entry points:** [routing/router.AgentRouter](../../src/wastech_orchestrator/routing/router.py) (`resolve_route`, `run_stage`), [routing/snapshots.py](../../src/wastech_orchestrator/routing/snapshots.py) (`SnapshotHook`).
- **Dependencies:** B18 (`run` call), B25 (`is_same_or_stricter`), B05; used by B06.
- **Status:** `documented` · [file](./blocks/B17-agent-router-and-fallback.md)

### B18 — Provider Adapters and Contract (Codex/Claude)

- **Purpose:** `AgentProvider` contract; translating `AgentRunRequest` into a CLI argv, launching, parsing output, classifying errors into `ErrorClass`; `preflight`.
- **Entry points:** [providers/base.py](../../src/wastech_orchestrator/providers/base.py), [providers/claude.ClaudeCodeProvider](../../src/wastech_orchestrator/providers/claude.py), [providers/codex.CodexProvider](../../src/wastech_orchestrator/providers/codex.py), [providers/errors.classify](../../src/wastech_orchestrator/providers/errors.py).
- **Dependencies:** B19 (launch), B20 (artifacts), B21 (redaction), B25 (env/forbidden/isolation); called only by B17.
- **Status:** `documented` · [file](./blocks/B18-agent-providers.md)

### B19 — Safe Subprocess Runner

- **Purpose:** single launch primitive: argv list (no shell), environment allowlist, timeout, stdin text, streaming stdout write to file, stderr capture.
- **Entry points:** [providers/process.run_process](../../src/wastech_orchestrator/providers/process.py).
- **Dependencies:** B25 (`build_child_env`); used by B18, B22, B24, B03/B04 (git probes).
- **Status:** `documented` · [file](./blocks/B19-subprocess-runner.md)

### B20 — Run Artifact Layout

- **Purpose:** deterministic (never-overwrite) artifact layout on disk; writing request/result, sha256, archiving task artifacts on rerun.
- **Entry points:** [providers/artifacts.py](../../src/wastech_orchestrator/providers/artifacts.py) (`task_artifact_dir`, `create_attempt_dir`, `archive_task_artifacts`, `sha256_file`).
- **Dependencies:** stdlib; used by B18, B06.
- **Status:** `documented` · [file](./blocks/B20-artifact-layout.md)

### B21 — Secret Redaction

- **Purpose:** end-to-end scrubbing of secret-like strings (token patterns + sensitive assignments) from text/dictionaries; collection of secrets from `denied_read_paths`.
- **Entry points:** [providers/redaction.py](../../src/wastech_orchestrator/providers/redaction.py) (`redact_text`, `redact_mapping`, `read_denied_secrets`).
- **Dependencies:** stdlib; used by B18, B22, B27, B06, B26.
- **Status:** `documented` · [file](./blocks/B21-secret-redaction.md)

---

## Git

### B22 — Git and GitHub Operations (Git Manager)

- **Purpose:** all git/gh operations via argv without shell: branch `agent/<id>-<slug>`, scoped staging (never `git add .`), commit/push/PR/merge with idempotency, the task-scoped audit commit + the `.worc/` gitignore exclude, working tree snapshots, terminal cleanup.
- **Entry points:** [git_manager.GitManager](../../src/wastech_orchestrator/git_manager.py) (`prepare_branch`, `commit_code`/`commit_subtask`/`commit_audit`, `push`, `create_pr`, `merge_pr`, `terminal_cleanup`, `capture`/`partial_change_since`, …), `append_runtime_excludes`.
- **Dependencies:** B19, B21, B07 (publish_operations), B25 (env); used by B06, B17 (SnapshotHook), B01.
- **Status:** `documented` · [file](./blocks/B22-git-manager.md)

---

## Checks (Quality Gate)

### B23 — Check Discovery and Resolution

- **Purpose:** determine the set of checks to run (deterministically from repository "evidence" or by trusting `checks.commands`; optionally with an agent fallback), cache the profile by fingerprint, invalidate on change; re-resolve on launch error.
- **Entry points:** [checks/resolver.CheckResolver](../../src/wastech_orchestrator/checks/resolver.py) (`resolve`/`reresolve`), [checks/diagnostics.py](../../src/wastech_orchestrator/checks/diagnostics.py), [checks/inspect.py](../../src/wastech_orchestrator/checks/inspect.py), [checks/detect.py](../../src/wastech_orchestrator/checks/detect.py), [checks/probe.py](../../src/wastech_orchestrator/checks/probe.py), [checks/validate.py](../../src/wastech_orchestrator/checks/validate.py), [checks/store.py](../../src/wastech_orchestrator/checks/store.py), [checks/fingerprint.py](../../src/wastech_orchestrator/checks/fingerprint.py), [checks/agent.py](../../src/wastech_orchestrator/checks/agent.py).
- **Dependencies:** B18 (agent fallback), B19 (probes), B25; used by B06, B01, B03.
- **Status:** `documented` · [file](./blocks/B23-check-discovery.md)

### B24 — Check Execution (testing stage)

- **Purpose:** run resolved checks in order (argv without shell, env allowlist, timeout), stop on first failure, logs, distinction between a launch error and a quality failure.
- **Entry points:** [check_runner.CheckRunner.run](../../src/wastech_orchestrator/check_runner.py) → `CheckOutcome`.
- **Dependencies:** B19, B25 (env), B21; used by B06 (`testing`).
- **Status:** `documented` · [file](./blocks/B24-check-execution.md)

---

## Security

### B25 — Security Policy Enforcement

- **Purpose:** primitives of the non-weakening security policy: environment variable allowlist, bypass-flag prohibition, frontmatter injection scanning, provider isolation preflight, permission-profile strictness ranking (for conditional fallback).
- **Entry points:** [security/env.build_child_env](../../src/wastech_orchestrator/security/env.py), [security/forbidden_args.find_forbidden_args](../../src/wastech_orchestrator/security/forbidden_args.py), [security/injection.scan_frontmatter](../../src/wastech_orchestrator/security/injection.py), [security/isolation.check_isolation](../../src/wastech_orchestrator/security/isolation.py), [security/profiles.is_same_or_stricter](../../src/wastech_orchestrator/security/profiles.py).
- **Dependencies:** stdlib; used by B18, B19, B22, B24, B17, B16, B05, B06, B01.
- **Status:** `documented` · [file](./blocks/B25-security-policy.md)

---

## Integrations and Cross-Cutting Services

### B26 — Notifications and HITL Transport (Telegram)

- **Purpose:** `Notifier` contract; Telegram implementation: sending a correlated request, polling for a response with timeout, fire-and-forget notifications; `NullNotifier` when the transport is disabled or not configured; Telegram preflight.
- **Entry points:** [notify/interface.py](../../src/wastech_orchestrator/notify/interface.py) (`Notifier`, `NullNotifier`, `AskResult`/`AskHandle`), [notify/telegram.py](../../src/wastech_orchestrator/notify/telegram.py) (`build_notifier`, `check_telegram_preflight`).
- **Dependencies:** `python-telegram-bot`, B21, B05 (`telegram.*`); used by B06, B12, B01.
- **Status:** `documented` · [file](./blocks/B26-notifications-telegram.md)

### B27 — Observability: Logging and Heartbeat

- **Purpose:** structured logging without secrets (logfmt/json, file rotation, redaction filter, context binding) and heartbeat messages during long blocking operations.
- **Entry points:** [observability/logging.py](../../src/wastech_orchestrator/observability/logging.py) (`configure_logging`, `bind`, `RedactionFilter`), [observability/progress.run_with_heartbeat](../../src/wastech_orchestrator/observability/progress.py).
- **Dependencies:** B21 (redaction filter); used by B06, B18, B22, B24, B01.
- **Status:** `documented` · [file](./blocks/B27-observability.md)

---

## Reviewed but Not Extracted as Standalone Blocks (`excluded`)

- **`providers/errors.py`** — included in B18 (adapter error classification rules).
- **`routing/snapshots.py`** — included in B17 (partial diff contract between Router and Git).
- **`checks/model.py`, `checks/profile.py`, `checks/schema_validate.py`, `checks/discovery_factory.py`, `checks/fingerprint.py`** — parts of B23 (check discovery models/schemas/factory).
- **`templates/`, `worc/` (markdown)** — package data delivered by B03; not executable code. The packaged flow role files live separately under `core/flow/packaged/roles/` (read by B15).
- **`__init__.py` packages, `__main__.py`** — re-exports/wrappers of entry points (reflected in B01).
