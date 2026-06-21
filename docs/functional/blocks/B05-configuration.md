# B05 — Configuration: Schema, Loading, Validation, Upgrade

> Reconstructed from code (`config/schema.py`, `config/loader.py`, `config/validation.py`, `config/upgrade.py`) and tests (`tests/config/`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/config/schema.py`, `src/wastech_orchestrator/config/loader.py`, `src/wastech_orchestrator/config/validation.py`, `src/wastech_orchestrator/config/upgrade.py`

## Responsibility

Own the typed model of `config.yaml` and its full ingestion lifecycle: the frozen-dataclass shapes ([schema.py](../../../src/wastech_orchestrator/config/schema.py)), fail-closed structural parsing of YAML into an `OrchestratorConfig` ([loader.py](../../../src/wastech_orchestrator/config/loader.py)), semantic cross-field validation ([validation.py](../../../src/wastech_orchestrator/config/validation.py)), and add-missing-only key migration between schema versions ([upgrade.py](../../../src/wastech_orchestrator/config/upgrade.py)). The module is pure: no config-file discovery, no atomic write/backup, no import-time side effects, and it never touches CLI syntax — that is each consumer block's concern.

The split is deliberate: `schema.py` holds _shapes only_ (no parsing, no validation, no CLI syntax — [schema.py:4-7](../../../src/wastech_orchestrator/config/schema.py#L4)); `loader.py` owns _structural_ parsing only and the cross-field rules live in `validation.py` ([loader.py:8-9](../../../src/wastech_orchestrator/config/loader.py#L8)).

## Public surface

- `CONFIG_SCHEMA_VERSION = 11` ([schema.py:57](../../../src/wastech_orchestrator/config/schema.py#L57)) — the `config.yaml` _format_ version; the loader refuses anything newer.
- `OrchestratorConfig` ([schema.py:301-317](../../../src/wastech_orchestrator/config/schema.py#L301)) — the top-level frozen dataclass: `orchestrator`, `repo`, `agents`, `security`, `validation`, `checks`, `git`, `telegram`, `skills`, `supervisor`, plus the top-level `prompt_audit: bool`.
- `ProviderConfig` ([schema.py:128-142](../../../src/wastech_orchestrator/config/schema.py#L128)) — per-provider shape: `command`, `model`, `timeout_seconds`, `permission_profile`, `extra_args`, `sandbox`, `max_turns`, `max_budget_usd`, `reasoning`, and `primary`.
- `SKIPPABLE_STAGES` ([schema.py:65-72](../../../src/wastech_orchestrator/config/schema.py#L65)) — the frozenset of stages a task may disable per-task via `stages.<stage>.enabled: false`.
- `loads_config(text, *, source)` / `load_config(path)` → `ConfigLoadResult` ([loader.py:689](../../../src/wastech_orchestrator/config/loader.py#L689), [loader.py:705](../../../src/wastech_orchestrator/config/loader.py#L705)) — structural parse; raises `ConfigError`.
- `ConfigError(issues)` ([loader.py:61-69](../../../src/wastech_orchestrator/config/loader.py#L61)) — carries _every_ problem found, not just the first.
- `ConfigLoadResult(config, warnings)` ([loader.py:72-77](../../../src/wastech_orchestrator/config/loader.py#L72)) — the parsed config plus non-fatal warnings.
- `validate_config(config)` → `list[str]` ([validation.py:66](../../../src/wastech_orchestrator/config/validation.py#L66)) — semantic gate; raises `ConfigError`, returns warnings.
- `upgrade_config_mapping(template, operator)` → `(merged, added, removed)` ([upgrade.py:64-80](../../../src/wastech_orchestrator/config/upgrade.py#L64)); `parse_mapping` / `packaged_template_mapping` / `render` ([upgrade.py:46](../../../src/wastech_orchestrator/config/upgrade.py#L46), [upgrade.py:54](../../../src/wastech_orchestrator/config/upgrade.py#L54), [upgrade.py:121](../../../src/wastech_orchestrator/config/upgrade.py#L121)).
- `DEFAULT_ALLOWED_ENV` — the `_DEFAULT_ALLOWED_ENV` constant ([loader.py:52-58](../../../src/wastech_orchestrator/config/loader.py#L52)): `PATH`, `HOME`, `USERPROFILE`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`.

## Behavior

### Schema shapes and enums

Every block is a `@dataclass(frozen=True)`, keyed by the canonical enums from `providers.base` ([schema.py:14](../../../src/wastech_orchestrator/config/schema.py#L14)). The shapes mirror `config.example.yaml`: `OrchestratorRuntimeConfig` (`auto_mode`, `poll_interval_seconds`), `RepoConfig`, `AgentsConfig`, `SecurityConfig`, `ValidationConfig`, `ChecksConfig`, `GitConfig`, `TelegramConfig`, `SkillsConfig`, `SupervisorConfig`.

`SKIPPABLE_STAGES` is exactly `{PLANNING, TESTING, REVIEW, FIXING}` ([schema.py:65-72](../../../src/wastech_orchestrator/config/schema.py#L65)). `refinement` is excluded (skipped deterministically by completeness classification, never a task flag); `implementation`/`publishing` are never skippable; `summary` is no longer skippable because the whole-task summary is always written by the constant supervisor layer ([B31](B31-supervisor.md)), not a graph node ([schema.py:59-64](../../../src/wastech_orchestrator/config/schema.py#L59)).

`ProviderConfig.primary` ([schema.py:140-142](../../../src/wastech_orchestrator/config/schema.py#L140)) marks the single global primary: the provider that runs any flow node with no `provider` field and the sole infrastructure-fallback target. `reasoning` is documented as the closed set `{low, medium, high, xhigh, max}` ([schema.py:139](../../../src/wastech_orchestrator/config/schema.py#L139)). Provider-specific optionals: `sandbox` (Codex), `max_turns` / `max_budget_usd` (Claude) ([schema.py:135-138](../../../src/wastech_orchestrator/config/schema.py#L135)).

`SupervisorConfig` ([schema.py:283-298](../../../src/wastech_orchestrator/config/schema.py#L283)) carries only `role_file` (default `roles/supervisor.md`), `model`, `reasoning` — it is oversight above any flow, validated under the same ceiling as a node. `SkillsConfig` ([schema.py:268-280](../../../src/wastech_orchestrator/config/schema.py#L268)) carries `scan_root` and an `exclude` denylist defaulting to `("run-checks", "test", "sync-docs")`. The top-level `prompt_audit` boolean ([schema.py:313-317](../../../src/wastech_orchestrator/config/schema.py#L313)) gates per-step prompt recording; a per-task `prompt_audit` always overrides it.

Other enums: `AuditBranch` (`task`/`sibling`) ([schema.py:75-79](../../../src/wastech_orchestrator/config/schema.py#L75)), `MergeStrategy` (`merge`/`squash`/`rebase`) ([schema.py:234-239](../../../src/wastech_orchestrator/config/schema.py#L234)), `CheckDiscoveryMode` (`auto`/`deterministic`/`configured`/`disabled`) ([schema.py:82-88](../../../src/wastech_orchestrator/config/schema.py#L82)), `CheckRefreshPolicy` (`on_change`/`always`/`never`) ([schema.py:91-96](../../../src/wastech_orchestrator/config/schema.py#L91)).

### Version history (what each bump changed)

`CONFIG_SCHEMA_VERSION` is bumped only when the _format_ changes, not on every release ([schema.py:16-19](../../../src/wastech_orchestrator/config/schema.py#L16)). Confirmed history from the source comments:

- **v2** — added the opt-in `git.auto_merge*` keys ([schema.py:20-21](../../../src/wastech_orchestrator/config/schema.py#L20)).
- **v3** — added the optional `prompts` block (later removed) ([schema.py:22-23](../../../src/wastech_orchestrator/config/schema.py#L22)).
- **v4** — added `agents.skip_stages` / `agents.allow_review_skip` ([schema.py:24-26](../../../src/wastech_orchestrator/config/schema.py#L24)).
- **v5** — added the `skills:` block and `checks.discovery.{run_at_task_start,approve_command_changes}` ([schema.py:27-29](../../../src/wastech_orchestrator/config/schema.py#L27)).
- **v6** — prompt overrides auto-detected by file presence; `prompts.overrides`/`prompts.strict` removed (tolerated/ignored) ([schema.py:30-33](../../../src/wastech_orchestrator/config/schema.py#L30)).
- **v7** — worc-home consolidation; `git.footprint.{location,tracking,external_root}` removed, leaving only `audit_commit_message` + `audit_on_branch` ([schema.py:34-38](../../../src/wastech_orchestrator/config/schema.py#L34)).
- **v8** — added the optional top-level `prompt_audit` flag (default false) ([schema.py:39-41](../../../src/wastech_orchestrator/config/schema.py#L39)).
- **v9** — the `prompts` block (`templates_dir`/`mode`) removed; a node's prompt template is its `role_file` (the _primary_ now) ([schema.py:42-44](../../../src/wastech_orchestrator/config/schema.py#L42)).
- **v10** — the global `agents.skip_stages` list removed (per-task `stages.<stage>.enabled: false` survives; `agents.allow_review_skip` stays) ([schema.py:45-50](../../../src/wastech_orchestrator/config/schema.py#L45)).
- **v11** — provider routing moves onto the flow node: the stage-keyed `agents.routing` block is removed, exactly one `agents.providers.<id>.primary: true` marks the global primary; the per-task auto-merge gate `git.auto_merge_allow_per_task` is also removed (a per-task `auto_merge` now wins outright) ([schema.py:51-56](../../../src/wastech_orchestrator/config/schema.py#L51)).

### Fail-closed structural loading

`loads_config` runs `yaml.safe_load`; a YAML error or a non-mapping root raises `ConfigError` immediately ([loader.py:691-696](../../../src/wastech_orchestrator/config/loader.py#L691)). Otherwise `_parse` assembles each block through typed readers that _collect_ problems into a shared `issues` list and return a safe placeholder on mismatch ([loader.py:80-89](../../../src/wastech_orchestrator/config/loader.py#L80)); only after the whole document is walked does a non-empty `issues` raise `ConfigError(issues)` with the full list ([loader.py:700-702](../../../src/wastech_orchestrator/config/loader.py#L700)). Every typed reader (`_str`, `_int`, `_bool`, `_float`, `_enum`, …) appends one issue per mismatch — so a config with several errors reports them all at once (test `test_all_issues_collected_not_just_first`, [test_loader.py:163-166](../../../tests/config/test_loader.py#L163)).

Unknown keys are rejected, not dropped: `_check_keys` reports `unknown key '<k>'` for any key outside the allowed set ([loader.py:92-104](../../../src/wastech_orchestrator/config/loader.py#L92)) — applied at the top level against `_TOP_LEVEL_KEYS` ([loader.py:633-646](../../../src/wastech_orchestrator/config/loader.py#L633)) and inside every block builder. Bad enum values fail closed too: `_enum` reports `invalid value <v>, expected one of <choices>` ([loader.py:251-264](../../../src/wastech_orchestrator/config/loader.py#L251)), and an unknown provider key in `agents.providers` is an explicit issue ([loader.py:343-348](../../../src/wastech_orchestrator/config/loader.py#L343)). The closed reasoning set `_REASONING_LEVELS = {low, medium, high, xhigh, max}` ([loader.py:51](../../../src/wastech_orchestrator/config/loader.py#L51)) is enforced in three places: provider `reasoning` ([loader.py:318-324](../../../src/wastech_orchestrator/config/loader.py#L318)), `checks.discovery.reasoning` ([loader.py:489-494](../../../src/wastech_orchestrator/config/loader.py#L489)), and `supervisor.reasoning` ([loader.py:620-625](../../../src/wastech_orchestrator/config/loader.py#L620)).

A partial config still loads: each reader supplies a §11 default mirroring `config.example.yaml` (e.g. `poll_interval_seconds` → 300, provider `timeout_seconds` → 7200, `denied_commands` → the four-command default).

### The schema_version gate (refuse newer)

`_check_schema_version` ([loader.py:649-666](../../../src/wastech_orchestrator/config/loader.py#L649)) enforces the one-directional gate: an absent `schema_version` is accepted (the future-migration hook), a value `<= CONFIG_SCHEMA_VERSION` is accepted, and a value _greater_ than 11 raises `schema_version <n> is newer than this orchestrator supports (11); upgrade wastech-orchestrator` ([loader.py:662-666](../../../src/wastech_orchestrator/config/loader.py#L662)). A non-integer (or bool) value is itself an error ([loader.py:659-661](../../../src/wastech_orchestrator/config/loader.py#L659)). `schema_version` is config metadata only: it is validated here and **not** stored on `OrchestratorConfig` ([loader.py:653-655](../../../src/wastech_orchestrator/config/loader.py#L653)). Confirmed by `test_newer_schema_version_is_refused` and `test_current_and_absent_schema_version_load` ([test_config_schema_version.py:16-24](../../../tests/config/test_config_schema_version.py#L16)).

### Tolerated (removed) keys — fail-open, never rejected

Removed-but-legacy keys are _tolerated_: neither accepted into the schema nor reported as errors, so an old config still loads. `_check_keys` takes a `tolerated` set for this ([loader.py:92-104](../../../src/wastech_orchestrator/config/loader.py#L92)). Tolerated keys: top-level `prompts` (v9) ([loader.py:670-672](../../../src/wastech_orchestrator/config/loader.py#L670)); `agents.skip_stages` (v10) and `agents.routing` (v11) ([loader.py:383-400](../../../src/wastech_orchestrator/config/loader.py#L383)); `git.auto_merge_allow_per_task` (v11) ([loader.py:556-571](../../../src/wastech_orchestrator/config/loader.py#L556)). Tests confirm each loads fail-open and is not stored on the schema (`test_legacy_skip_stages_tolerated_not_error`, `test_legacy_routing_block_is_tolerated`, `test_legacy_auto_merge_allow_per_task_is_tolerated`, `test_legacy_prompts_block_is_tolerated` — [test_loader.py:60-66](../../../tests/config/test_loader.py#L60), [test_loader.py:148-154](../../../tests/config/test_loader.py#L148), [test_loader.py:259-263](../../../tests/config/test_loader.py#L259), [test_loader.py:306-312](../../../tests/config/test_loader.py#L306)).

### Semantic cross-field validation

`validate_config` runs after a successful load and is a second fail-closed gate, also collecting all issues into one `ConfigError` ([validation.py:66-110](../../../src/wastech_orchestrator/config/validation.py#L66)). The confirmed rules:

- **Exactly one global primary, in `agents.allowed`** — `_check_global_primary` counts providers with `primary: true`; `!= 1` is rejected (`exactly one provider must set primary: true`), and the single primary must be in `agents.allowed` ([validation.py:44-63](../../../src/wastech_orchestrator/config/validation.py#L44)). Tests cover zero, two, and not-in-allowed ([test_validation.py:28-53](../../../tests/config/test_validation.py#L28)).
- **Forbidden args** — every provider's `extra_args` is screened by `find_forbidden_args` from [B25](B25-security-policy.md), reused so the config-time and run-time checks agree; any sandbox/approval-weakening flag is a config issue ([validation.py:32-41](../../../src/wastech_orchestrator/config/validation.py#L32), [validation.py:100-102](../../../src/wastech_orchestrator/config/validation.py#L100)). Tests reject `--dangerously-bypass-approvals-and-sandbox`, `--sandbox=danger-full-access` (joined and split forms), and `--dangerously-skip-permissions` ([test_validation.py:71-104](../../../tests/config/test_validation.py#L71)).
- **Loop limits** — `max_total_fix_iterations >= max_fix_cycles` ([validation.py:87-91](../../../src/wastech_orchestrator/config/validation.py#L87)); `poll_interval_seconds >= 0` ([validation.py:80-84](../../../src/wastech_orchestrator/config/validation.py#L80)); `decomposition.max_subtasks >= 2` ([validation.py:94-98](../../../src/wastech_orchestrator/config/validation.py#L94)).
- **Check coherence** — `checks.timeout_seconds > 0`; each command is normalized via `checks.model.normalize_check_command` and rejected if it carries a shell metacharacter, a forbidden arg, or matches a `security.denied_commands` entry ([validation.py:141-169](../../../src/wastech_orchestrator/config/validation.py#L141)); a blank legacy string is a tolerated no-op ([validation.py:155-156](../../../src/wastech_orchestrator/config/validation.py#L155)). `checks.discovery.timeout_seconds > 0`, and a configured `discovery.provider` must be both in `agents.allowed` and have an `agents.providers` entry ([validation.py:171-187](../../../src/wastech_orchestrator/config/validation.py#L171)). `discovery.mode == disabled` produces a **warning**, not an error ([validation.py:188-191](../../../src/wastech_orchestrator/config/validation.py#L188), test `test_disabled_mode_warns_not_errors` [test_checks_discovery.py:118-121](../../../tests/config/test_checks_discovery.py#L118)).
- **Supervisor path containment** — `supervisor.role_file` must not contain `..` or be absolute ([validation.py:113-124](../../../src/wastech_orchestrator/config/validation.py#L113)) — the same rule the flow validator applies to a node `role_file`.
- **Telegram** — `ask_timeout_s > 0` and `bot_token_env`/`chat_id_env` are valid environment-variable names ([validation.py:127-138](../../../src/wastech_orchestrator/config/validation.py#L127)).

### Config upgrade (add-missing-only)

`upgrade_config_mapping` ([upgrade.py:64-80](../../../src/wastech_orchestrator/config/upgrade.py#L64)) recursively merges the packaged template into the operator mapping: at every existing leaf the **operator value wins**, nested mappings merge so a new sub-key is added without touching siblings, operator-only keys are preserved, and template-ordered keys come first so additions land in the documented order ([upgrade.py:98-118](../../../src/wastech_orchestrator/config/upgrade.py#L98)). Removed keys (`_REMOVED_KEYS`: `prompts`, `agents.skip_stages`, `agents.routing`, `git.auto_merge_allow_per_task`) are stripped in place ([upgrade.py:31-36](../../../src/wastech_orchestrator/config/upgrade.py#L31), [upgrade.py:83-95](../../../src/wastech_orchestrator/config/upgrade.py#L83)), and `schema_version` is always stamped to `CONFIG_SCHEMA_VERSION` ([upgrade.py:79](../../../src/wastech_orchestrator/config/upgrade.py#L79)). It is **idempotent**: upgrading the packaged template against itself adds nothing (`test_packaged_template_is_complete_and_self_idempotent` [test_upgrade.py:81-88](../../../tests/config/test_upgrade.py#L81)). `render` re-emits via `yaml.safe_dump` (so inline comments are lost) with a header pointing back to `config.example.yaml` ([upgrade.py:38-43](../../../src/wastech_orchestrator/config/upgrade.py#L38), [upgrade.py:121-126](../../../src/wastech_orchestrator/config/upgrade.py#L121)). The timestamped backup and `--dry-run` belong to the CLI driver (`cmd_upgrade_config`, [B01](B01-cli-and-operator-commands.md)) — this module only computes the merge.

### Load + validate pipeline

```mermaid
flowchart TB
    s(["load_config(path) / loads_config(text)"]) --> y["yaml.safe_load"]
    y -->|"non-mapping / YAML error"| e1["ConfigError"]
    y --> p["_parse: unknown keys + schema_version gate +<br/>block readers (collect ALL issues)"]
    p -->|"issues present"| e1
    p --> res["ConfigLoadResult(config, warnings)"]
    res --> v["validate_config: §11/§21.4 semantics<br/>(one primary, limits, extra_args, checks, telegram, supervisor)"]
    v -->|"violation"| e2["ConfigError (all issues)"]
    v --> ok["config admitted to the pipeline"]
```

## Invariants & guarantees

- **Fail closed, report everything.** A non-mapping root, an unknown key, an unknown provider/enum, a bad type, or a newer `schema_version` is an error — never silently dropped — and every problem is collected before raising ([loader.py:1-6](../../../src/wastech_orchestrator/config/loader.py#L1), [loader.py:700-702](../../../src/wastech_orchestrator/config/loader.py#L700)).
- **Exactly one global primary.** Validation enforces `len(primaries) == 1` and membership in `agents.allowed` — the router relies on this ([validation.py:52-63](../../../src/wastech_orchestrator/config/validation.py#L52)).
- **Security cannot be weakened at load.** `extra_args` and check commands that would disable the sandbox/approvals are rejected at config time using the same `find_forbidden_args` the run-time command builders use ([validation.py:1-6](../../../src/wastech_orchestrator/config/validation.py#L1), [validation.py:32-41](../../../src/wastech_orchestrator/config/validation.py#L32)).
- **Schema shapes are immutable.** All dataclasses are `frozen=True`; `schema.py` carries no parsing or validation logic ([schema.py:4-7](../../../src/wastech_orchestrator/config/schema.py#L4)).
- **Versioning is one-directional and lossless for operators.** Newer is refused; older/absent is accepted; upgrade adds only what is missing and never overwrites an operator value ([loader.py:649-666](../../../src/wastech_orchestrator/config/loader.py#L649), [upgrade.py:64-80](../../../src/wastech_orchestrator/config/upgrade.py#L64)).
- **No import-time side effects.** Loading is an explicit call; only `load_config` (reads a file) and `packaged_template_mapping` (reads packaged data) touch the filesystem.

## Dependencies

- **Uses:** [B25](B25-security-policy.md) (`find_forbidden_args`, reused for `extra_args` and check commands), [B23](B23-check-discovery.md) (`checks.model` — `normalize_check_command`, `argv_matches_denied`, `shell_metachars`), `providers.base` (`ProviderId`, `Stage`), PyYAML.
- **Used by:** [B01](B01-cli-and-operator-commands.md) (`_load_config`, `cmd_upgrade_config` / `upgrade-config` + `upgrade-docs`), [B04](B04-install-registry-and-config-discovery.md) (resolves the path this module then loads), [B06](B06-orchestrator-pipeline.md) (reads `OrchestratorConfig`), [B18](B18-agent-providers.md) (`ProviderConfig` drives the adapters), [B29](B29-flow-definition-and-validation.md) (the flow config-aware validator reads `providers.allowed`/`reasoning`/the permission ceiling), [B31](B31-supervisor.md) (`SupervisorConfig`), [B13](B13-skill-selection.md) (`SkillsConfig`).

## Audit candidates

See [the audit](../../backlog/2026-06-21-audit.md) for the consolidated list.

- `config/schema.py:124-125` ([loader.py:361-362](../../../src/wastech_orchestrator/config/loader.py#L361)) — `agents.decomposition.min_size_signal` and `commit_per_subtask` are parsed and stored but **never read by any runtime code** (only `enabled` and `max_subtasks` are consumed — [orchestrator.py:1159-1161](../../../src/wastech_orchestrator/core/orchestrator.py#L1159), [orchestrator.py:1805](../../../src/wastech_orchestrator/core/orchestrator.py#L1805)). Dead config knobs.
- `templates/config.example.yaml:34` — the comment "advisory threshold passed to the planning prompt" overstates `min_size_signal`: no code injects it into any prompt (grep finds zero readers outside loader/installer). Stale/overstated comment.
- `templates/config.example.yaml:35` — the comment "one local commit per subtask" implies `commit_per_subtask` is a toggle, but subtask commit is unconditional ([orchestrator.py:1038](../../../src/wastech_orchestrator/core/orchestrator.py#L1038)) and the flag is never read. Misleading comment on a dead knob.
- `core/flow/schema.py:133-137` ([snapshot.py:378-381](../../../src/wastech_orchestrator/core/flow/snapshot.py#L378)) — the flow `decomposition.gate` fields `gate_min`/`gate_max`/`linear_depends_on` and `commit_each_subtask` are parsed and validated for structure but never consumed at runtime: the accept gate uses `agents.decomposition.max_subtasks` and enforces `2..n` + linearity unconditionally ([decomposition.py:106-145](../../../src/wastech_orchestrator/core/decomposition.py#L106)). Decorative flow knobs.
- `templates/config.example.yaml:71-74` ([loader.py:428-434](../../../src/wastech_orchestrator/config/loader.py#L428)) — the example's `denied_commands` lists only 3 entries and omits `gh pr merge`, but the loader default (and `test_denied_commands_default_blocks_gh_pr_merge`, [test_loader.py:297-300](../../../tests/config/test_loader.py#L297)) includes it. An operator who copies the example loses the `gh pr merge` denial. Example/default drift.
- `templates/config.example.yaml:83` ([loader.py:462](../../../src/wastech_orchestrator/config/loader.py#L462)) — the example sets `quarantine_folder: "./tasks/rejected"` while the loader default is `"./.worc/tasks/rejected"`; the two disagree on where rejected tasks land. Example/default drift.

## Tests

- [tests/config/test_loader.py](../../../tests/config/test_loader.py) — structural fail-closed paths: non-mapping/empty root, unknown top-level and block keys, type/enum rejection, collect-all-issues, the reasoning closed set, provider/auto-merge/prompt-audit parsing, and all four tolerated-legacy-key paths.
- [tests/config/test_validation.py](../../../tests/config/test_validation.py) — every semantic reject path: the one-global-primary rule (zero/two/not-in-allowed), loop-limit and `max_subtasks` bounds, sandbox/permission-bypass `extra_args`, poll interval, telegram timeout and env-name validation; `test_packaged_config_validates_clean` anchors the packaged config.
- [tests/config/test_config_schema_version.py](../../../tests/config/test_config_schema_version.py) — the `schema_version` gate: newer refused, current/absent/non-integer/packaged behavior.
- [tests/config/test_checks_discovery.py](../../../tests/config/test_checks_discovery.py) — the structured-command union, discovery defaults/parsing, shell-metacharacter and denied-command rejection, `discovery.provider` membership, and the `disabled`-mode warning.
- [tests/config/test_upgrade.py](../../../tests/config/test_upgrade.py) — add-missing merge, operator-value precedence, removed-key stripping (`prompts`, `skip_stages`), idempotence, packaged-template completeness, and render round-trip.
- [tests/config/test_roundtrip.py](../../../tests/config/test_roundtrip.py) — the shipped `config.example.yaml` (packaged and repo-root copies) loads, validates clean, and the two copies are in sync.
