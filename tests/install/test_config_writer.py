"""Config generation DoD: selected-only providers, exactly one global primary, safe defaults,
absolute-path YAML round-trip (Windows/macOS), and a clean load+validate of the generated config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import AuditBranch, ObserveMode
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


def test_generated_config_uses_worc_home_and_audit_trail(tmp_path: Path) -> None:
    spec = _spec(tmp_path, (ProviderId.CODEX,))
    cfg = loads_config(build_and_validate(spec)).config
    # The task + summary are audit-committed in the repo; the quarantine lives under .worc/ so
    # rejected tasks are never swept into that commit.
    assert cfg.git.footprint.audit_on_branch is AuditBranch.TASK
    expected_quarantine = tmp_path / "my-repo" / ".worc" / "tasks" / "rejected"
    assert cfg.validation.quarantine_folder == str(expected_quarantine)
    assert cfg.repo.local_path == str(tmp_path / "my-repo")
    assert cfg.orchestrator.poll_interval_seconds == 300
    # The generated config carries the default queue selector and task lifecycle dir, and still
    # validates clean.
    assert cfg.orchestrator.queue == "default"
    assert cfg.paths.tasks_dir == "tasks"
    assert validate_config(cfg) == []


def test_generated_config_is_stamped_with_schema_version(tmp_path: Path) -> None:
    from wastech_orchestrator.config.schema import CONFIG_SCHEMA_VERSION

    text = build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))
    assert f"schema_version: {CONFIG_SCHEMA_VERSION}" in text


def test_generated_config_includes_logging_defaults(tmp_path: Path) -> None:
    # A fresh install ships the quiet level. The dataclass/loader fallback stays "info" — this is
    # an install-time posture, not a change to what an omitted key means.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.logging.level == "warning"
    assert cfg.logging.artifacts == "standard"


def test_generated_config_ships_memory_off(tmp_path: Path) -> None:
    # A fresh install ships the memory subsystem off, and writes the key rather than relying on
    # the absent-block fallback: the operator finds the switch in their own file.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.memory.enabled is False
    assert "enabled: false" in build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))


def test_shipped_security_posture_is_written(tmp_path: Path) -> None:
    text = build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))
    cfg = loads_config(text).config
    # Advanced mode out of the box, written key by key so the relaxation is visible in the
    # operator's own file. The dataclass/loader fallback stays fail-closed for a config that omits
    # the key — only what `install` writes is the relaxed posture.
    assert cfg.security.strict_isolation is False
    assert "strict_isolation: false" in text
    assert cfg.security.disable_read_isolation is True
    assert "disable_read_isolation: true" in text
    # USER must be allowlisted so macOS subscription CLIs can reach their Keychain credentials.
    assert "USER" in cfg.security.allowed_environment
    # The git-evidence grant is seeded ON, and seeded explicitly rather than left to the schema
    # default: it is inert beside the advanced mode above and becomes the node's capability the
    # moment the operator sets strict_isolation: true.
    assert cfg.security.allow_git_evidence is True
    assert "allow_git_evidence: true" in text
    assert "git push" in cfg.security.denied_commands
    # Assigned variables are seeded empty and, for the same reason as the grant above, explicitly:
    # a fresh install shows the key exists rather than leaving it to be discovered in the reference.
    assert cfg.security.extra_environment == {}
    assert "extra_environment" in build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))
    assert "gh pr create" in cfg.security.denied_commands
    assert "gh pr merge" in cfg.security.denied_commands
    assert cfg.agents.providers[ProviderId.CODEX].permission_profile == "workspace-write"
    assert cfg.agents.providers[ProviderId.CODEX].extra_args == ()


def test_os_launch_essentials_are_allowlisted_for_the_install_host(tmp_path: Path) -> None:
    # The installer writes the host OS's launch essentials so a fresh install starts the agent CLIs
    # out of the box (on Windows that includes SystemRoot, without which claude.exe crashes). It
    # writes only the host OS's set — the other OS's names are never dragged in.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert set(default_allowed_environment()) <= set(cfg.security.allowed_environment)


def test_explicit_model_and_reasoning_defaults_are_written(tmp_path: Path) -> None:
    text = build_and_validate(_spec(tmp_path, (ProviderId.CODEX, ProviderId.CLAUDE)))
    cfg = loads_config(text).config
    claude = cfg.agents.providers[ProviderId.CLAUDE]
    codex = cfg.agents.providers[ProviderId.CODEX]
    # provider-config-cleanup #3: fresh installs ship explicit model/reasoning, not "" / null.
    assert (claude.model, claude.reasoning) == ("claude-sonnet-5", "high")
    assert (codex.model, codex.reasoning) == ("gpt-5.4", "high")
    # provider-config-cleanup #2: the unused max_budget_usd field is gone from the generated config.
    assert "max_budget_usd" not in text
    assert not hasattr(claude, "max_budget_usd")
    assert not hasattr(codex, "max_budget_usd")


def test_create_pr_and_auto_mode_are_reflected(tmp_path: Path) -> None:
    cfg = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX,), create_pr=False, auto_mode=True))
    ).config
    assert cfg.git.create_pull_request is False
    assert cfg.orchestrator.auto_mode.enabled is True


def test_install_seeds_empty_command_sets(tmp_path: Path) -> None:
    # `init` no longer seeds commands (v15): it writes an empty gate; the operator authors it.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.checks.command_sets == {}


def test_generated_config_includes_optional_sections(tmp_path: Path) -> None:
    text = build_and_validate(_spec(tmp_path, (ProviderId.CLAUDE,)))
    cfg = loads_config(text).config
    assert cfg.supervisor.role_file == "roles/supervisor.md"
    # The whole-layer switch is written explicitly at its default, so an operator finds it in their
    # own config rather than only in the reference.
    assert cfg.supervisor.enabled is True
    assert "enabled: true" in text.split("supervisor:", 1)[1].split("logging:", 1)[0]
    # Each delivered supervisor phase carries a concrete, visible model/reasoning (the global
    # primary's model + a non-max reasoning), not an implicit inherit-from-primary null.
    for phase in (cfg.supervisor.observe, cfg.supervisor.finalize, cfg.supervisor.handoff):
        assert phase.model == "claude-sonnet-5"
        assert phase.reasoning not in ("xhigh", "max")  # default must not be fragile
    # Effort is split by what the phase costs: advisory notes can fire on every step of a fix loop,
    # while finalize writes the PR body and handoff briefs the next subtask.
    assert cfg.supervisor.observe.reasoning == "low"
    assert cfg.supervisor.finalize.reasoning == "high"
    assert cfg.supervisor.handoff.reasoning == "high"
    # Cadence is delivered explicitly at the global default: deviations only, never every step.
    assert cfg.supervisor.observe.mode is ObserveMode.EVENTS
    # Provider is pinned to the primary so it stays aligned with the models (also the primary's).
    assert cfg.supervisor.provider == ProviderId.CLAUDE
    # The dynamic skill layer is off out of the box (opt-in).
    assert cfg.skills.dynamic is False
    assert cfg.skills.strict is False
    # The documented telegram.trace knob is present in the delivered config.
    assert cfg.telegram.trace is False
    assert "trace:" in text
    assert cfg.prompt_audit is False
    # Fresh install ships trust_level: auto (routine in-repo deletions no longer gate) + no floor.
    assert cfg.security.trust_level == "auto"
    assert cfg.security.protected_paths == ()
    for key in ("supervisor:", "skills:", "prompt_audit:", "trust_level:", "protected_paths:"):
        assert key in text


def test_supervisor_model_tracks_the_global_primary(tmp_path: Path) -> None:
    # A Codex-primary install resolves the supervisor model to Codex's model, not Claude's.
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    for phase in (cfg.supervisor.observe, cfg.supervisor.finalize, cfg.supervisor.handoff):
        assert phase.model == "gpt-5.4"
    assert cfg.supervisor.finalize.reasoning == "high"
    # A codex-primary install pins supervisor.provider to codex, aligned with the codex model.
    assert cfg.supervisor.provider == ProviderId.CODEX


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
