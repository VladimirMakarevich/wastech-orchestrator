# Functional Block Registry

Statuses: `discovered` — identified, not yet investigated; `in-progress` — under analysis; `documented` — investigated and documented; `needs-review` — behavior cannot be unambiguously reconstructed; `excluded` — reviewed, but not a standalone block.

All 32 functional blocks (B01–B32) carry the status `documented`, (re)built from executable code and tests in the 2026-06-21 reconstruction. Each is described in a dedicated file under `blocks/` with `file:line` evidence. The flow-graph layer is in [flows/](./flows/index.md) (the per-flow node graphs).

---

## Interface and Launch Control

### B01 — CLI and Operator Commands

- **Purpose:** argument parsing, subcommand dispatch, exit codes; thin drivers for the 12 subcommands (`install`, `run`, `watch`, `stop`, `restart`, `preflight`, `telegram-test`, `status`, `upgrade-config`, `upgrade-docs`, `rerun`, `finalize`) + `--version`.
- **Entry points:** `cli.py` `build_parser` / `main` / `cmd_*`; `__main__.py`; console scripts in `pyproject.toml`.
- **Dependencies:** B06 (orchestrator), B02 (watch), B03 (install), B04/B05 (config), B07 (read-only `status`), B25 (isolation preflight), B26 (telegram-test), B29 (preflight `validate_all`).
- **Status:** `documented` · [file](./blocks/B01-cli-and-operator-commands.md)

### B02 — Watch Daemon and Task Scheduling

- **Purpose:** periodic discovery of pending tasks and one-at-a-time submission; daemonization with a PID file, graceful `SIGTERM` shutdown between ticks, protection against a second daemon; auto-mode gating.
- **Entry points:** `cli.py` `watch_loop`/`watch_once`/`cmd_watch`/`cmd_stop`/`cmd_restart`; `process_control.py` (`StopController`, `stop_process`, `is_running`).
- **Dependencies:** B06 (`resume`, `acquire_slot`, `run_task`, `refresh_repo`), B05, B16 (pending files).
- **Status:** `documented` · [file](./blocks/B02-watch-daemon-and-scheduling.md)

### B03 — Installer and Project Scaffolding

- **Purpose:** `install` (wizard → generate valid `config.yaml` with an empty `command_sets`, scaffold `<repo>/.worc/` + repo-root `tasks/`, seed editable copies of the built-in flows + node prompts into `.worc/flows/`, gitignore `.worc/`, auto-preflight); `upgrade-config`/`upgrade-docs`.
- **Entry points:** `cli.py` `cmd_install`; `install/wizard.py` `run_wizard`; `install/config_writer.py` `build_and_validate`; `install/detect.py`.
- **Dependencies:** B01, B04, B05, B29 (preflight validates flows; source of the seeded `.worc/flows/` copies), B22 (`append_runtime_excludes`).
- **Status:** `documented` · [file](./blocks/B03-installer-and-scaffolding.md)

### B04 — Install Registry and Config Discovery

- **Purpose:** resolve the config path (explicit `--config` → `<git-root>/.worc/config.yaml` → none) and the `.worc/` home / repo-root `tasks/` split. (There is no separate "install registry" module — a project is installed iff its `.worc/config.yaml` exists.)
- **Entry points:** `cli.py` `resolve_config_path`/`load_config_for`/`worc_home_for`/`tasks_root_for`; git-root via `install/detect.py` `git_info`.
- **Dependencies:** B01, B03, B05, B07/B20 (the home holds state.db + artifacts).
- **Status:** `documented` · [file](./blocks/B04-install-registry-and-config-discovery.md)

### B05 — Configuration: Schema, Loading, Validation, Upgrade

