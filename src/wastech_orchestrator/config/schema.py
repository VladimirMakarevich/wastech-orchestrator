"""Configuration schema (spec §11).

Frozen dataclasses mirroring the config.yaml structure, one per §11 block, keyed by the canonical
enums from ``providers.base``. This module holds *shapes only* — no parsing, no validation, and no
CLI syntax. The loader (``config.loader``) maps YAML into these types; the validator
(``config.validation``) enforces the §11/§21.4 semantic rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from wastech_orchestrator.providers.base import ProviderId, Stage

# The config.yaml format version. Bumped only when the *format* changes (not on every release). The
# loader refuses a config whose ``schema_version`` is newer than this (fail-loud); an absent or
# older value is accepted — the older case is the hook for a future migration runner. See the
# spec's "Versioning & compatibility" section.
# v2: added the opt-in ``git.auto_merge*`` keys (auto-merge bypass). Old (v1/absent) configs omit
# them and take the safe ``false`` defaults — no migration flips anything.
# v3: added the optional ``prompts`` block (operator-customizable stage prompts). Old (v1/v2/absent)
# configs omit it and take the safe defaults (packaged templates, append mode) — no migration.
# v4: added the optional ``agents.skip_stages`` / ``agents.allow_review_skip`` keys (stage-skip
# control). Old configs omit them and take the safe defaults (no skips, review-skip disallowed) — no
# migration flips anything.
# v5 (2026-06-14, post-test-run): adds the optional `skills:` block (§2.1) and the
# `checks.discovery.{run_at_task_start,approve_command_changes}` keys (§1.2). All are
# backward-compatible (absent => safe defaults); `upgrade-config` adds them to an older config.
# v6 (2026-06-14, prompt-templates-simplification): prompt overrides are now auto-detected by file
# presence in `prompts.templates_dir` — the `prompts.overrides` map and `prompts.strict` flag are
# removed, and `prompts.mode` now defaults to `replace`. Legacy `overrides`/`strict` keys are
# tolerated (ignored) on load; `upgrade-config` strips them. Old configs still load fail-open.
# v7 (2026-06-16, worc-home-consolidation): the git footprint collapses to a single canonical
# layout — all runtime files live under the gitignored `<repo>/.worc/` home, while the task file and
# its `<id>.summary.md` stay at the repo root and are audit-committed. The `git.footprint.location`,
# `.tracking`, and `.external_root` keys are removed; `git.footprint` now carries only
# `audit_commit_message` + `audit_on_branch`.
# v8 (2026-06-16, prompt-audit): adds the optional top-level `prompt_audit` flag (default false).
# Old configs omit it and take the safe `false` default — no migration flips anything;
# `upgrade-config` adds it from the packaged template. A per-task `prompt_audit` overrides it.
# v9 (flow-engine P1 Slice 7): the `prompts` block (`templates_dir`/`mode`) is removed — a flow
# node's prompt template is its `role_file`, not a stage-indexed packaged default. `upgrade-config`
# strips an operator's `prompts:` block; old configs still load fail-open (the key is ignored).
# v10 (2026-06-19, flexible-flow stage-skip): the global `agents.skip_stages` list is removed — with
# fully configurable flows, "skip a stage for every task" is redundant (drop the node from the flow,
# or author an operator flow). Per-task `stages.<stage>.enabled: false` survives as a bounded,
# validated toggle (flow-contract §10), and `agents.allow_review_skip` stays (now gating only the
# per-task review skip). `upgrade-config` strips `agents.skip_stages`; old configs still load
# fail-open (the key is tolerated/ignored).
# v11 (2026-06-19, flow-engine PRE.1/PRE.2): provider routing moves onto the flow node. The
# stage-keyed `agents.routing` block is removed — a node declares its own `provider` (else the
# global primary), and exactly one `agents.providers.<id>.primary: true` marks that global primary
# (the sole infra-fallback target). The per-task auto-merge gate `git.auto_merge_allow_per_task` is
# also removed: a per-task `auto_merge` now wins outright (PRE.2). `upgrade-config` strips both dead
# keys; old configs still load fail-open (the keys are tolerated/ignored).
CONFIG_SCHEMA_VERSION = 11

# Stages a task may skip per-task via ``stages.<stage>.enabled: false`` (flow-contract §10 bounded
# exception). ``refinement`` is excluded — it is skipped deterministically by completeness
# classification (``derived.needs_refinement``), never a task flag — and
# ``implementation``/``publishing`` are never skippable (the core work and the output). ``testing``
# is skippable but runs no agent (it is the Check Runner).
SKIPPABLE_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.PLANNING,
        Stage.TESTING,
        Stage.REVIEW,
        Stage.FIXING,
        Stage.SUMMARY,
    }
)


class AuditBranch(StrEnum):
    """Which branch the audit trail is committed onto (spec §21)."""

    TASK = "task"
    SIBLING = "sibling"


class CheckDiscoveryMode(StrEnum):
    """How the check profile is resolved (backlog: automatic check discovery, §9)."""

    AUTO = "auto"  # deterministic detection, then agent fallback when confidence is low
    DETERMINISTIC = "deterministic"  # inspect known project evidence only; never an agent
    CONFIGURED = "configured"  # use checks.commands as-is (the backward-compatible default)
    DISABLED = "disabled"  # explicit no-check mode (a prominent warning + audit record)


class CheckRefreshPolicy(StrEnum):
    """When a cached resolved profile is recomputed (backlog: automatic check discovery, §10)."""

    ON_CHANGE = "on_change"  # rediscover when the discovery-input fingerprint changes
    ALWAYS = "always"
    NEVER = "never"


@dataclass(frozen=True)
class AutoModeConfig:
    enabled: bool


@dataclass(frozen=True)
class OrchestratorRuntimeConfig:
    auto_mode: AutoModeConfig
    # Seconds between `watch` ticks; each tick fetch/pulls base_branch to discover tasks pushed to
    # git, then processes pending. 0 = single-pass (no loop, no periodic sync). See spec §8.3.
    poll_interval_seconds: int


@dataclass(frozen=True)
class RepoConfig:
    url: str
    local_path: str
    base_branch: str
    branch_prefix: str


@dataclass(frozen=True)
class DecompositionConfig:
    enabled: bool
    max_subtasks: int
    min_size_signal: str
    commit_per_subtask: bool


@dataclass(frozen=True)
class ProviderConfig:
    command: str
    model: str
    timeout_seconds: int
    permission_profile: str
    extra_args: tuple[str, ...] = ()
    # Provider-specific (optional): Codex sandbox; Claude max_turns / max_budget_usd.
    sandbox: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    reasoning: str | None = None  # "low" | "medium" | "high" | "xhigh" | "max"
    # Exactly one configured provider must set ``primary: true`` — the global primary that runs any
    # flow node with no ``provider`` field, and the single infrastructure-fallback target (PRE.1).
    primary: bool = False


@dataclass(frozen=True)
class AgentsConfig:
    allowed: tuple[ProviderId, ...]
    max_stage_attempts: int
    max_fix_cycles: int
    max_total_fix_iterations: int
    decomposition: DecompositionConfig
    providers: dict[ProviderId, ProviderConfig]
    # Gate for the high-risk per-task ``review`` skip (no agent quality gate before commit/PR): a
    # task may disable review via ``stages.review.enabled: false`` only when true, else rejected.
    allow_review_skip: bool = False


@dataclass(frozen=True)
class SecurityConfig:
    strict_isolation: bool
    allowed_environment: tuple[str, ...]
    denied_read_paths: tuple[str, ...]
    denied_commands: tuple[str, ...]


@dataclass(frozen=True)
class ValidationConfig:
    max_task_bytes: int
    max_task_lines: int
    max_line_bytes: int
    max_control_ratio: float
    required_fields: tuple[str, ...]
    reject_unknown_fields: bool
    quarantine_folder: str


@dataclass(frozen=True)
class CheckCommandSpec:
    """A structured check command: a logical ``name`` plus an explicit argv list (no shell).

    The backward-compatible alternative to a legacy shell-style string in ``checks.commands``. Both
    forms normalize to ``checks.model.ResolvedCheck`` at consumption time (the loader stays
    shapes-only and does no ``shlex`` splitting).
    """

    argv: tuple[str, ...]
    name: str | None = None


@dataclass(frozen=True)
class CheckDiscoveryConfig:
    """Check discovery policy (backlog: automatic check discovery, §9).

    The defaults are backward compatible: ``configured`` uses ``checks.commands`` as-is. ``install``
    opts new repositories into ``auto``. ``model``/``reasoning``/``provider``/``timeout_seconds``
    parameterize the agent-assisted fallback (a deliberately cheap model + low reasoning)."""

    mode: CheckDiscoveryMode = CheckDiscoveryMode.CONFIGURED
    agent_fallback: bool = True
    refresh: CheckRefreshPolicy = CheckRefreshPolicy.ON_CHANGE
    provider: ProviderId | None = None  # which provider runs discovery; None => first available
    model: str = ""  # a cheap model id for discovery; empty => skip agent fallback
    reasoning: str | None = "low"  # low | medium | high | xhigh | max
    timeout_seconds: int = 120
    # Run discovery inside the state machine at task start (not only at install), so `auto` mode can
    # resolve/agent-assist when the task begins (§1.2). Deterministic install-time discovery stays a
    # cache-warming option. Default on; the agent fallback still only fires in `auto` + opted-in.
    run_at_task_start: bool = True
    # Treat a *changed* set of check commands as a sensitive change: write it to the resolved
    # profile and require human approval on first use (§1.2). Disabling it under auto/deterministic
    # is the operator's call but is logged loudly (it decides what "passing" means).
    approve_command_changes: bool = True


@dataclass(frozen=True)
class ChecksConfig:
    # A backward-compatible union: legacy shell-style strings and/or structured CheckCommandSpec.
    commands: tuple[str | CheckCommandSpec, ...]
    # Per-command timeout for the Check Runner (spec §4.8). The process runner requires a timeout;
    # each ``commands`` entry is launched as an argv list (no shell) and bounded by this value.
    timeout_seconds: int = 7200
    discovery: CheckDiscoveryConfig = field(default_factory=CheckDiscoveryConfig)


@dataclass(frozen=True)
class FootprintConfig:
    """The audit-trail policy. The orchestrator's runtime files always live under the gitignored
    ``<repo>/.worc/`` home; only the task file and its ``<id>.summary.md`` are committed (§21)."""

    audit_commit_message: str
    audit_on_branch: AuditBranch


class MergeStrategy(StrEnum):
    """The ``gh pr merge`` strategy used when ``git.auto_merge`` fires (auto-merge bypass)."""

    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


@dataclass(frozen=True)
class GitConfig:
    create_pull_request: bool
    pr_base: str
    footprint: FootprintConfig
    # --- auto-merge bypass (DANGER: skips the human review gate; all default to the safe value) ---
    # When true, every successfully published PR is merged to ``pr_base`` automatically.
    # The mid-pipeline dangerous-diff approval still fires (auto_merge only affects publishing).
    auto_merge: bool = False
    # Strategy passed to ``gh pr merge`` when a merge fires.
    auto_merge_strategy: MergeStrategy = MergeStrategy.SQUASH
    # False: merge immediately (`gh pr merge`). True: arm GitHub-native auto-merge (`--auto`), which
    # merges only after required status checks pass.
    auto_merge_wait_for_checks: bool = False


@dataclass(frozen=True)
class TelegramConfig:
    """Optional Telegram human-in-the-loop and terminal notifications."""

    enabled: bool
    bot_token_env: str
    chat_id_env: str
    ask_timeout_s: int


@dataclass(frozen=True)
class SkillsConfig:
    """Planning-selected repo skill references (post-test-run §2.1).

    The orchestrator scans ``<scan_root>`` (default ``<repo.local_path>/.claude/skills``) for
    ``*/SKILL.md`` name+description, lets ``planning`` pick the relevant ones, and passes the chosen
    files to downstream stages as read-only reference paths. ``exclude`` is the gate-duplicating
    denylist withheld from planning. Defaults reproduce the no-config behavior (scan the target repo
    clone, exclude the three orchestrator-gate skills). ``scan_root`` empty → the default location.
    """

    scan_root: str = ""
    exclude: tuple[str, ...] = ("run-checks", "test", "sync-docs")


@dataclass(frozen=True)
class OrchestratorConfig:
    orchestrator: OrchestratorRuntimeConfig
    repo: RepoConfig
    agents: AgentsConfig
    security: SecurityConfig
    validation: ValidationConfig
    checks: ChecksConfig
    git: GitConfig
    telegram: TelegramConfig
    skills: SkillsConfig = SkillsConfig()
    # When true, every task records each step's prompt + who-metadata (provider/model/attempt/
    # fallback/status) under `logs/<task-id>/prompt-audit/`. A per-task `prompt_audit` always
    # overrides this (task wins); recording a prompt is not a privilege escalation, so there is no
    # operator gate.
    prompt_audit: bool = False
