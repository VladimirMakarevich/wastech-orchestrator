"""Configuration validator — the fail-closed gate.

Enforces every semantic rule so an unsafe or contradictory config never reaches the
pipeline. This is the config-time half of the "security cannot be weakened" invariant
``extra_args`` that would disable the sandbox/approvals are
rejected here.

All problems are collected and raised together via the typed :class:`ConfigError` from the loader.
"""

from __future__ import annotations

import re

from wastech_orchestrator.checks.model import (
    CheckCommandError,
    argv_matches_denied,
    is_safe_relpath,
    normalize_check_command,
    shell_metachars,
)
from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.providers.capabilities import is_reasoning_supported, reasoning_levels_for
from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_extra_args(pid: ProviderId, args: tuple[str, ...], issues: list[str]) -> None:
    """Reject any extra_args flag that disables the sandbox/permissions.

    Delegates the detection to the shared
    :func:`~wastech_orchestrator.security.forbidden_args.find_forbidden_args` (also used at run time
    by the provider command builders) and frames each finding as a config issue.
    """
    where = f"agents.providers.{pid.value}.extra_args"
    issues.extend(f"{where}: {reason}" for reason in find_forbidden_args(args))


def _check_sandbox_field(pid: ProviderId, sandbox: str | None, issues: list[str]) -> None:
    """Reject a legacy ``sandbox: read-only|workspace-write``.

    A Codex node's isolation is now the generated permission profile selected by
    ``permission_profile``; the ``sandbox`` field survives only as the ``danger-full-access`` escape
    (still gated by ``strict_isolation`` at preflight). A safe legacy value is rejected rather
    than silently ignored — ``upgrade-config`` folds it into ``permission_profile``.
    """
    if sandbox is None or sandbox == FORBIDDEN_SANDBOX_VALUE:
        return
    where = f"agents.providers.{pid.value}.sandbox"
    issues.append(
        f"{where}: {sandbox!r} is a legacy setting; set permission_profile: {sandbox!r} instead "
        f"(only {FORBIDDEN_SANDBOX_VALUE!r} remains valid here, and only under "
        "strict_isolation: false). Run `wastech-orchestrator upgrade-config` to migrate."
    )


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


def _global_primary(config: OrchestratorConfig) -> ProviderId | None:
    primaries = [pid for pid, p in config.agents.providers.items() if p.primary]
    return primaries[0] if len(primaries) == 1 else None


# Recognized model-name prefixes per vendor, for the advisory model↔provider warning ONLY. A
# deliberately conservative heuristic: an unrecognized model yields no vendor (no false positive),
# matching the codebase's stance that model ids are otherwise passed through unverified.
_MODEL_VENDOR_PREFIXES: tuple[tuple[ProviderId, tuple[str, ...]], ...] = (
    (ProviderId.CLAUDE, ("claude", "sonnet", "opus", "haiku", "fable")),
    (ProviderId.CODEX, ("gpt", "o1", "o3", "o4", "codex")),
)


def _infer_model_vendor(model: str | None) -> ProviderId | None:
    """Best-effort vendor of a model id from its name prefix; ``None`` when unrecognized."""
    if not model:
        return None
    lowered = model.strip().lower()
    for vendor, prefixes in _MODEL_VENDOR_PREFIXES:
        if lowered.startswith(prefixes):
            return vendor
    return None


def _check_reasoning(
    *,
    where: str,
    provider: ProviderId,
    reasoning: str | None,
    issues: list[str],
) -> None:
    if reasoning is None or is_reasoning_supported(provider, reasoning):
        return
    issues.append(
        f"{where}: invalid value {reasoning!r} for provider {provider.value!r}, "
        f"expected one of {sorted(reasoning_levels_for(provider))}"
    )


