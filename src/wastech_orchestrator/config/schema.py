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
CONFIG_SCHEMA_VERSION = 1

# Stages routed to an agent provider. The remaining stages (``testing``, ``publishing``) are run by
# the orchestrator itself (Check Runner / Git Manager), so they never appear in ``agents.routing``
# (spec §5, §11).
ROUTABLE_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.REFINEMENT,
        Stage.PLANNING,
        Stage.IMPLEMENTATION,
        Stage.REVIEW,
        Stage.FIXING,
        Stage.SUMMARY,
    }
)


class FootprintLocation(StrEnum):
    """Where orchestration/task artifacts live relative to the target repo (spec §21)."""

    EXTERNAL = "external"
    IN_REPO = "in_repo"


class FootprintTracking(StrEnum):
    """How those artifacts are tracked by git (spec §21)."""

    NONE = "none"
    EXCLUDE_LOCAL = "exclude_local"
    COMMIT = "commit"


class AuditBranch(StrEnum):
    """Which branch the audit trail is committed onto (tracking=commit only, spec §21)."""

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
class RouteConfig:
    primary: ProviderId
    fallback: ProviderId | None


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


@dataclass(frozen=True)
class AgentsConfig:
    allowed: tuple[ProviderId, ...]
    max_stage_attempts: int
    max_fix_cycles: int
    max_total_fix_iterations: int
    decomposition: DecompositionConfig
    routing: dict[Stage, RouteConfig]
    providers: dict[ProviderId, ProviderConfig]


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
    location: FootprintLocation
    tracking: FootprintTracking
    external_root: str
    audit_commit_message: str
    audit_on_branch: AuditBranch


@dataclass(frozen=True)
class GitConfig:
    create_pull_request: bool
    pr_base: str
    footprint: FootprintConfig


@dataclass(frozen=True)
class TelegramConfig:
    """Optional Telegram human-in-the-loop and terminal notifications."""

    enabled: bool
    bot_token_env: str
    chat_id_env: str
    ask_timeout_s: int


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
