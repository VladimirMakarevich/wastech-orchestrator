"""Config generation DoD: selected-only providers, exactly one global primary, the shipped security
posture, absolute-path YAML round-trip (Windows/macOS), and a clean load+validate of the result.

The generated file is deliberately small — it carries what the install resolved plus a few
affordances, and omits every key it would only have written at the loader's own default. Two tests
carry that contract: ``test_generated_config_writes_only_the_blocks_it_must`` pins the shape, and
``test_omitted_blocks_resolve_to_what_install_used_to_write`` is the regression net that makes the
omission safe — it asserts the *resolved* value of every key that used to be spelled out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import AuditBranch, BranchMode, MergeStrategy, ObserveMode
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.install import config_writer
from wastech_orchestrator.install.config_writer import InstallSpec, build_and_validate
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security.env import default_allowed_environment


def _spec(
    tmp_path: Path,
    providers: tuple[ProviderId, ...],
    *,
    create_pr: bool = True,
    auto_mode: bool = False,
) -> InstallSpec:
    return InstallSpec(
        repo_url="git@github.com:me/my-repo.git",
        repo_local_path=tmp_path / "my-repo",
        base_branch="main",
        providers=providers,
        create_pull_request=create_pr,
        auto_mode=auto_mode,
    )


def test_codex_only_marks_codex_primary(tmp_path: Path) -> None:
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.agents.allowed == (ProviderId.CODEX,)
    assert set(cfg.agents.providers) == {ProviderId.CODEX}
    assert cfg.agents.providers[ProviderId.CODEX].primary is True


def test_claude_only_marks_claude_primary(tmp_path: Path) -> None:
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CLAUDE,)))).config
    assert cfg.agents.allowed == (ProviderId.CLAUDE,)
    assert set(cfg.agents.providers) == {ProviderId.CLAUDE}
    assert ProviderId.CODEX not in cfg.agents.providers
    assert cfg.agents.providers[ProviderId.CLAUDE].primary is True


def test_both_mark_exactly_claude_as_primary(tmp_path: Path) -> None:
    cfg = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX, ProviderId.CLAUDE)))
    ).config
    assert set(cfg.agents.allowed) == {ProviderId.CODEX, ProviderId.CLAUDE}
    # Exactly one global primary; Claude is preferred when both are selected.
    primaries = [pid for pid, p in cfg.agents.providers.items() if p.primary]
    assert primaries == [ProviderId.CLAUDE]


def test_generated_config_writes_only_the_blocks_it_must(tmp_path: Path) -> None:
    # The delivered file is what the operator has to DECIDE, not a transcript of the schema. Only
    # three kinds of key survive: what the install resolved (repo, providers, git, auto_mode), what
    # deviates from the fail-closed default (the security posture), and three affordances
    # (`paths.tasks_dir`, the empty `checks.command_sets` gate, the `telegram.enabled` switch).
    text = build_and_validate(_spec(tmp_path, (ProviderId.CLAUDE, ProviderId.CODEX)))
    data = yaml.safe_load(text)
    assert set(data) == {
        "schema_version",
        "orchestrator",
        "repo",
        "paths",
        "agents",
        "security",
        "checks",
        "git",
        "telegram",
    }
    # Nothing inside those blocks is spelled out at its default either.
    assert set(data["orchestrator"]) == {"auto_mode"}
    assert set(data["orchestrator"]["auto_mode"]) == {"enabled"}
    assert set(data["repo"]) == {"url", "local_path", "base_branch"}
    assert set(data["agents"]) == {"allowed", "providers"}
    assert set(data["agents"]["providers"]["claude"]) == {"model", "reasoning", "primary"}
    assert set(data["agents"]["providers"]["codex"]) == {"model", "reasoning"}
    assert set(data["security"]) == {
        "strict_isolation",
        "disable_read_isolation",
        "allow_git_evidence",
    }
    assert set(data["checks"]) == {"command_sets"}
    assert set(data["git"]) == {"create_pull_request", "pr_base"}
    assert set(data["telegram"]) == {"enabled"}
    # The operator is told what is missing and where to read about it, rather than left to wonder.
    assert "config.example.yaml" in text
    assert "Not written here" in text


def test_omitted_blocks_resolve_to_what_install_used_to_write(tmp_path: Path) -> None:
    # The regression net for the omission: every key the generator dropped must still RESOLVE to the
    # value it used to write. A default that drifts away from this list silently changes what a
    # fresh install does, which is exactly the failure mode dropping the keys could hide.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CLAUDE,)))).config

    assert cfg.orchestrator.poll_interval_seconds == 300
    assert cfg.orchestrator.queue == "default"
    assert cfg.orchestrator.auto_mode.confirm_next_task is False

    assert cfg.repo.branch_prefix == "worc"
    assert cfg.repo.branch_mode is BranchMode.NEW

    assert (cfg.agents.max_stage_attempts, cfg.agents.max_fix_cycles) == (3, 15)
    assert cfg.agents.max_total_fix_iterations == 30
    assert (cfg.agents.decomposition.enabled, cfg.agents.decomposition.max_subtasks) == (False, 8)
    retry = cfg.agents.retry
    assert (retry.max_attempts, retry.base_delay_s) == (2, 2.0)
    assert (retry.max_delay_s, retry.max_blocked_s) == (30.0, 21600.0)

    claude = cfg.agents.providers[ProviderId.CLAUDE]
    assert claude.command == "claude"
    assert claude.timeout_seconds == 7200
    assert claude.permission_profile == "workspace-write"
    assert claude.extra_args == ()
    assert claude.max_turns == 400
    assert claude.max_turns_gate is False

    assert cfg.security.allowed_environment == default_allowed_environment()
    assert cfg.security.extra_environment == {}
    assert cfg.security.denied_read_paths == (".env", "secrets/**")
    assert cfg.security.denied_commands == (
        "git commit",
        "git push",
        "gh pr create",
        "gh pr merge",
    )
    assert cfg.security.trust_level == "auto"
    assert cfg.security.protected_paths == ()

    assert cfg.validation.max_task_bytes == 262144
    assert cfg.validation.max_task_lines == 5000
    assert cfg.validation.max_line_bytes == 8192
    assert cfg.validation.max_control_ratio == 0.01

    assert cfg.checks.timeout_seconds == 7200

    assert cfg.git.auto_merge is False
    assert cfg.git.auto_merge_strategy is MergeStrategy.SQUASH
    assert cfg.git.auto_merge_wait_for_checks is False
    assert cfg.git.merge_flow == "merge"
    assert cfg.git.footprint.audit_on_branch is AuditBranch.TASK
    assert "{task_id}" in cfg.git.footprint.audit_commit_message

    assert cfg.telegram.bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert cfg.telegram.chat_id_env == "TELEGRAM_CHAT_ID"
    assert cfg.telegram.ask_timeout_s == 28800
    assert cfg.telegram.trace is False

    assert cfg.supervisor.enabled is True
    assert cfg.supervisor.role_file == "roles/supervisor.md"
    assert cfg.supervisor.observe.mode is ObserveMode.EVENTS
    # The one non-default among them, so it moved into the dataclass rather than being written out:
    # observation is advisory and can fire on every step of a deep fix loop, so it stays cheap.
    assert cfg.supervisor.observe.reasoning == "low"

    assert (cfg.logging.level, cfg.logging.artifacts) == ("warning", "standard")
    assert cfg.logging.clean_runs_on_success is True
    assert cfg.tools.default_timeout_seconds == 3600
    assert cfg.prompt_audit is False
    assert validate_config(cfg) == []


def test_generated_config_keeps_the_repo_and_task_affordances(tmp_path: Path) -> None:
    spec = _spec(tmp_path, (ProviderId.CODEX,))
    cfg = loads_config(build_and_validate(spec)).config
    assert cfg.repo.local_path == str(tmp_path / "my-repo")
    # Written at its default on purpose: the operator has to know where task files go, and a repo
    # that already uses `tasks/` renames it here.
    assert cfg.paths.tasks_dir == "tasks"
    # Rejected tasks land under .worc/ so they are never swept into the audit commit. The path is
    # left at the repo-root-relative default rather than frozen absolute at install time.
    assert cfg.validation.quarantine_folder == "./.worc/tasks/rejected"
    assert validate_config(cfg) == []


def test_generated_config_is_stamped_with_schema_version(tmp_path: Path) -> None:
    from wastech_orchestrator.config.schema import CONFIG_SCHEMA_VERSION

    text = build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))
    assert f"schema_version: {CONFIG_SCHEMA_VERSION}" in text


@pytest.mark.parametrize(
    "providers",
    [(ProviderId.CODEX,), (ProviderId.CLAUDE,), (ProviderId.CLAUDE, ProviderId.CODEX)],
)
def test_generated_config_ships_memory_off(
    tmp_path: Path, providers: tuple[ProviderId, ...]
) -> None:
    # The memory subsystem is experimental (unaudited store, no redaction guarantee), so NO install
    # ever turns it on — whatever the provider selection. The block is not written at all: its
    # absence IS "off", and a key that only ever says `false` is noise in a small config.
    text = build_and_validate(_spec(tmp_path, providers))
    assert loads_config(text).config.memory.enabled is False
    assert "memory" not in yaml.safe_load(text)


def test_shipped_security_posture_is_written(tmp_path: Path) -> None:
    text = build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))
    cfg = loads_config(text).config
    # Advanced mode out of the box, written key by key so the relaxation is visible in the
    # operator's own file. This is the one block that CANNOT be dropped: the dataclass/loader
    # fallback stays fail-closed, so a config that omits these keys is strict, not relaxed.
    assert cfg.security.strict_isolation is False
    assert "strict_isolation: false" in text
    assert cfg.security.disable_read_isolation is True
    assert "disable_read_isolation: true" in text
    # The git-evidence grant is seeded ON: inert beside the advanced mode above, and the node's
    # capability the moment the operator sets strict_isolation: true.
    assert cfg.security.allow_git_evidence is True
    assert "allow_git_evidence: true" in text


def test_os_launch_essentials_are_allowlisted_for_the_install_host(tmp_path: Path) -> None:
    # The allowlist is resolved by the loader per OS instead of being frozen at install time, so it
    # carries the host's launch essentials wherever the config is read (on Windows that includes
    # SystemRoot, without which claude.exe crashes) — and a config copied between machines adapts.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.security.allowed_environment == default_allowed_environment()
    assert "USER" in cfg.security.allowed_environment  # macOS Keychain login for subscription CLIs


def test_explicit_model_and_reasoning_defaults_are_written(tmp_path: Path) -> None:
    text = build_and_validate(_spec(tmp_path, (ProviderId.CODEX, ProviderId.CLAUDE)))
    cfg = loads_config(text).config
    claude = cfg.agents.providers[ProviderId.CLAUDE]
    codex = cfg.agents.providers[ProviderId.CODEX]
    # provider-config-cleanup #3: fresh installs ship explicit model/reasoning, not "" / null —
    # the loader's fallback for both is empty, so these two are genuine decisions, not defaults.
    assert (claude.model, claude.reasoning) == ("claude-sonnet-5", "high")
    assert (codex.model, codex.reasoning) == ("gpt-5.4", "high")
    assert "max_budget_usd" not in text


def test_create_pr_and_auto_mode_are_reflected(tmp_path: Path) -> None:
    cfg = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX,), create_pr=False, auto_mode=True))
    ).config
    assert cfg.git.create_pull_request is False
    assert cfg.orchestrator.auto_mode.enabled is True


def test_install_seeds_empty_command_sets(tmp_path: Path) -> None:
    # `install` seeds no commands: it writes an empty gate, and the operator authors it.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.checks.command_sets == {}


def test_supervisor_follows_the_global_primary(tmp_path: Path) -> None:
    # The layer is not pinned in the file any more: `provider` and the per-phase `model` stay unset,
    # which means "the global primary" and "that provider's model" — so a Codex-primary install
    # runs its supervisor on Codex's model by construction, and flipping the primary keeps them
    # aligned instead of leaving a stale claude model pinned beside a codex primary.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.supervisor.provider is None
    assert cfg.agents.providers[ProviderId.CODEX].primary is True
    for phase in (cfg.supervisor.observe, cfg.supervisor.finalize, cfg.supervisor.handoff):
        assert phase.model is None
    # Effort still differs by phase: the advisory notes are cheap, the producers' tier is not.
    assert cfg.supervisor.observe.reasoning == "low"
    assert cfg.supervisor.finalize.reasoning is None  # => the provider's own `high`
    assert cfg.agents.providers[ProviderId.CODEX].reasoning == "high"


@pytest.mark.parametrize(
    "path",
    [
        r"C:\projects\my-repo",
        r"C:\Users\tom\new-repo-orchestrator",  # \U \t \n would all corrupt if double-quoted
        "/Users/vlad/projects/my-repo-orchestrator",
    ],
)
def test_absolute_path_strings_survive_yaml_roundtrip(path: str) -> None:
    text = config_writer.render({"repo": {"local_path": path}})
    assert yaml.safe_load(text)["repo"]["local_path"] == path
