"""Configuration loader.

Reads a config.yaml into the typed schema, fail-closed: a non-mapping root, an unknown top-level
or per-block key, an unknown route stage, or a bad enum value is an error — never silently dropped.
Every problem is collected and reported together via a typed :class:`ConfigError` (not bare
strings). Loading is an explicit call with no import-time side effects.

This module owns *structural* parsing only. The cross-field semantic rules live in
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
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    TRUST_LEVELS,
    AgentsConfig,
    AuditBranch,
    AutoModeConfig,
    BranchMode,
    CheckCommandSpec,
    ChecksConfig,
    CommandSet,
    DecompositionConfig,
    FootprintConfig,
    GitConfig,
    LoggingConfig,
    MemoryConfig,
    MergeStrategy,
    OrchestratorConfig,
    OrchestratorRuntimeConfig,
    PathsConfig,
    ProviderConfig,
    RepoConfig,
    RetryConfig,
    SecurityConfig,
    SkillsConfig,
    SupervisorConfig,
    TelegramConfig,
    ToolsConfig,
    ValidationConfig,
)
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.providers.capabilities import all_reasoning_levels
from wastech_orchestrator.security.env import default_allowed_environment

# Defaults mirror / the packaged config.example.yaml so a partial config still loads safely.
_DEFAULT_AUDIT_MESSAGE = "chore(orchestrator): audit trail for {task_id}"

_REASONING_LEVELS: frozenset[str] = all_reasoning_levels()

_DEFAULT_DENIED_READ_PATHS: tuple[str, ...] = (".env", "secrets/**")
_DEFAULT_DENIED_COMMANDS: tuple[str, ...] = (
    "git commit",
    "git push",
    "gh pr create",
    "gh pr merge",
)


class ConfigError(Exception):
    """A config file is structurally invalid or violates a rule.

    Carries *every* problem found (``issues``), not just the first — fail-closed reporting.
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


def _check_keys(
    m: Mapping[str, Any],
    allowed: set[str],
    where: str,
    issues: list[str],
    *,
    tolerated: set[str] | None = None,
) -> None:
    # ``tolerated`` keys (e.g. removed-but-legacy config keys) are neither accepted into the schema
    # nor reported as errors — the caller handles them (typically a deprecation warning).
    ignore = allowed | (tolerated or set())
    issues.extend(f"{where}: unknown key {key!r}" for key in sorted(set(m) - ignore))


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


def _opt_bool(m: Mapping[str, Any], key: str, where: str, issues: list[str]) -> bool | None:
    """A tri-state bool: absent → ``None`` (defer to the default), else a validated bool."""
    if key not in m:
        return None
    value = m[key]
    if not isinstance(value, bool):
        issues.append(f"{where}.{key}: expected a boolean, got {type(value).__name__}")
        return None
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


def _command_spec(m: Mapping[str, Any], where: str, issues: list[str]) -> CheckCommandSpec | None:
    """Parse one ``{name, argv, cwd?}`` command mapping (structural only; the ``cwd`` traversal
    check lives in ``config.validation``, the ``shlex``/argv normalization in ``checks.model``)."""
    _check_keys(m, {"name", "argv", "cwd"}, where, issues)
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
    cwd = _opt_str(m, "cwd", where, issues)
    return CheckCommandSpec(argv=tuple(argv), name=name, cwd=cwd)


def _command_set(m: Mapping[str, Any], where: str, issues: list[str]) -> CommandSet:
    """Parse one ``checks.command_sets.<name>`` mapping into a :class:`CommandSet` (shapes only)."""
    _check_keys(m, {"paths", "timeout_seconds", "skip_if_unavailable", "commands"}, where, issues)
    raw_cmds = m.get("commands")
    commands: list[CheckCommandSpec] = []
    if not isinstance(raw_cmds, list):
        issues.append(f"{where}.commands: expected a list of {{name, argv, cwd?}} mappings")
    else:
        for index, item in enumerate(raw_cmds):
            item_where = f"{where}.commands[{index}]"
            if isinstance(item, Mapping):
                spec = _command_spec({str(k): v for k, v in item.items()}, item_where, issues)
                if spec is not None:
                    commands.append(spec)
            else:
                issues.append(
                    f"{item_where}: expected a {{name, argv, cwd?}} mapping, "
                    f"got {type(item).__name__}"
                )
    return CommandSet(
        commands=tuple(commands),
        paths=_str_tuple(m, "paths", (), where, issues),
        timeout_seconds=_opt_int(m, "timeout_seconds", where, issues),
        skip_if_unavailable=_bool(m, "skip_if_unavailable", False, where, issues),
    )