def validate_config(config: OrchestratorConfig) -> list[str]:
    """Validate a parsed config . Raises :class:`ConfigError` on any violation.

    Returns a list of non-fatal warnings (empty in v1 — every validator finding is a hard error).
    """
    issues: list[str] = []
    warnings: list[str] = []
    agents = config.agents
    allowed = frozenset(agents.allowed)

    # Provider routing: exactly one global primary, in agents.allowed (PRE.1 — node-based routing).
    _check_global_primary(config, allowed, issues)

    # Watch poll interval: negative is meaningless; 0 means single-pass (no loop).
    if config.orchestrator.poll_interval_seconds < 0:
        issues.append(
            "orchestrator.poll_interval_seconds must be >= 0 "
            f"(got {config.orchestrator.poll_interval_seconds})"
        )

    # Queue selector: the equality filter compares against a task's `queue`, which is itself a
    # non-empty string; an empty/whitespace selector could never match and is rejected.
    if not config.orchestrator.queue.strip():
        issues.append("orchestrator.queue must be a non-empty string")

    # Loop-control hard cap: the global cap must be >= a single fix loop.
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

    # Transient-retry policy: counts and delays cannot be negative (0 attempts = disable retry), and
    # a per-retry delay cap below the base delay would silently clamp every backoff to the base.
    retry = agents.retry
    if retry.max_attempts < 0:
        issues.append(f"agents.retry.max_attempts must be >= 0 (got {retry.max_attempts})")
    if retry.base_delay_s < 0:
        issues.append(f"agents.retry.base_delay_s must be >= 0 (got {retry.base_delay_s})")
    if retry.max_delay_s < 0:
        issues.append(f"agents.retry.max_delay_s must be >= 0 (got {retry.max_delay_s})")
    if retry.max_delay_s < retry.base_delay_s:
        issues.append(
            "agents.retry.max_delay_s must be >= agents.retry.base_delay_s "
            f"({retry.max_delay_s} < {retry.base_delay_s})"
        )
    if retry.max_blocked_s < 0:
        issues.append(f"agents.retry.max_blocked_s must be >= 0 (got {retry.max_blocked_s})")

    # Security: extra_args must not weaken the sandbox/permissions.
    for pid, provider in agents.providers.items():
        _check_reasoning(
            where=f"agents.providers.{pid.value}.reasoning",
            provider=pid,
            reasoning=provider.reasoning,
            issues=issues,
        )
        _check_extra_args(pid, provider.extra_args, issues)
        _check_sandbox_field(pid, provider.sandbox, issues)

    _validate_checks(config, issues, warnings)
    _validate_telegram(config, issues)
    _validate_confirmation_gates(config, issues)
    _validate_supervisor(config, issues, warnings)
    _validate_skills(config, warnings)
    _validate_security(config, issues)
    _validate_paths(config, issues)

    if issues:
        raise ConfigError(issues)
    return warnings


def _validate_paths(config: OrchestratorConfig, issues: list[str]) -> None:
    """The task lifecycle directory must live inside the repo working tree. The git audit commit
    stages files under ``<tasks_dir>/<state>/<id>.md`` and relies on git tracking them, so the value
    must be repo-relative (no absolute path, no ``~``, no ``..`` traversal). It must also not live
    under the gitignored ``.worc/`` home — that would silently drop the audit trail from git."""
    tasks_dir = config.paths.tasks_dir
    where = "paths.tasks_dir"
    if not tasks_dir.strip():
        issues.append(f"{where}: must be a non-empty repo-relative directory")
        return
    if not is_safe_relpath(tasks_dir):
        issues.append(
            f"{where} {tasks_dir!r} must be a repo-relative directory "
            "(no absolute path, no '~', no '..' traversal)"
        )
        return
    normalized = tasks_dir.replace("\\", "/").strip().strip("/")
    if normalized == ".worc" or normalized.startswith(".worc/"):
        issues.append(
            f"{where} {tasks_dir!r} must not live under the gitignored '.worc/' home "
            "(the task lifecycle would be excluded from the git audit trail)"
        )


#: Named once so the warning below and the early return it guards cannot drift apart.
_INERT_SUPERVISOR_KEYS = "role_file / provider / observe / finalize / handoff"


