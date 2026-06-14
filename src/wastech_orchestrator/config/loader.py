"""Configuration loader (spec §11).

Reads a config.yaml into the typed schema, fail-closed: a non-mapping root, an unknown top-level
or per-block key, an unknown route stage, or a bad enum value is an error — never silently dropped.
Every problem is collected and reported together via a typed :class:`ConfigError` (not bare
strings). Loading is an explicit call with no import-time side effects.

This module owns *structural* parsing only. The cross-field §11/§21.4 semantic rules live in
``config.validation``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from wastech_orchestrator.config.schema import (
    CONFIG_SCHEMA_VERSION,
    ROUTABLE_STAGES,
    SKIPPABLE_STAGES,
    AgentsConfig,
    AuditBranch,
    AutoModeConfig,
    CheckCommandSpec,
    CheckDiscoveryConfig,
    CheckDiscoveryMode,
    CheckRefreshPolicy,
    ChecksConfig,
    DecompositionConfig,
    FootprintConfig,
    FootprintLocation,
    FootprintTracking,
    GitConfig,
    MergeStrategy,
    OrchestratorConfig,
    OrchestratorRuntimeConfig,
    PromptMode,
    PromptsConfig,
    ProviderConfig,
    RepoConfig,
    RouteConfig,
    SecurityConfig,
    SkillsConfig,
    TelegramConfig,
    ValidationConfig,
)
from wastech_orchestrator.providers.base import ProviderId, Stage

# Defaults mirror §11 / the packaged config.example.yaml so a partial config still loads safely.
_DEFAULT_AUDIT_MESSAGE = "chore(orchestrator): audit trail for {task_id}"

_REASONING_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})
_DEFAULT_ALLOWED_ENV: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)


class ConfigError(Exception):
    """A config file is structurally invalid or violates a §11/§21.4 rule.

    Carries *every* problem found (``issues``), not just the first — fail-closed reporting (§11).
    """

    def __init__(self, issues: list[str]) -> None:
        self.issues: list[str] = list(issues)
        super().__init__("; ".join(self.issues) if self.issues else "invalid configuration")


@dataclass(frozen=True)
class ConfigLoadResult:
    """The parsed config plus any non-fatal warnings (e.g. a legacy-config migration)."""

    config: OrchestratorConfig
    warnings: tuple[str, ...]


# --- typed readers (each appends to ``issues`` on a mismatch and returns a safe placeholder) ---


def _mapping(value: Any, where: str, issues: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        issues.append(f"{where}: expected a mapping, got {type(value).__name__}")
        return {}
    return {str(k): v for k, v in value.items()}


def _check_keys(m: Mapping[str, Any], allowed: set[str], where: str, issues: list[str]) -> None:
    for key in sorted(set(m) - allowed):
        issues.append(f"{where}: unknown key {key!r}")


def _str(m: Mapping[str, Any], key: str, default: str, where: str, issues: list[str]) -> str:
    if key not in m:
        return default
    value = m[key]
    if not isinstance(value, str):
        issues.append(f"{where}.{key}: expected a string, got {type(value).__name__}")
        return default
    return value


def _int(m: Mapping[str, Any], key: str, default: int, where: str, issues: list[str]) -> int:
    if key not in m:
        return default
    value = m[key]
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"{where}.{key}: expected an integer, got {type(value).__name__}")
        return default
    return value


def _bool(m: Mapping[str, Any], key: str, default: bool, where: str, issues: list[str]) -> bool:
    if key not in m:
        return default
    value = m[key]
    if not isinstance(value, bool):
        issues.append(f"{where}.{key}: expected a boolean, got {type(value).__name__}")
        return default
    return value


def _float(m: Mapping[str, Any], key: str, default: float, where: str, issues: list[str]) -> float:
    if key not in m:
        return default
    value = m[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        issues.append(f"{where}.{key}: expected a number, got {type(value).__name__}")
        return default
    return float(value)


def _str_tuple(
    m: Mapping[str, Any], key: str, default: tuple[str, ...], where: str, issues: list[str]
) -> tuple[str, ...]:
    if key not in m:
        return default
    value = m[key]
    if not isinstance(value, list):
        issues.append(f"{where}.{key}: expected a list, got {type(value).__name__}")
        return default
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(f"{where}.{key}[{index}]: expected a string, got {type(item).__name__}")
            continue
        out.append(item)
    return tuple(out)


def _check_command_list(
    m: Mapping[str, Any], key: str, where: str, issues: list[str]
) -> tuple[str | CheckCommandSpec, ...]:
    """Read ``checks.commands`` as a backward-compatible union of strings and ``{name, argv}`` maps.

    Structural only — no ``shlex`` splitting here (the canonical normalization lives in
    ``checks.model``). A legacy string is kept verbatim; a mapping must carry a non-empty ``argv``
    list of strings and may carry an optional ``name``.
    """
    if key not in m:
        return ()
    value = m[key]
    if not isinstance(value, list):
        issues.append(f"{where}.{key}: expected a list, got {type(value).__name__}")
        return ()
    out: list[str | CheckCommandSpec] = []
    for index, item in enumerate(value):
        item_where = f"{where}.{key}[{index}]"
        if isinstance(item, str):
            out.append(item)
            continue
        if isinstance(item, Mapping):
            spec = _command_spec({str(k): v for k, v in item.items()}, item_where, issues)
            if spec is not None:
                out.append(spec)
            continue
        issues.append(
            f"{item_where}: expected a string or a {{name, argv}} mapping, "
            f"got {type(item).__name__}"
        )
    return tuple(out)


def _command_spec(m: Mapping[str, Any], where: str, issues: list[str]) -> CheckCommandSpec | None:
    _check_keys(m, {"name", "argv"}, where, issues)
    raw_argv = m.get("argv")
    if not isinstance(raw_argv, list) or not raw_argv:
        issues.append(f"{where}.argv: expected a non-empty list of strings")
        return None
    argv: list[str] = []
    ok = True
    for ai, token in enumerate(raw_argv):
        if not isinstance(token, str):
            issues.append(f"{where}.argv[{ai}]: expected a string, got {type(token).__name__}")
            ok = False
            continue
        argv.append(token)
    if not ok:
        return None
    name = m.get("name")
    if name is not None and not isinstance(name, str):
        issues.append(f"{where}.name: expected a string, got {type(name).__name__}")
        name = None
    return CheckCommandSpec(argv=tuple(argv), name=name)


def _opt_str(m: Mapping[str, Any], key: str, where: str, issues: list[str]) -> str | None:
    if key not in m or m[key] is None:
        return None
    value = m[key]
    if not isinstance(value, str):
        issues.append(f"{where}.{key}: expected a string, got {type(value).__name__}")
        return None
    return value


def _opt_int(m: Mapping[str, Any], key: str, where: str, issues: list[str]) -> int | None:
    if key not in m or m[key] is None:
        return None
    value = m[key]
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"{where}.{key}: expected an integer, got {type(value).__name__}")
        return None
    return value


def _opt_float(m: Mapping[str, Any], key: str, where: str, issues: list[str]) -> float | None:
    if key not in m or m[key] is None:
        return None
    value = m[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        issues.append(f"{where}.{key}: expected a number, got {type(value).__name__}")
        return None
    return float(value)


def _enum[E: StrEnum](
    value: Any, enum_cls: type[E], where: str, issues: list[str], default: E
) -> E:
    if value is None:
        return default
    choices = sorted(member.value for member in enum_cls)
    if not isinstance(value, str):
        issues.append(f"{where}: expected one of {choices}, got {type(value).__name__}")
        return default
    try:
        return enum_cls(value)
    except ValueError:
        issues.append(f"{where}: invalid value {value!r}, expected one of {choices}")
        return default


# --- block builders ---


def _build_auto_mode(raw: Any, issues: list[str]) -> AutoModeConfig:
    where = "orchestrator.auto_mode"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"enabled"}, where, issues)
    return AutoModeConfig(enabled=_bool(m, "enabled", False, where, issues))


def _build_orchestrator(raw: Any, issues: list[str]) -> OrchestratorRuntimeConfig:
    where = "orchestrator"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"auto_mode", "poll_interval_seconds"}, where, issues)
    return OrchestratorRuntimeConfig(
        auto_mode=_build_auto_mode(m.get("auto_mode"), issues),
        poll_interval_seconds=_int(m, "poll_interval_seconds", 300, where, issues),
    )


def _build_repo(raw: Any, issues: list[str]) -> RepoConfig:
    m = _mapping(raw, "repo", issues)
    _check_keys(m, {"url", "local_path", "base_branch", "branch_prefix"}, "repo", issues)
    return RepoConfig(
        url=_str(m, "url", "", "repo", issues),
        local_path=_str(m, "local_path", "./workspace/repo", "repo", issues),
        base_branch=_str(m, "base_branch", "main", "repo", issues),
        branch_prefix=_str(m, "branch_prefix", "agent", "repo", issues),
    )


def _build_provider(raw: Any, pid: ProviderId, issues: list[str]) -> ProviderConfig:
    where = f"agents.providers.{pid.value}"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {
            "command",
            "model",
            "timeout_seconds",
            "permission_profile",
            "extra_args",
            "sandbox",
            "max_turns",
            "max_budget_usd",
            "reasoning",
        },
        where,
        issues,
    )
    reasoning_raw = _opt_str(m, "reasoning", where, issues)
    if reasoning_raw is not None and reasoning_raw not in _REASONING_LEVELS:
        issues.append(
            f"{where}.reasoning: invalid value {reasoning_raw!r}, "
            f"expected one of {sorted(_REASONING_LEVELS)}"
        )
        reasoning_raw = None
    return ProviderConfig(
        command=_str(m, "command", pid.value, where, issues),
        model=_str(m, "model", "", where, issues),
        timeout_seconds=_int(m, "timeout_seconds", 7200, where, issues),
        permission_profile=_str(m, "permission_profile", "workspace-write", where, issues),
        extra_args=_str_tuple(m, "extra_args", (), where, issues),
        sandbox=_opt_str(m, "sandbox", where, issues),
        max_turns=_opt_int(m, "max_turns", where, issues),
        max_budget_usd=_opt_float(m, "max_budget_usd", where, issues),
        reasoning=reasoning_raw,
    )


def _build_providers(raw: Any, issues: list[str]) -> dict[ProviderId, ProviderConfig]:
    m = _mapping(raw, "agents.providers", issues)
    providers: dict[ProviderId, ProviderConfig] = {}
    for key in sorted(m):
        try:
            pid = ProviderId(key)
        except ValueError:
            issues.append(f"agents.providers: unknown provider {key!r}")
            continue
        providers[pid] = _build_provider(m[key], pid, issues)
    return providers


def _build_route(raw: Any, stage: Stage, issues: list[str]) -> RouteConfig:
    where = f"agents.routing.{stage.value}"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"primary", "fallback"}, where, issues)
    primary_raw = m.get("primary")
    if primary_raw is None:
        issues.append(f"{where}.primary: required")
        primary = ProviderId.CLAUDE  # placeholder; an issue was recorded so loading will fail
    else:
        primary = _enum(primary_raw, ProviderId, f"{where}.primary", issues, ProviderId.CLAUDE)
    fallback_raw = m.get("fallback")
    fallback = (
        None
        if fallback_raw is None
        else _enum(fallback_raw, ProviderId, f"{where}.fallback", issues, ProviderId.CODEX)
    )
    return RouteConfig(primary=primary, fallback=fallback)


def _build_routing(raw: Any, issues: list[str]) -> dict[Stage, RouteConfig]:
    m = _mapping(raw, "agents.routing", issues)
    routing: dict[Stage, RouteConfig] = {}
    for key in sorted(m):
        try:
            stage = Stage(key)
        except ValueError:
            issues.append(f"agents.routing: unknown route key {key!r}")
            continue
        routing[stage] = _build_route(m[key], stage, issues)
    return routing


def _legacy_codex_routing() -> dict[Stage, RouteConfig]:
    return {
        stage: RouteConfig(primary=ProviderId.CODEX, fallback=None)
        for stage in sorted(ROUTABLE_STAGES, key=lambda s: s.value)
    }


def _build_decomposition(raw: Any, issues: list[str]) -> DecompositionConfig:
    where = "agents.decomposition"
    m = _mapping(raw, where, issues)
    _check_keys(
        m, {"enabled", "max_subtasks", "min_size_signal", "commit_per_subtask"}, where, issues
    )
    return DecompositionConfig(
        enabled=_bool(m, "enabled", False, where, issues),
        max_subtasks=_int(m, "max_subtasks", 8, where, issues),
        min_size_signal=_str(m, "min_size_signal", "large", where, issues),
        commit_per_subtask=_bool(m, "commit_per_subtask", True, where, issues),
    )


def _build_allowed(raw: Any, issues: list[str]) -> tuple[ProviderId, ...]:
    if raw is None:
        return (ProviderId.CLAUDE, ProviderId.CODEX)
    if not isinstance(raw, list):
        issues.append(f"agents.allowed: expected a list, got {type(raw).__name__}")
        return ()
    allowed: list[ProviderId] = []
    for index, item in enumerate(raw):
        try:
            allowed.append(ProviderId(item))
        except (ValueError, TypeError):
            issues.append(f"agents.allowed[{index}]: unknown provider {item!r}")
    return tuple(allowed)


def _build_skip_stages(m: Mapping[str, Any], issues: list[str]) -> tuple[Stage, ...]:
    """Parse ``agents.skip_stages`` into a tuple of ``Stage`` (fail-closed).

    Each entry must name a stage in ``SKIPPABLE_STAGES``; anything else (unknown name, or a real
    stage that is not skippable like ``implementation``/``publishing``) is a config issue.
    """
    if "skip_stages" not in m:
        return ()
    value = m["skip_stages"]
    if not isinstance(value, list):
        issues.append(f"agents.skip_stages: expected a list, got {type(value).__name__}")
        return ()
    choices = sorted(s.value for s in SKIPPABLE_STAGES)
    out: list[Stage] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(
                f"agents.skip_stages[{index}]: expected a string, got {type(item).__name__}"
            )
            continue
        try:
            stage = Stage(item)
        except ValueError:
            issues.append(f"agents.skip_stages[{index}]: unknown stage {item!r}")
            continue
        if stage not in SKIPPABLE_STAGES:
            issues.append(
                f"agents.skip_stages[{index}]: stage {item!r} is not skippable (allowed: {choices})"
            )
            continue
        out.append(stage)
    return tuple(out)


def _build_agents(raw: Any, issues: list[str], warnings: list[str]) -> AgentsConfig:
    m = _mapping(raw, "agents", issues)
    _check_keys(
        m,
        {
            "allowed",
            "max_stage_attempts",
            "max_fix_cycles",
            "max_total_fix_iterations",
            "decomposition",
            "routing",
            "providers",
            "skip_stages",
            "allow_review_skip",
        },
        "agents",
        issues,
    )
    if "routing" in m:
        routing = _build_routing(m["routing"], issues)
    else:
        # Legacy Codex-only config: no routing block. Migrate to a Codex route for every agent
        # stage, with a warning (spec §11).
        warnings.append(
            "agents.routing is missing; migrating legacy Codex-only config to a Codex route "
            "for all agent stages"
        )
        routing = _legacy_codex_routing()
    skip_stages = _build_skip_stages(m, issues)
    allow_review_skip = _bool(m, "allow_review_skip", False, "agents", issues)
    # Fail-closed: disabling the review quality gate (no agent review before commit/PR) requires the
    # explicit opt-in, whether the skip comes from the global list or a per-task override.
    if Stage.REVIEW in skip_stages and not allow_review_skip:
        issues.append("agents.skip_stages: 'review' requires agents.allow_review_skip: true")
    return AgentsConfig(
        allowed=_build_allowed(m.get("allowed"), issues),
        max_stage_attempts=_int(m, "max_stage_attempts", 3, "agents", issues),
        max_fix_cycles=_int(m, "max_fix_cycles", 15, "agents", issues),
        max_total_fix_iterations=_int(m, "max_total_fix_iterations", 30, "agents", issues),
        decomposition=_build_decomposition(m.get("decomposition"), issues),
        routing=routing,
        providers=_build_providers(m.get("providers"), issues),
        skip_stages=skip_stages,
        allow_review_skip=allow_review_skip,
    )


def _build_security(raw: Any, issues: list[str]) -> SecurityConfig:
    where = "security"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {"strict_isolation", "allowed_environment", "denied_read_paths", "denied_commands"},
        where,
        issues,
    )
    return SecurityConfig(
        strict_isolation=_bool(m, "strict_isolation", True, where, issues),
        allowed_environment=_str_tuple(
            m, "allowed_environment", _DEFAULT_ALLOWED_ENV, where, issues
        ),
        denied_read_paths=_str_tuple(m, "denied_read_paths", (".env", "secrets/**"), where, issues),
        denied_commands=_str_tuple(
            m,
            "denied_commands",
            ("git commit", "git push", "gh pr create", "gh pr merge"),
            where,
            issues,
        ),
    )


def _build_validation(raw: Any, issues: list[str]) -> ValidationConfig:
    where = "validation"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {
            "max_task_bytes",
            "max_task_lines",
            "max_line_bytes",
            "max_control_ratio",
            "required_fields",
            "reject_unknown_fields",
            "quarantine_folder",
        },
        where,
        issues,
    )
    return ValidationConfig(
        max_task_bytes=_int(m, "max_task_bytes", 262144, where, issues),
        max_task_lines=_int(m, "max_task_lines", 5000, where, issues),
        max_line_bytes=_int(m, "max_line_bytes", 8192, where, issues),
        max_control_ratio=_float(m, "max_control_ratio", 0.01, where, issues),
        required_fields=_str_tuple(m, "required_fields", ("id", "title"), where, issues),
        reject_unknown_fields=_bool(m, "reject_unknown_fields", True, where, issues),
        quarantine_folder=_str(m, "quarantine_folder", "./tasks/rejected", where, issues),
    )


def _build_discovery(raw: Any, issues: list[str]) -> CheckDiscoveryConfig:
    where = "checks.discovery"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {
            "mode",
            "agent_fallback",
            "refresh",
            "provider",
            "model",
            "reasoning",
            "timeout_seconds",
            "run_at_task_start",
            "approve_command_changes",
        },
        where,
        issues,
    )
    if "reasoning" not in m:
        reasoning: str | None = "low"
    else:
        reasoning = _opt_str(m, "reasoning", where, issues)
        if reasoning is not None and reasoning not in _REASONING_LEVELS:
            issues.append(
                f"{where}.reasoning: invalid value {reasoning!r}, "
                f"expected one of {sorted(_REASONING_LEVELS)}"
            )
            reasoning = None
    provider_raw = m.get("provider")
    provider = (
        None
        if provider_raw is None
        else _enum(provider_raw, ProviderId, f"{where}.provider", issues, ProviderId.CLAUDE)
    )
    return CheckDiscoveryConfig(
        mode=_enum(
            m.get("mode"),
            CheckDiscoveryMode,
            f"{where}.mode",
            issues,
            CheckDiscoveryMode.CONFIGURED,
        ),
        agent_fallback=_bool(m, "agent_fallback", True, where, issues),
        refresh=_enum(
            m.get("refresh"),
            CheckRefreshPolicy,
            f"{where}.refresh",
            issues,
            CheckRefreshPolicy.ON_CHANGE,
        ),
        provider=provider,
        model=_str(m, "model", "", where, issues),
        reasoning=reasoning,
        timeout_seconds=_int(m, "timeout_seconds", 120, where, issues),
        run_at_task_start=_bool(m, "run_at_task_start", True, where, issues),
        approve_command_changes=_bool(m, "approve_command_changes", True, where, issues),
    )


def _build_checks(raw: Any, issues: list[str]) -> ChecksConfig:
    where = "checks"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"commands", "timeout_seconds", "discovery"}, where, issues)
    return ChecksConfig(
        commands=_check_command_list(m, "commands", where, issues),
        timeout_seconds=_int(m, "timeout_seconds", 7200, where, issues),
        discovery=_build_discovery(m.get("discovery"), issues),
    )


def _build_footprint(raw: Any, issues: list[str]) -> FootprintConfig:
    where = "git.footprint"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {"location", "tracking", "external_root", "audit_commit_message", "audit_on_branch"},
        where,
        issues,
    )
    return FootprintConfig(
        location=_enum(
            m.get("location"),
            FootprintLocation,
            f"{where}.location",
            issues,
            FootprintLocation.IN_REPO,
        ),
        tracking=_enum(
            m.get("tracking"),
            FootprintTracking,
            f"{where}.tracking",
            issues,
            FootprintTracking.COMMIT,
        ),
        external_root=_str(m, "external_root", "./", where, issues),
        audit_commit_message=_str(m, "audit_commit_message", _DEFAULT_AUDIT_MESSAGE, where, issues),
        audit_on_branch=_enum(
            m.get("audit_on_branch"),
            AuditBranch,
            f"{where}.audit_on_branch",
            issues,
            AuditBranch.TASK,
        ),
    )


def _build_git(raw: Any, issues: list[str]) -> GitConfig:
    where = "git"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {
            "create_pull_request",
            "pr_base",
            "footprint",
            "auto_merge",
            "auto_merge_strategy",
            "auto_merge_allow_per_task",
            "auto_merge_wait_for_checks",
        },
        where,
        issues,
    )
    return GitConfig(
        create_pull_request=_bool(m, "create_pull_request", True, where, issues),
        pr_base=_str(m, "pr_base", "main", where, issues),
        footprint=_build_footprint(m.get("footprint"), issues),
        auto_merge=_bool(m, "auto_merge", False, where, issues),
        auto_merge_strategy=_enum(
            m.get("auto_merge_strategy"),
            MergeStrategy,
            f"{where}.auto_merge_strategy",
            issues,
            MergeStrategy.SQUASH,
        ),
        auto_merge_allow_per_task=_bool(m, "auto_merge_allow_per_task", False, where, issues),
        auto_merge_wait_for_checks=_bool(m, "auto_merge_wait_for_checks", False, where, issues),
    )


def _build_telegram(raw: Any, issues: list[str]) -> TelegramConfig:
    where = "telegram"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"enabled", "bot_token_env", "chat_id_env", "ask_timeout_s"}, where, issues)
    return TelegramConfig(
        enabled=_bool(m, "enabled", False, where, issues),
        bot_token_env=_str(m, "bot_token_env", "TELEGRAM_BOT_TOKEN", where, issues),
        chat_id_env=_str(m, "chat_id_env", "TELEGRAM_CHAT_ID", where, issues),
        ask_timeout_s=_int(m, "ask_timeout_s", 28800, where, issues),
    )


def _build_prompt_overrides(raw: Any, issues: list[str]) -> tuple[tuple[Stage, str], ...]:
    where = "prompts.overrides"
    m = _mapping(raw, where, issues)
    overrides: dict[Stage, str] = {}
    for key in sorted(m):
        try:
            stage = Stage(key)
        except ValueError:
            issues.append(f"{where}: unknown stage key {key!r}")
            continue
        value = m[key]
        if not isinstance(value, str):
            issues.append(f"{where}.{key}: expected a string, got {type(value).__name__}")
            continue
        overrides[stage] = value
    return tuple(overrides.items())


def _build_prompts(raw: Any, issues: list[str]) -> PromptsConfig:
    where = "prompts"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"templates_dir", "mode", "strict", "overrides"}, where, issues)
    return PromptsConfig(
        templates_dir=_str(m, "templates_dir", "./templates/prompts", where, issues),
        mode=_enum(m.get("mode"), PromptMode, f"{where}.mode", issues, PromptMode.APPEND),
        strict=_bool(m, "strict", False, where, issues),
        overrides=_build_prompt_overrides(m.get("overrides"), issues),
    )


def _build_skills(raw: Any, issues: list[str]) -> SkillsConfig:
    where = "skills"
    if raw is None:
        return SkillsConfig()
    m = _mapping(raw, where, issues)
    _check_keys(m, {"scan_root", "exclude"}, where, issues)
    return SkillsConfig(
        scan_root=_str(m, "scan_root", "", where, issues),
        exclude=_str_tuple(m, "exclude", ("run-checks", "test", "sync-docs"), where, issues),
    )


_TOP_LEVEL_KEYS = {
    "schema_version",
    "orchestrator",
    "repo",
    "agents",
    "security",
    "validation",
    "checks",
    "git",
    "telegram",
    "prompts",
    "skills",
}


def _check_schema_version(raw: Mapping[str, Any], issues: list[str]) -> None:
    """Refuse a config whose ``schema_version`` is newer than this orchestrator understands.

    Absent or ``<= CONFIG_SCHEMA_VERSION`` is accepted (a lower value is the future-migration hook);
    a newer value is a fail-closed error directing the operator to upgrade. ``schema_version`` is
    config metadata only — it is validated here and not stored on :class:`OrchestratorConfig`.
    """
    if "schema_version" not in raw:
        return
    value = raw["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"schema_version: expected an integer, got {type(value).__name__}")
        return
    if value > CONFIG_SCHEMA_VERSION:
        issues.append(
            f"schema_version {value} is newer than this orchestrator supports "
            f"({CONFIG_SCHEMA_VERSION}); upgrade wastech-orchestrator"
        )


def _parse(raw: Mapping[str, Any], issues: list[str], warnings: list[str]) -> OrchestratorConfig:
    _check_keys(raw, _TOP_LEVEL_KEYS, "<root>", issues)
    _check_schema_version(raw, issues)
    return OrchestratorConfig(
        orchestrator=_build_orchestrator(raw.get("orchestrator"), issues),
        repo=_build_repo(raw.get("repo"), issues),
        agents=_build_agents(raw.get("agents"), issues, warnings),
        security=_build_security(raw.get("security"), issues),
        validation=_build_validation(raw.get("validation"), issues),
        checks=_build_checks(raw.get("checks"), issues),
        git=_build_git(raw.get("git"), issues),
        telegram=_build_telegram(raw.get("telegram"), issues),
        prompts=_build_prompts(raw.get("prompts"), issues),
        skills=_build_skills(raw.get("skills"), issues),
    )


def loads_config(text: str, *, source: str = "<string>") -> ConfigLoadResult:
    """Parse config YAML text into the typed schema. Raises :class:`ConfigError` on any problem."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"{source}: YAML parse error: {exc}"]) from exc
    if raw is None or not isinstance(raw, Mapping):
        raise ConfigError([f"{source}: top-level config must be a mapping"])
    issues: list[str] = []
    warnings: list[str] = []
    config = _parse({str(k): v for k, v in raw.items()}, issues, warnings)
    if issues:
        raise ConfigError(issues)
    return ConfigLoadResult(config=config, warnings=tuple(warnings))


def load_config(path: str | Path) -> ConfigLoadResult:
    """Read and parse a config file. Structural problems raise :class:`ConfigError`.

    Semantic §11/§21.4 rules are enforced separately by ``config.validation.validate_config``.
    """
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    return loads_config(text, source=str(file))