def _build_command_sets(raw: Any, issues: list[str]) -> dict[str, CommandSet]:
    """Parse ``checks.command_sets`` (name → set); the mapping key is the set name."""
    if raw is None:
        return {}
    m = _mapping(raw, "checks.command_sets", issues)
    sets: dict[str, CommandSet] = {}
    for name in m:
        where = f"checks.command_sets.{name}"
        sets[name] = _command_set(_mapping(m[name], where, issues), where, issues)
    return sets


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


# ``max_turns`` accepts a positive integer (a turn cap), the string ``"none"``/``"max"`` or YAML
# ``null`` (no orchestrator-imposed cap — the ``--max-turns`` flag is omitted entirely), or nothing
# (the default below). Sentinels are case-insensitive.
_DEFAULT_MAX_TURNS = 400
_MAX_TURNS_UNLIMITED = frozenset({"none", "max"})


def _max_turns(m: Mapping[str, Any], where: str, issues: list[str]) -> int | None:
    """Parse ``max_turns`` to a turn cap (``int``) or ``None`` (no cap → flag omitted).

    Positive int → cap; ``"none"``/``"max"`` (case-insensitive) or YAML ``null`` → ``None``; absent
    → :data:`_DEFAULT_MAX_TURNS`; anything else (``0``, negative, other strings, wrong type) → an
    issue plus the default.
    """
    key = "max_turns"
    if key not in m:
        return _DEFAULT_MAX_TURNS
    value = m[key]
    if value is None:
        return None  # explicit null → no cap
    if isinstance(value, str):
        if value.strip().lower() in _MAX_TURNS_UNLIMITED:
            return None
        issues.append(
            f"{where}.{key}: expected a positive integer or one of "
            f"{sorted(_MAX_TURNS_UNLIMITED)}, got {value!r}"
        )
        return _DEFAULT_MAX_TURNS
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(
            f"{where}.{key}: expected a positive integer or 'none'/'max', "
            f"got {type(value).__name__}"
        )
        return _DEFAULT_MAX_TURNS
    if value <= 0:
        issues.append(
            f"{where}.{key}: expected a positive integer (use 'none' or 'max' for no cap), "
            f"got {value}"
        )
        return _DEFAULT_MAX_TURNS
    return value


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
    _check_keys(m, {"enabled", "confirm_next_task"}, where, issues)
    return AutoModeConfig(
        enabled=_bool(m, "enabled", False, where, issues),
        confirm_next_task=_bool(m, "confirm_next_task", False, where, issues),
    )


def _build_orchestrator(raw: Any, issues: list[str]) -> OrchestratorRuntimeConfig:
    where = "orchestrator"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"auto_mode", "poll_interval_seconds", "queue"}, where, issues)
    return OrchestratorRuntimeConfig(
        auto_mode=_build_auto_mode(m.get("auto_mode"), issues),
        poll_interval_seconds=_int(m, "poll_interval_seconds", 300, where, issues),
        queue=_str(m, "queue", "default", where, issues),
    )


def _build_repo(raw: Any, issues: list[str]) -> RepoConfig:
    m = _mapping(raw, "repo", issues)
    _check_keys(
        m,
        {
            "url",
            "local_path",
            "base_branch",
            "branch_prefix",
            "branch_mode",
            "checkout_base_on_cleanup",
        },
        "repo",
        issues,
    )
    return RepoConfig(
        url=_str(m, "url", "", "repo", issues),
        local_path=_str(m, "local_path", "./workspace/repo", "repo", issues),
        base_branch=_str(m, "base_branch", "main", "repo", issues),
        branch_prefix=_str(m, "branch_prefix", "worc", "repo", issues),
        branch_mode=_enum(
            m.get("branch_mode"), BranchMode, "repo.branch_mode", issues, BranchMode.NEW
        ),
        checkout_base_on_cleanup=_opt_bool(m, "checkout_base_on_cleanup", "repo", issues),
    )


def _build_paths(raw: Any, issues: list[str]) -> PathsConfig:
    # The whole block is optional; an absent `paths` yields the default `tasks_dir`. Only the string
    # shape is checked here — the repo-relative / `.worc` rules are enforced by the gate.
    if raw is None:
        return PathsConfig()
    m = _mapping(raw, "paths", issues)
    _check_keys(m, {"tasks_dir"}, "paths", issues)
    return PathsConfig(tasks_dir=_str(m, "tasks_dir", "tasks", "paths", issues))


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
            "max_turns_gate",
            "allow_native_memory",
            "reasoning",
            "primary",
        },
        where,
        issues,
        # ``max_budget_usd`` (removed v14) is tolerated, not accepted — a stale config still loads.
        tolerated={"max_budget_usd"},
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
        max_turns=_max_turns(m, where, issues),
        reasoning=reasoning_raw,
        primary=_bool(m, "primary", False, where, issues),
        max_turns_gate=_bool(m, "max_turns_gate", False, where, issues),
        allow_native_memory=_bool(m, "allow_native_memory", False, where, issues),
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