- **Purpose:** the config shape (`CONFIG_SCHEMA_VERSION = 11`), fail-closed loading (refuse newer, reject unknown keys/enums), semantic validation (exactly one provider `primary`, forbidden-args, coherence), add-missing-only upgrade with backup.
- **Entry points:** `config/schema.py`, `config/loader.py`, `config/validation.py`, `config/upgrade.py`.
- **Dependencies:** B04, B01 (upgrade), B25 (forbidden-args), B29 (config-aware flow validation reads it), B18/B31/B13 (sub-configs).
- **Status:** `documented` · [file](./blocks/B05-configuration.md)

## Orchestration Core

### B06 — Orchestrator Pipeline

- **Purpose:** the single-slot wrapper around the flow engine: gate → slot → flow resolution → isolation/check preflight (normalize command sets) → branch → drive the engine in phases → terminal handling (cleanup, ledger, auto-merge — skipped on an incomplete check gate); `rerun`/`finalize`/`resume`.
- **Entry points:** `orchestrator.py` `Orchestrator`, `run_task` ([:342](../../src/wastech_orchestrator/core/orchestrator.py#L342)), `_drive_via_engine`/`_engine_run`/`_run_phases`, `resume`, `build_orchestrator` ([composition.py:78](../../src/wastech_orchestrator/composition.py#L78)).
- **Dependencies:** B28/B30/B31 (engine, runners, supervisor), B16, B07, B08–B15, B17, B22, B23/B24, B26.
- **Status:** `documented` · [file](./blocks/B06-orchestrator-pipeline.md)

### B07 — State Machine and State Store

- **Purpose:** the pure status machine (statuses + `ALLOWED_TRANSITIONS`) and the SQLite store (schema v10: tasks + flow checkpoint, node_runs, provider_attempts, check_runs, artifacts, publish_operations, subtasks, evaluations, editing_lineage, node_lineage); transactional, fail-closed on schema mismatch.
- **Entry points:** `core/state_machine.py` (`Status`, `assert_transition`); `state_store.py` (`StateStore`, `DB_SCHEMA_VERSION`, `_SCHEMA`).
- **Dependencies:** used by B06, B28 (checkpoint), B30, B22, B10, B01.
- **Status:** `documented` · [file](./blocks/B07-state-machine-and-store.md)

### B08 — Ledger and Failure Reports

- **Purpose:** append-only `completed.jsonl` (one record per terminal; the duplicate-id gate source); `failure_report.json`/`stuck.md`; the deterministic minimal-summary fallback.
- **Entry points:** `ledger.py` (`Ledger`, `LedgerRecord`, `write_failure_report`, `write_minimal_summary`).
- **Dependencies:** B06 (appends), B16 (duplicate-id), B31 (primary summary; this is the fallback), B28 (failure report via `recorder.py`).
- **Status:** `documented` · [file](./blocks/B08-ledger-and-failure-reports.md)

### B09 — Fix Loop Control

- **Purpose:** the shared rework-accounting primitive (`record_rework`) + the operator-facing `LoopCounters`; the actual bounding is generic engine bookkeeping (B28), clamped to `agents.max_fix_cycles` / `max_total_fix_iterations`.
- **Entry points:** `core/loop_control.py` (`LoopCounters`, `record_rework`).
- **Dependencies:** B28 (enforces budgets), B07 (counters), B05 (caps), B06 (`_sync_counters_from_run_state`), B08.
- **Status:** `documented` · [file](./blocks/B09-fix-loop-control.md)

### B10 — Recovery and Resume

- **Purpose:** reconcile persisted state on startup (`NONE`/`RESUME`/`CLEANUP`/`MANUAL`); hydrate the flow checkpoint; fingerprint-mismatch restart; decomposition resume from the first uncommitted subtask.
- **Entry points:** `core/recovery.py` (`RecoveryReconciler`); `core/flow/recorder.py` (`hydrate_run_state`).
- **Dependencies:** B06 (resume entry points), B28 (checkpoint), B07, B22, B29 (re-validate against live config).
- **Status:** `documented` · [file](./blocks/B10-recovery-and-resume.md)

### B11 — Task Decomposition

- **Purpose:** the deterministic acceptance rule (agent recommends, core decides), subtask spec artifacts, and the region-driven fan-out.
- **Entry points:** `core/decomposition.py` (`decide_decomposition`, `write_subtask_artifacts`); `core/flow/postprocess.py` (`read_decomposition`).
- **Dependencies:** B28/B29 (region driving + config), B06 (materialize + fan-out), B07 (subtasks), B10.
- **Status:** `documented` · [file](./blocks/B11-task-decomposition.md)

### B12 — HITL and Typed Node Output

- **Purpose:** the typed node-output schema (selected per node by an `OutputContract`: question/approval + planning skills/subtasks) and the durable interaction-artifact lifecycle that the human gate and dangerous-diff approval build on.
- **Entry points:** `core/hitl.py`; `notify/interface.py` (`AskHandle`/`AskResult`).
- **Dependencies:** B30 (HumanGate + runners), B26 (transport), B14 (guardrail approval), B16.
- **Status:** `documented` · [file](./blocks/B12-hitl-and-typed-output.md)

### B13 — Skill Inventory and Selection

- **Purpose:** bounded `.claude/skills/**/SKILL.md` inventory scan, planning-proposed selection filtered to known/non-excluded names, read-only reference paths downstream.
- **Entry points:** `core/skills.py` (`SkillInventoryScanner`, `resolve_skills`).
- **Dependencies:** B06 (scan + apply), B30 (`skill_paths`), B15 (`{skills_path}`), B25 (denied paths), B05.
- **Status:** `documented` · [file](./blocks/B13-skill-selection.md)

### B14 — Dangerous Diff Classification

- **Purpose:** the pure decision logic (deletion / dependency-manifest / protected-path) over changed paths under the `trust_level` policy + `protected_paths` floor; the guard that applies it lives in the agent node runner (B30).
- **Entry points:** `core/dangerous_diff.py` (`evaluate_diff_gate`, `classify_dangerous_diff`, `DangerousDiff`).
- **Dependencies:** B30 (applies the guard), B12 (approval), B22 (`changed_code_entries`).
- **Status:** `documented` · [file](./blocks/B14-dangerous-diff-guardrail.md)

### B15 — Prompt Templates and Rendering

- **Purpose:** the security-critical renderer that substitutes only allowlisted `{name}` path tokens; the flow-node renderer that reads a node `role_file` with flow-dir containment.
- **Entry points:** `core/prompts.py` (`render_prompt`, `ALLOWED_PROMPT_VARS`); `core/flow/prompt.py` (`render_role_prompt`).
- **Dependencies:** B30 (callers), B29 (role_file traversal validated at load), B13, B05 (v9 removed the `prompts` block).
- **Status:** `documented` · [file](./blocks/B15-prompt-templates.md)

## The Flow Engine (execution spine)

### B28 — Flow Engine and Graph Traversal

- **Purpose:** the single execution model — traverse a validated node graph, route on outcome, charge fix budgets, own all transitions; region confinement for decomposition; the run-state checkpoint.
- **Entry points:** `core/flow/engine.py` (`FlowEngine`, `NodeOutcome`, `FlowRunResult`); `core/flow/engine_driver.py` (`drive_flow`, `partition_decomposition`); `core/flow/run_state.py`.
- **Dependencies:** B29 (snapshot), B30 (runners), B07 (recorder/checkpoint), B09 (`record_rework`), B08 (failure report).
- **Status:** `documented` · [file](./blocks/B28-flow-engine.md)

### B29 — Flow Definition, Registry and Validation

- **Purpose:** the typed flow document + provider-neutral contracts; the fail-closed YAML loader; the registry (`task_type` → snapshot, operator override, `validate_all`); the three-layer fatal validator.
- **Entry points:** `core/flow/schema.py`, `core/flow/contracts.py`, `core/flow/snapshot.py`, `core/flow/registry.py`, `core/flow/validator.py`; packaged `packaged/flows/*.yaml`.
- **Dependencies:** B25 (forbidden-args, profiles), B05 (config-aware layer), B18 (`ProviderId`); used by B28, B06.
- **Status:** `documented` · [file](./blocks/B29-flow-definition-and-validation.md)

### B30 — Flow Node Runners

- **Purpose:** the five node-kind runners (agent/evaluator/checks/hitl/publish) + the shared services/inputs, the human gate, prompt assembly, `output_artifact` post-processing, and output-policy containment.
- **Entry points:** `core/flow/nodes/*.py`, `core/flow/prompt.py`, `core/flow/postprocess.py`, `core/flow/output_policy.py`, `core/flow/wiring.py`.
- **Dependencies:** B17 (router), B24/B32 (checks/checkers), B22 (git), B26 (notifier), B12, B14, B15, B07.
- **Status:** `documented` · [file](./blocks/B30-flow-node-runners.md)

### B31 — Supervisor Oversight Layer

- **Purpose:** the constant per-task advisory layer (not a node) that observes each completed step read-only and writes the whole-task summary at close.
- **Entry points:** `core/supervisor.py` (`Supervisor`, `observe`, `finalize`).
- **Dependencies:** B17 (router), B07 (`evaluations`), B15, B20 (`summary.{md,json}`), B05 (`SupervisorConfig`); driven by B06.
- **Status:** `documented` · [file](./blocks/B31-supervisor.md)

### B32 — Flow Checkers (citation, dependency_scan)

- **Purpose:** the core-owned non-`command_profile` checkers — the deterministic citation-manifest validator (gating) and the argv dependency scanners (evidence, never gates).
- **Entry points:** `core/flow/checkers/citation.py`, `core/flow/checkers/dependency_scan.py`.
- **Dependencies:** B30 (`ChecksNodeRunner` dispatch + `output_policy`), B19 (`run_process`); used by the `deep_research` / `security_audit` flows.
- **Status:** `documented` · [file](./blocks/B32-flow-checkers.md)

## Task Ingestion

### B16 — Task Model, Parsing, and Validation Gate

- **Purpose:** the task model + front-matter allowlist; Phase-A hard reject (quarantine, no branch) and Phase-B completeness (feeds the refinement-skip fact).
- **Entry points:** `task/model.py`, `task/parser.py`, `task/validation_gate.py`.
- **Dependencies:** B06 (gate runs first), B25 (injection scan), B08 (duplicate-id), B07 (duplicate-id + `load_normalized` on resume), B12.
- **Status:** `documented` · [file](./blocks/B16-task-parsing-and-validation-gate.md)

## Execution and Providers

### B17 — Agent Router and Fallback Policy

- **Purpose:** node-based route resolution (declared provider else the single global primary), infrastructure-only fallback, `session_unavailable` same-provider retry, partial-change capture.
- **Entry points:** `routing/router.py` (`AgentRouter`, `resolve_route`, `run_stage`, `fallback_allowed`); `routing/snapshots.py`.
- **Dependencies:** B18 (sole caller), B25 (profile strictness), B07 (provider_attempts); called by B30, B31.
- **Status:** `documented` · [file](./blocks/B17-agent-router-and-fallback.md)

### B18 — Provider Adapters and Contract (Codex/Claude)

- **Purpose:** the `AgentProvider` contract + the `codex`/`claude` adapters: build argv lists, deliver the prompt on stdin, normalize errors, resume sessions — no fallback, no git, no state changes.
- **Entry points:** `providers/base.py`, `providers/claude.py`, `providers/codex.py`, `providers/errors.py`.
- **Dependencies:** B19 (process), B21 (redaction), B20 (artifacts), B25 (forbidden-args), B07 (editing_lineage raw session).
- **Status:** `documented` · [file](./blocks/B18-agent-providers.md)

### B19 — Safe Subprocess Launcher

- **Purpose:** the single argv-without-shell chokepoint (mandatory timeout, isolated env, stdin delivery, stdout-to-file, captured stderr).
- **Entry points:** `providers/process.py` (`run_process`, `ProcessResult`).
- **Dependencies:** used by B18, B22, B24, B32; B25 (env allowlist).
- **Status:** `documented` · [file](./blocks/B19-subprocess-runner.md)

### B20 — Run Artifact File Layout

- **Purpose:** the per-attempt on-disk artifact layout under `logs/<task-id>/...`; never-overwrite; archive-for-rerun; sha256 registration.
- **Entry points:** `providers/artifacts.py` (`task_artifact_dir`, `create_attempt_dir`, `archive_task_artifacts`).
- **Dependencies:** B18 (writers), B21 (redaction), B07 (artifacts table), B24 (check logs).
- **Status:** `documented` · [file](./blocks/B20-artifact-layout.md)

### B21 — Secret Redaction

- **Purpose:** scrub literal + pattern-based secrets before any artifact/log write; normalized (non-secret) session id.
- **Entry points:** `providers/redaction.py` (`redact_text`, `redact_mapping`, `read_denied_secrets`, `normalized_session_id`).
- **Dependencies:** used by B18, B20, B27, B06.
- **Status:** `documented` · [file](./blocks/B21-secret-redaction.md)

## Git

### B22 — Git and GitHub Operations (Git Manager)

- **Purpose:** all git/gh via the safe runner: branch flow, scoped staging, idempotent commit/push/PR/merge, diffs/snapshots, fail-closed terminal cleanup. Git is the orchestrator's sole responsibility.
- **Entry points:** `git_manager.py` (`GitManager`).
- **Dependencies:** B19 (runner), B07 (publish idempotency), B21 (diff redaction); used by B06, B30 (publish node).
- **Status:** `documented` · [file](./blocks/B22-git-manager.md)

## Checks (quality gate)

### B23 — Check Resolution and Selection

- **Purpose:** normalize the operator's `checks.command_sets` into runnable `ResolvedCheckSet`s (the trivial resolver — no discovery, cache, or agent) and select which sets run for a task's diff (the pure, deterministic `select_check_sets`); the canonical check/check-set model + argv-safety predicates. An empty `command_sets` mapping = no gate.
- **Entry points:** `checks/model.py` (`ResolvedCheck`, `ResolvedCheckSet`, `normalize_command_sets`, `is_safe_relpath`, predicates), `checks/resolver.py` (`CheckResolver.resolve`), `checks/selection.py` (`select_check_sets`).
- **Dependencies:** B05 (config shapes + safety predicates at load); used by B24 (executes the selected sets), B30 (checks node selects + runs), B06 (preflight normalizes), B01 (command-set summary).
- **Status:** `documented` · [file](./blocks/B23-check-discovery.md)

### B24 — Check Execution (command-set)

- **Purpose:** run every check in the diff-selected command sets through the safe runner (run-all, no fail-fast), each in its `cwd` under a per-set-or-global timeout, with a `skip_if_unavailable` toolchain probe; aggregate into `CheckOutcome` (quality-fail vs launch-fail vs skip / nothing-ran).
- **Entry points:** `check_runner.py` (`CheckRunner`, `CheckOutcome`, `CheckRunResult`).
- **Dependencies:** B23 (what to run — `ResolvedCheckSet`), B30 (the checks node), B32 (other checkers), B19, B25 (env).
- **Status:** `documented` · [file](./blocks/B24-check-execution.md)

## Security

### B25 — Security Policy Enforcement

- **Purpose:** forbidden-args, env allowlist, front-matter injection scan, offline isolation preflight, profile strictness; the network policy as a flow-ceiling control.
- **Entry points:** `security/forbidden_args.py`, `security/env.py`, `security/injection.py`, `security/isolation.py`, `security/profiles.py`.
- **Dependencies:** B05 (load-time), B18 (build-time), B17 (`is_same_or_stricter`), B29 (flow validator), B16 (injection scan), B06 (preflight).
- **Status:** `documented` · [file](./blocks/B25-security-policy.md)

## Integrations and Cross-Cutting Services

### B26 — Notifications and HITL Transport (Telegram)

- **Purpose:** the `Notifier` protocol (best-effort terminal notice; durable ask), the Telegram transport (env-named credentials, correlated polling), the `NullNotifier` fallback.
- **Entry points:** `notify/interface.py`, `notify/telegram.py`, `notify/__init__.py`.
- **Dependencies:** B12 (interaction shapes), B30 (`NotifierPort`), B06, B21, B05 (telegram config).
- **Status:** `documented` · [file](./blocks/B26-notifications-telegram.md)

### B27 — Observability: Logging and Heartbeat

- **Purpose:** structured logging (logfmt/json, rotation, `RedactionFilter`, task-scoped `bind`), heartbeat threads for long operations, and the per-node prompt-audit JSON.
- **Entry points:** `observability/logging.py`, `observability/progress.py`, `core/flow/observability.py`.
- **Dependencies:** B21 (redaction), B18/B22/B24 (heartbeat), B06 (prompt-audit gate, `_observe`), B30 (`record_run_observability`), B05.
- **Status:** `documented` · [file](./blocks/B27-observability.md)

---

## Module → Block Map

Every module under `src/wastech_orchestrator/` is assigned to exactly one block:

| Module(s) | Block |
| --- | --- |
| `cli.py` (+ `__main__.py`) | B01 (watch parts B02, install parts B03, discovery parts B04) |
| `process_control.py` | B02 |
| `install/wizard.py`, `install/config_writer.py`, `install/detect.py` | B03 (`detect.git_info` also B04) |
| `config/schema.py`, `config/loader.py`, `config/validation.py`, `config/upgrade.py` | B05 |
| `core/orchestrator.py` | B06 |
| `core/state_machine.py`, `state_store.py` | B07 |
| `ledger.py` | B08 |
| `core/loop_control.py` | B09 |
| `core/recovery.py`, `core/flow/recorder.py` | B10 |
| `core/decomposition.py` | B11 |
| `core/hitl.py` | B12 |
| `core/skills.py` | B13 |
| `core/dangerous_diff.py` | B14 |
| `core/prompts.py`, `core/flow/prompt.py` | B15 |
| `task/model.py`, `task/parser.py`, `task/validation_gate.py` | B16 |
| `routing/router.py`, `routing/snapshots.py` | B17 |
| `providers/base.py`, `providers/claude.py`, `providers/codex.py`, `providers/errors.py` | B18 |
| `providers/process.py` | B19 |
| `providers/artifacts.py` | B20 |
| `providers/redaction.py` | B21 |
| `git_manager.py` | B22 |
| `checks/{model,resolver,selection}.py` | B23 |
| `check_runner.py` | B24 |
| `security/{forbidden_args,env,injection,isolation,profiles}.py` | B25 |
| `notify/{interface,telegram,__init__}.py` | B26 |
| `observability/{logging,progress}.py`, `core/flow/observability.py` | B27 |
| `core/flow/{engine,engine_driver,run_state,snapshot}.py` | B28 |
| `core/flow/{schema,contracts,registry,validator}.py`, `packaged/flows/*.yaml` | B29 |
| `core/flow/nodes/*.py`, `core/flow/{postprocess,output_policy,wiring}.py` | B30 |
| `core/supervisor.py` | B31 |
| `core/flow/checkers/{citation,dependency_scan}.py` | B32 |

### Excluded (not standalone blocks)

- Package `__init__.py` files and `py.typed` markers — packaging/exports only.
- `core/flow/__init__.py`, `core/flow/contracts.py` re-exports — vocabulary, part of B28/B29.
- `core/flow/observability.py` — assigned to B27 (the prompt-audit observability path).