def _validate_skills(config: OrchestratorConfig, warnings: list[str]) -> None:
    """A dynamic skill map needs the layer that proposes it.

    A warning rather than a fatal issue, because the dynamic layer is fail-open by design (a
    proposal naming a skill that does not resolve is filtered out, never an error): "each node
    gets only the
    skills pinned on it in the flow" is a correct degradation, not a lost guarantee. It is silent,
    though, which is what earns the warning.

    Worded about the **resolved** value on purpose: a ``skills:`` block written without a
    ``dynamic:`` key resolves to true (the loader's default for a present block), so this can
    fire for
    an operator who never typed the word.
    """
    if config.skills.dynamic and not config.supervisor.enabled:
        warnings.append(
            "skills.dynamic resolves to true but supervisor.enabled is false — the once-per-task "
            "node→skills proposal is a supervisor turn, so each node gets only the skills pinned "
            "on it in the flow YAML"
        )


def _validate_supervisor(
    config: OrchestratorConfig, issues: list[str], warnings: list[str]
) -> None:
    """The supervisor layer is validated under the same ceiling as a flow node.

    ``permission_profile`` is forced ``read-only`` in code (the layer never writes). ``provider``
    (when set) must be in ``agents.allowed`` and ``reasoning`` must be supported by the resolved
    provider (the explicit ``supervisor.provider``, or the global primary when absent) — symmetric
    with flow-node validation. Model is passed through unverified, as everywhere. We also enforce
    that ``role_file`` has no path traversal (``..`` or an absolute path) — the flow validator's
    containment rule for a node ``role_file``.

    One ``provider`` serves the whole layer, but model and effort are per phase, so every check
    below runs once per phase block (``observe`` / ``finalize`` / ``handoff``) against that one
    resolved provider.

    When ``provider`` is unset, each phase's model is sent to the inherited global primary. A model
    whose vendor plainly clashes with that primary (a ``claude-*`` model under a ``codex``
    primary, say) 400s on every supervisor turn at runtime, silently masked by the cross-provider
    fallback. We can't verify a model in general, but a recognized cross-vendor prefix is a strong
    signal — surfaced as a WARNING, not fatal (the run still degrades via fallback; "fatal only when
    no runtime fallback"). The check fires only on the inherited path; an explicit provider is the
    operator's call and is validated for ``agents.allowed`` membership above.

    With the layer switched off none of these values is ever read: no provider is resolved, no model
    is sent, and no role file is opened, so refusing a config over a model that will never be called
    would only punish an operator who left the block behind. It becomes one warning instead. Nothing
    is actually lost by that — ``read_role_file`` enforces flow-dir containment unconditionally at
    read time and the router re-checks ``agents.allowed`` at route resolution; these are the early,
    friendly copies of both.
    """
    supervisor = config.supervisor
    if not supervisor.enabled:
        warnings.append(
            "supervisor.enabled: false — the rest of the supervisor block "
            f"({_INERT_SUPERVISOR_KEYS}) is inert and is not validated"
        )
        return
    provider = supervisor.provider
    if provider is not None and provider not in frozenset(config.agents.allowed):
        issues.append(f"supervisor.provider {provider.value!r} is not in agents.allowed")
    resolved = provider or _global_primary(config)
    phases = (
        ("observe", supervisor.observe.model, supervisor.observe.reasoning),
        ("finalize", supervisor.finalize.model, supervisor.finalize.reasoning),
        ("handoff", supervisor.handoff.model, supervisor.handoff.reasoning),
    )
    if resolved is not None:
        for phase, model, reasoning in phases:
            _check_reasoning(
                where=f"supervisor.{phase}.reasoning",
                provider=resolved,
                reasoning=reasoning,
                issues=issues,
            )
            if provider is not None:
                continue
            vendor = _infer_model_vendor(model)
            if vendor is not None and vendor != resolved:
                warnings.append(
                    f"supervisor.{phase}.model {model!r} looks like a {vendor.value} "
                    f"model, but supervisor.provider is unset so it inherits the global primary "
                    f"{resolved.value!r}; this will fail at runtime and fall back. Pin "
                    f"supervisor.provider to {vendor.value!r} or set a {resolved.value} model."
                )
    role_file = config.supervisor.role_file
    parts = role_file.replace("\\", "/").split("/")
    if ".." in parts or role_file.startswith("/"):
        issues.append(f"supervisor.role_file {role_file!r} contains path traversal")


