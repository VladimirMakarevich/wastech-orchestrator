from pathlib import Path

import pytest
import tools.mdlint as mdlint


def build_cli(root: Path) -> Path:
    """Create the CLI entry point a linter checkout at ``root`` would expose once built."""
    entry = root / mdlint.CLI_ENTRY
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("", encoding="utf-8")
    return entry


def test_env_var_checkout_wins_over_a_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = build_cli(tmp_path / "configured")
    build_cli(tmp_path / "repos" / "wastech-mdlint")
    monkeypatch.setenv(mdlint.HOME_ENV_VAR, str(tmp_path / "configured"))

    assert mdlint.find_cli(tmp_path / "repos" / "orchestrator") == configured


def test_a_sibling_checkout_is_used_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sibling = build_cli(tmp_path / "wastech-mdlint")
    monkeypatch.delenv(mdlint.HOME_ENV_VAR, raising=False)

    assert mdlint.find_cli(tmp_path / "orchestrator") == sibling


def test_a_configured_but_unbuilt_checkout_resolves_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env var pointing at a checkout that was never built must not fall through to a sibling.

    Silently linting with a different copy than the one asked for is how two machines end up
    reporting different findings for the same commit.
    """
    (tmp_path / "configured").mkdir()
    build_cli(tmp_path / "wastech-mdlint")
    monkeypatch.setenv(mdlint.HOME_ENV_VAR, str(tmp_path / "configured"))

    assert mdlint.find_cli(tmp_path / "orchestrator") is None


def test_only_overlays_present_in_the_checkout_are_run(tmp_path: Path) -> None:
    """Presence of an additive config is the branch signal, so absence must not be an error."""
    assert mdlint.configs_to_run(tmp_path) == [mdlint.BASE_CONFIG]

    for overlay in mdlint.OVERLAY_CONFIGS:
        (tmp_path / overlay).write_text("{}", encoding="utf-8")
    assert mdlint.configs_to_run(tmp_path) == [mdlint.BASE_CONFIG, *mdlint.OVERLAY_CONFIGS]


def test_this_repository_ships_the_shared_config_the_wrapper_expects() -> None:
    """The wrapper's base config name and the tracked file are one decision in two places."""
    assert (mdlint.repo_root() / mdlint.BASE_CONFIG).is_file()


def test_a_missing_linter_skips_locally_and_fails_under_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mdlint.shutil, "which", lambda _: None)
    monkeypatch.delenv("CI", raising=False)
    assert mdlint.main([]) == 0

    monkeypatch.setenv("CI", "true")
    assert mdlint.main([]) == 2


def test_every_config_runs_and_the_worst_exit_code_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = build_cli(tmp_path / "linter")
    monkeypatch.setattr(mdlint.shutil, "which", lambda _: "node")
    monkeypatch.setattr(mdlint, "find_cli", lambda _root: cli)
    monkeypatch.setattr(mdlint, "configs_to_run", lambda _root: ["base.json", "overlay.json"])

    ran: list[str] = []

    def record(_node: str, _cli: Path, _root: Path, config: str, _extra: list[str]) -> int:
        ran.append(config)
        return 1 if config == "overlay.json" else 0

    monkeypatch.setattr(mdlint, "run_pass", record)

    assert mdlint.main([]) == 1
    assert ran == ["base.json", "overlay.json"]
