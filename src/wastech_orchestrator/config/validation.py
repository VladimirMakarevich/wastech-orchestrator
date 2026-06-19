"""Configuration validator (spec §11, §21.4) — the fail-closed gate.

Enforces every §11 / §21.4 semantic rule so an unsafe or contradictory config never reaches the
pipeline. This is the config-time half of the "security cannot be weakened" invariant
(.agents/rules/security.md): ``extra_args`` that would disable the sandbox/approvals are
rejected here. The adversarial test matrix lives in P6.

All problems are collected and raised together via the typed :class:`ConfigError` from the loader.
"""

from __future__ import annotations

import re

from wastech_orchestrator.checks.model import (
    CheckCommandError,
    argv_matches_denied,
    normalize_check_command,
    shell_metachars,
)
from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import (
    CheckDiscoveryMode,
    OrchestratorConfig,
)
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security.forbidden_args import find_forbidden_args

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_extra_args(pid: ProviderId, args: tuple[str, ...], issues: list[str]) -> None:
    """Reject any extra_args flag that disables the sandbox/permissions (spec §11).

    Delegates the detection to the shared
    :func:`~wastech_orchestrator.security.forbidden_args.find_forbidden_args` (also used at run time
    by the provider command builders) and frames each finding as a config issue.
    """
    where = f"agents.providers.{pid.value}.extra_args"
    for reason in find_forbidden_args(args):
        issues.append(f"{where}: {reason}")


def _check_global_primary(
    config: OrchestratorConfig, allowed: frozenset[ProviderId], issues: list[str]
) -> None:
    """Exactly one configured provider must be the global primary, and it must be allowed (PRE.1).

    The global primary runs any flow node with no ``provider`` field and is the sole
    infrastructure-fallback target; the router relies on this invariant.
    """
    primaries = [pid for pid, p in config.agents.providers.items() if p.primary]
    if len(primaries) != 1:
        issues.append(
            "agents.providers: exactly one provider must set primary: true "
            f"(found {len(primaries)}: {sorted(p.value for p in primaries)})"
        )
        return
    if primaries[0] not in allowed:
        issues.append(
            f"agents.providers.{primaries[0].value}.primary: the global primary must be in "
            "agents.allowed"
        )


def validate_config(config: OrchestratorConfig) -> list[str]:
    """Validate a parsed config against §11/§21.4. Raises :class:`ConfigError` on any violation.

    Returns a list of non-fatal warnings (empty in v1 — every validator finding is a hard error).
    """
    issues: list[str] = []
    warnings: list[str] = []
    agents = config.agents
    allowed = frozenset(agents.allowed)

    # Provider routing: exactly one global primary, in agents.allowed (PRE.1 — node-based routing).
    _check_global_primary(config, allowed, issues)

    # Watch poll interval (§8.3): negative is meaningless; 0 means single-pass (no loop).
    if config.orchestrator.poll_interval_seconds < 0:
        issues.append(
            "orchestrator.poll_interval_seconds must be >= 0 "
            f"(got {config.orchestrator.poll_interval_seconds})"
        )

    # Loop-control hard cap (§8.1): the global cap must be >= a single fix loop.
    if agents.max_total_fix_iterations < agents.max_fix_cycles:
        issues.append(
            "agents.max_total_fix_iterations must be >= agents.max_fix_cycles "
            f"({agents.max_total_fix_iterations} < {agents.max_fix_cycles})"
        )

    # Decomposition: a split must produce at least 2 subtasks.
    if agents.decomposition.max_subtasks < 2:
        issues.append(
            "agents.decomposition.max_subtasks must be >= 2 "
            f"(got {agents.decomposition.max_subtasks})"
        )

    # Security: extra_args must not weaken the sandbox/permissions.
    for pid, provider in agents.providers.items():
        _check_extra_args(pid, provider.extra_args, issues)

    _validate_checks(config, issues, warnings)
    _validate_telegram(config, issues)
    _validate_supervisor(config, issues)

    if issues:
        raise ConfigError(issues)
    return warnings


def _validate_supervisor(config: OrchestratorConfig, issues: list[str]) -> None:
    """The supervisor layer is validated under the same ceiling as a flow node (P2.1).

    ``permission_profile`` is forced ``read-only`` in code (the layer never writes), ``reasoning``
    is already allowlisted by the loader; here we enforce that ``role_file`` contains no path
    traversal (``..`` or an absolute path) — the same containment rule the flow validator applies to
    a node ``role_file``.
    """
    role_file = config.supervisor.role_file
    parts = role_file.replace("\\", "/").split("/")
    if ".." in parts or role_file.startswith("/"):
        issues.append(f"supervisor.role_file {role_file!r} contains path traversal")


def _validate_telegram(config: OrchestratorConfig, issues: list[str]) -> None:
    telegram = config.telegram
    if telegram.ask_timeout_s <= 0:
        issues.append(f"telegram.ask_timeout_s must be > 0 (got {telegram.ask_timeout_s})")
    for field, value in (
        ("bot_token_env", telegram.bot_token_env),
        ("chat_id_env", telegram.chat_id_env),
    ):
        if not _ENV_NAME_RE.fullmatch(value):
            issues.append(
                f"telegram.{field} must be a valid environment variable name (got {value!r})"
            )


def _validate_checks(config: OrchestratorConfig, issues: list[str], warnings: list[str]) -> None:
    """Validate configured check commands and the discovery block (automatic check discovery).

    Each command must be a launchable argv with no shell metacharacters, no sandbox-weakening
    flag, and must not be a denied command (e.g. ``git commit``). The same predicates run at
    discovery time on candidates (defense in depth, mirroring ``find_forbidden_args``).
    """
    checks = config.checks
    if checks.timeout_seconds <= 0:
        issues.append(f"checks.timeout_seconds must be > 0 (got {checks.timeout_seconds})")

    denied = config.security.denied_commands
    for index, raw in enumerate(checks.commands):
        where = f"checks.commands[{index}]"
        if isinstance(raw, str) and not raw.strip():
            continue  # a blank legacy string is a tolerated no-op (§4.8)
        try:
            check = normalize_check_command(raw)
        except CheckCommandError as exc:
            issues.append(f"{where}: {exc}")
            continue
        bad = shell_metachars(check.argv)
        if bad is not None:
            issues.append(f"{where}: argv token {bad!r} contains a shell metacharacter")
        for reason in find_forbidden_args(check.argv):
            issues.append(f"{where}: {reason}")
        matched = argv_matches_denied(check.argv, denied)
        if matched is not None:
            issues.append(f"{where}: matches denied command {matched!r}")

    discovery = checks.discovery
    if discovery.timeout_seconds <= 0:
        issues.append(
            f"checks.discovery.timeout_seconds must be > 0 (got {discovery.timeout_seconds})"
        )
    if discovery.provider is not None:
        allowed = frozenset(config.agents.allowed)
        provider_ids = frozenset(config.agents.providers)
        if discovery.provider not in allowed:
            issues.append(
                f"checks.discovery.provider: {discovery.provider.value!r} is not in agents.allowed"
            )
        if discovery.provider not in provider_ids:
            issues.append(
                f"checks.discovery.provider: {discovery.provider.value!r} "
                "has no agents.providers entry"
            )
    if discovery.mode is CheckDiscoveryMode.DISABLED:
        warnings.append(
            "checks.discovery.mode is 'disabled': the quality gate is OFF — no checks will run"
        )