def _validate_security(config: OrchestratorConfig, issues: list[str]) -> None:
    """The protected-paths allowlist holds repo-relative globs (it matches against repo-relative
    diff paths). Reject an absolute path, ``~``, or ``..`` traversal — the same containment rule
    applied to a check command's ``cwd``."""
    for index, pattern in enumerate(config.security.protected_paths):
        if not is_safe_relpath(pattern):
            issues.append(
                f"security.protected_paths[{index}] {pattern!r} must be a "
                "repo-relative glob (no absolute path, no '~', no '..' traversal)"
            )


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


def _validate_confirmation_gates(config: OrchestratorConfig, issues: list[str]) -> None:
    """An enabled operator-confirmation gate requires a Telegram transport.

    Both gates resolve to STOP on silence; an enabled gate with no transport could never reach the
    operator and would be a silently-failing safety control, so it is a misconfiguration rather than
    a no-op (fail-closed at preflight).
    """
    if config.telegram.enabled:
        return
    if config.orchestrator.auto_mode.confirm_next_task:
        issues.append("orchestrator.auto_mode.confirm_next_task requires telegram.enabled: true")
    for pid, provider in config.agents.providers.items():
        if provider.max_turns_gate:
            issues.append(
                f"agents.providers.{pid.value}.max_turns_gate requires telegram.enabled: true"
            )


def _validate_checks(config: OrchestratorConfig, issues: list[str], warnings: list[str]) -> None:
    """Validate the operator's ``checks.command_sets``.

    Each command must be a launchable argv with no shell metacharacters, no sandbox-weakening flag,
    no path-traversal ``cwd``, and must not be a denied command (e.g. ``git commit``). An empty
    ``command_sets`` is a valid no-gate config (every task passes the checks node).
    """
    checks = config.checks
    if checks.timeout_seconds <= 0:
        issues.append(f"checks.timeout_seconds must be > 0 (got {checks.timeout_seconds})")

    denied = config.security.denied_commands
    for name, cset in checks.command_sets.items():
        base = f"checks.command_sets.{name}"
        if not name.strip():
            issues.append("checks.command_sets: a set name must be a non-empty string")
        if cset.timeout_seconds is not None and cset.timeout_seconds <= 0:
            issues.append(f"{base}.timeout_seconds must be > 0 (got {cset.timeout_seconds})")
        if not cset.commands:
            issues.append(f"{base}.commands must list at least one command")
        for pi, raw_path in enumerate(cset.paths):
            if not raw_path.strip():
                issues.append(f"{base}.paths[{pi}] must be a non-empty string")
        for index, spec in enumerate(cset.commands):
            where = f"{base}.commands[{index}]"
            try:
                check = normalize_check_command(spec)
            except CheckCommandError as exc:
                issues.append(f"{where}: {exc}")
                continue
            bad = shell_metachars(check.argv)
            if bad is not None:
                issues.append(f"{where}: argv token {bad!r} contains a shell metacharacter")
            issues.extend(f"{where}: {reason}" for reason in find_forbidden_args(check.argv))
            matched = argv_matches_denied(check.argv, denied)
            if matched is not None:
                issues.append(f"{where}: matches denied command {matched!r}")
            if spec.cwd is not None and not is_safe_relpath(spec.cwd):
                issues.append(
                    f"{where}.cwd {spec.cwd!r} must be a repo-relative path "
                    "(no absolute path, no '..' traversal)"
                )