def _build_decomposition(raw: Any, issues: list[str]) -> DecompositionConfig:
    where = "agents.decomposition"
    m = _mapping(raw, where, issues)
    # ``min_size_signal`` / ``commit_per_subtask`` (removed v12) are tolerated, not accepted: an old
    # config still loads fail-open and ``upgrade-config`` strips the dead keys.
    _check_keys(
        m,
        {"enabled", "max_subtasks"},
        where,
        issues,
        tolerated={"min_size_signal", "commit_per_subtask"},
    )
    return DecompositionConfig(
        enabled=_bool(m, "enabled", False, where, issues),
        max_subtasks=_int(m, "max_subtasks", 8, where, issues),
    )


def _build_retry(raw: Any, issues: list[str]) -> RetryConfig:
    where = "agents.retry"
    if raw is None:
        return RetryConfig()  # whole block optional → defaults (back-compat)
    m = _mapping(raw, where, issues)
    _check_keys(m, {"max_attempts", "base_delay_s", "max_delay_s", "max_blocked_s"}, where, issues)
    return RetryConfig(
        max_attempts=_int(m, "max_attempts", 2, where, issues),
        base_delay_s=_float(m, "base_delay_s", 2.0, where, issues),
        max_delay_s=_float(m, "max_delay_s", 30.0, where, issues),
        max_blocked_s=_float(m, "max_blocked_s", 21600.0, where, issues),
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


def _build_agents(raw: Any, issues: list[str]) -> AgentsConfig:
    m = _mapping(raw, "agents", issues)
    # ``skip_stages`` (removed v10), the stage-keyed ``routing`` block (removed v11 — a flow node
    # declares its own ``provider``, else the global ``providers.<id>.primary``), and
    # ``allow_review_skip`` (removed v13 — no ``review``-special-case; per-task disable is by node
    # id and the operator owns which nodes are safe to disable) are tolerated, not accepted: an old
    # config still loads fail-open and ``upgrade-config`` strips the dead keys.
    _check_keys(
        m,
        {
            "allowed",
            "max_stage_attempts",
            "max_fix_cycles",
            "max_total_fix_iterations",
            "decomposition",
            "retry",
            "providers",
        },
        "agents",
        issues,
        tolerated={"skip_stages", "routing", "allow_review_skip"},
    )
    return AgentsConfig(
        allowed=_build_allowed(m.get("allowed"), issues),
        max_stage_attempts=_int(m, "max_stage_attempts", 3, "agents", issues),
        max_fix_cycles=_int(m, "max_fix_cycles", 15, "agents", issues),
        max_total_fix_iterations=_int(m, "max_total_fix_iterations", 30, "agents", issues),
        decomposition=_build_decomposition(m.get("decomposition"), issues),
        retry=_build_retry(m.get("retry"), issues),
        providers=_build_providers(m.get("providers"), issues),
    )


def _build_security(raw: Any, issues: list[str]) -> SecurityConfig:
    where = "security"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {
            "strict_isolation",
            "allowed_environment",
            "denied_read_paths",
            "denied_commands",
            "trust_level",
            "protected_paths",
        },
        where,
        issues,
    )
    trust_level = _str(m, "trust_level", "strict", where, issues)
    if trust_level not in TRUST_LEVELS:
        issues.append(
            f"{where}.trust_level: invalid value {trust_level!r}, "
            f"expected one of {sorted(TRUST_LEVELS)}"
        )
        trust_level = "strict"
    return SecurityConfig(
        strict_isolation=_bool(m, "strict_isolation", True, where, issues),
        allowed_environment=_str_tuple(
            m, "allowed_environment", default_allowed_environment(), where, issues
        ),
        denied_read_paths=_with_security_defaults(
            _DEFAULT_DENIED_READ_PATHS,
            _str_tuple(m, "denied_read_paths", (), where, issues),
        ),
        denied_commands=_with_security_defaults(
            _DEFAULT_DENIED_COMMANDS,
            _str_tuple(m, "denied_commands", (), where, issues),
        ),
        trust_level=trust_level,
        protected_paths=_str_tuple(m, "protected_paths", (), where, issues),
    )


def _with_security_defaults(
    defaults: tuple[str, ...], configured: tuple[str, ...]
) -> tuple[str, ...]:
    """Append operator denials to the mandatory baseline without duplicate rules."""
    return tuple(dict.fromkeys((*defaults, *configured)))


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
        quarantine_folder=_str(m, "quarantine_folder", "./.worc/tasks/rejected", where, issues),
    )


def _build_checks(raw: Any, issues: list[str]) -> ChecksConfig:
    where = "checks"
    m = _mapping(raw, where, issues)
    # ``discovery`` (whole block) and ``commands`` (flat list) were removed in config v15 —
    # tolerated (ignored), not accepted: a stale config still loads fail-open and ``upgrade-config``
    # strips the dead keys. The gate is now exactly the operator's ``command_sets`` (empty = none).
    _check_keys(
        m,
        {"command_sets", "timeout_seconds"},
        where,
        issues,
        tolerated={"discovery", "commands"},
    )
    return ChecksConfig(
        command_sets=_build_command_sets(m.get("command_sets"), issues),
        timeout_seconds=_int(m, "timeout_seconds", 7200, where, issues),
    )


def _build_footprint(raw: Any, issues: list[str]) -> FootprintConfig:
    where = "git.footprint"
    m = _mapping(raw, where, issues)
    _check_keys(m, {"audit_commit_message", "audit_on_branch"}, where, issues)
    return FootprintConfig(
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
    # ``auto_merge_allow_per_task`` (removed v11) is tolerated, not accepted: a per-task
    # ``auto_merge`` now wins outright (PRE.2), so the gate is gone. Old configs load fail-open;
    # ``upgrade-config`` strips the dead key.
    _check_keys(
        m,
        {
            "create_pull_request",
            "pr_base",
            "footprint",
            "auto_merge",
            "auto_merge_strategy",
            "auto_merge_wait_for_checks",
            "merge_flow",
        },
        where,
        issues,
        tolerated={"auto_merge_allow_per_task"},
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
        auto_merge_wait_for_checks=_bool(m, "auto_merge_wait_for_checks", False, where, issues),
        merge_flow=_str(m, "merge_flow", "merge", where, issues),
    )


def _build_telegram(raw: Any, issues: list[str]) -> TelegramConfig:
    where = "telegram"
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {"enabled", "bot_token_env", "chat_id_env", "ask_timeout_s", "trace"},
        where,
        issues,
    )
    return TelegramConfig(
        enabled=_bool(m, "enabled", False, where, issues),
        bot_token_env=_str(m, "bot_token_env", "TELEGRAM_BOT_TOKEN", where, issues),
        chat_id_env=_str(m, "chat_id_env", "TELEGRAM_CHAT_ID", where, issues),
        ask_timeout_s=_int(m, "ask_timeout_s", 28800, where, issues),
        trace=_bool(m, "trace", False, where, issues),
    )


def _build_skills(raw: Any, issues: list[str]) -> SkillsConfig:
    where = "skills"
    if raw is None:
        return SkillsConfig()
    m = _mapping(raw, where, issues)
    _check_keys(m, {"dynamic", "strict"}, where, issues)
    return SkillsConfig(
        dynamic=_bool(m, "dynamic", True, where, issues),
        strict=_bool(m, "strict", False, where, issues),
    )


def _build_supervisor(raw: Any, issues: list[str]) -> SupervisorConfig:
    where = "supervisor"
    if raw is None:
        return SupervisorConfig()
    m = _mapping(raw, where, issues)
    _check_keys(m, {"role_file", "model", "reasoning", "provider"}, where, issues)
    reasoning = _opt_str(m, "reasoning", where, issues)
    if reasoning is not None and reasoning not in _REASONING_LEVELS:
        issues.append(
            f"{where}.reasoning: invalid value {reasoning!r}, "
            f"expected one of {sorted(_REASONING_LEVELS)}"
        )
        reasoning = None
    provider_raw = _opt_str(m, "provider", where, issues)
    provider: ProviderId | None = None
    if provider_raw is not None:
        try:
            provider = ProviderId(provider_raw)
        except ValueError:
            issues.append(f"{where}.provider: unknown provider {provider_raw!r}")
    return SupervisorConfig(
        role_file=_str(m, "role_file", "roles/supervisor.md", where, issues),
        model=_opt_str(m, "model", where, issues),
        reasoning=reasoning,
        provider=provider,
    )


_LOG_LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error"})
_ARTIFACT_LEVELS: frozenset[str] = frozenset({"minimal", "standard", "full"})


def _build_logging(raw: Any, issues: list[str]) -> LoggingConfig:
    where = "logging"
    if raw is None:
        return LoggingConfig()
    m = _mapping(raw, where, issues)
    _check_keys(m, {"level", "artifacts"}, where, issues)
    level = _str(m, "level", "info", where, issues)
    if level not in _LOG_LEVELS:
        issues.append(
            f"{where}.level: invalid value {level!r}, expected one of {sorted(_LOG_LEVELS)}"
        )
        level = "info"
    artifacts = _str(m, "artifacts", "standard", where, issues)
    if artifacts not in _ARTIFACT_LEVELS:
        issues.append(
            f"{where}.artifacts: invalid value {artifacts!r}, "
            f"expected one of {sorted(_ARTIFACT_LEVELS)}"
        )
        artifacts = "standard"
    return LoggingConfig(level=level, artifacts=artifacts)


def _build_memory(raw: Any, issues: list[str]) -> MemoryConfig:
    where = "memory"
    if raw is None:
        return MemoryConfig()  # absent => disabled defaults (Q10): today's behavior exactly
    m = _mapping(raw, where, issues)
    _check_keys(
        m,
        {
            "enabled",
            "short_term_ttl_days",
            "packet_max_lines",
            "packet_max_long_term",
            "packet_max_entity",
            "packet_max_episodic",
            "promote_min_tasks",
            "promote_window_days",
            "cleanup_min_interval_s",
            "cleanup_max_scanned",
            "cleanup_max_edits",
            "cleanup_max_wall_clock_s",
            "cleanup_promotions_per_pass",
        },
        where,
        issues,
    )
    return MemoryConfig(
        enabled=_bool(m, "enabled", False, where, issues),
        short_term_ttl_days=_int(m, "short_term_ttl_days", 30, where, issues),
        packet_max_lines=_int(m, "packet_max_lines", 120, where, issues),
        packet_max_long_term=_int(m, "packet_max_long_term", 3, where, issues),
        packet_max_entity=_int(m, "packet_max_entity", 5, where, issues),
        packet_max_episodic=_int(m, "packet_max_episodic", 3, where, issues),
        promote_min_tasks=_int(m, "promote_min_tasks", 2, where, issues),
        promote_window_days=_int(m, "promote_window_days", 60, where, issues),
        cleanup_min_interval_s=_int(m, "cleanup_min_interval_s", 300, where, issues),
        cleanup_max_scanned=_int(m, "cleanup_max_scanned", 200, where, issues),
        cleanup_max_edits=_int(m, "cleanup_max_edits", 50, where, issues),
        cleanup_max_wall_clock_s=_float(m, "cleanup_max_wall_clock_s", 5.0, where, issues),
        cleanup_promotions_per_pass=_int(m, "cleanup_promotions_per_pass", 0, where, issues),
    )


def _build_tools(raw: Any, issues: list[str]) -> ToolsConfig:
    where = "tools"
    if raw is None:
        return ToolsConfig()  # absent => the built-in 3600s default (P5)
    m = _mapping(raw, where, issues)
    _check_keys(m, {"default_timeout_seconds"}, where, issues)
    return ToolsConfig(
        default_timeout_seconds=_int(
            m, "default_timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS, where, issues
        ),
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
    "skills",
    "supervisor",
    "paths",
    "logging",
    "memory",
    "tools",
    "prompt_audit",
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
    # ``prompts`` (removed in config v9) is tolerated, not accepted: an old config still loads
    # fail-open and ``upgrade-config`` strips the dead block.
    _check_keys(raw, _TOP_LEVEL_KEYS, "<root>", issues, tolerated={"prompts"})
    _check_schema_version(raw, issues)
    return OrchestratorConfig(
        orchestrator=_build_orchestrator(raw.get("orchestrator"), issues),
        repo=_build_repo(raw.get("repo"), issues),
        agents=_build_agents(raw.get("agents"), issues),
        security=_build_security(raw.get("security"), issues),
        validation=_build_validation(raw.get("validation"), issues),
        checks=_build_checks(raw.get("checks"), issues),
        git=_build_git(raw.get("git"), issues),
        telegram=_build_telegram(raw.get("telegram"), issues),
        skills=_build_skills(raw.get("skills"), issues),
        supervisor=_build_supervisor(raw.get("supervisor"), issues),
        paths=_build_paths(raw.get("paths"), issues),
        logging=_build_logging(raw.get("logging"), issues),
        memory=_build_memory(raw.get("memory"), issues),
        tools=_build_tools(raw.get("tools"), issues),
        prompt_audit=_bool(raw, "prompt_audit", False, "<root>", issues),
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

    Semantic rules are enforced separately by ``config.validation.validate_config``.
    """
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    return loads_config(text, source=str(file))
