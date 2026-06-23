"""End-to-end deterministic resolution (automatic check discovery)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.checks.conftest import make_process_result
from wastech_orchestrator.checks.model import CheckSource
from wastech_orchestrator.checks.resolver import CheckResolver
from wastech_orchestrator.config.schema import OrchestratorConfig


def _resolver(config: OrchestratorConfig, root: Path, art: Path, **kw: object) -> CheckResolver:
    return CheckResolver(config, repo_root=root, artifacts_root=art, **kw)  # type: ignore[arg-type]


def test_configured_mode_uses_commands_as_is(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo()
    config = make_checks_config(local_path=str(root), mode="configured", commands=["mytool --flag"])
    profile = _resolver(config, root, tmp_path / "art").resolve()
    assert profile.ready is True
    assert profile.source is CheckSource.CONFIGURED
    assert profile.checks[0].argv == ("mytool", "--flag")


def test_deterministic_picks_venv_pytest_script(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo(
        {"pyproject.toml": "[project]\ndependencies=['pytest']\n"},
        venv=".venv",
        venv_tools=("pytest",),
    )
    config = make_checks_config(local_path=str(root), mode="deterministic")
    profile = _resolver(config, root, tmp_path / "art").resolve()
    assert profile.ready is True
    tests = [c.argv for c in profile.checks if c.name == "tests"]
    assert (".venv/bin/pytest",) in tests


def test_venv_python_dash_m_chosen_when_bare_pytest_missing(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    # No pytest script in the venv (only the interpreter); bare `pytest` is not on PATH.
    root = make_repo({"pyproject.toml": "[project]\ndependencies=['pytest']\n"}, venv=".venv")
    config = make_checks_config(local_path=str(root), mode="deterministic")
    resolver = _resolver(
        config,
        root,
        tmp_path / "art",
        which=lambda _name: None,  # nothing on PATH (the incident)
        run_process=lambda *a, **k: make_process_result(exit_code=0),  # `import pytest` succeeds
    )
    profile = resolver.resolve()
    assert profile.ready is True
    tests = [c.argv for c in profile.checks if c.name == "tests"]
    assert tests == [(".venv/bin/python", "-m", "pytest")]


def test_nothing_launchable_is_not_ready(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo({"pyproject.toml": "[project]\ndependencies=['pytest']\n"})
    config = make_checks_config(local_path=str(root), mode="deterministic")
    resolver = _resolver(config, root, tmp_path / "art", which=lambda _name: None)
    profile = resolver.resolve()
    assert profile.ready is False
    assert profile.checks == ()


def test_wrapper_supersedes_language_checks(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo(
        {
            "Makefile": "check:\n\tpytest\n",
            "pyproject.toml": "[project]\ndependencies=['pytest']\n",
        },
        venv=".venv",
        venv_tools=("pytest",),
    )
    config = make_checks_config(local_path=str(root), mode="deterministic")
    resolver = _resolver(config, root, tmp_path / "art", which=lambda name: "/usr/bin/make")
    profile = resolver.resolve()
    assert [c.argv for c in profile.checks] == [("make", "check")]


def test_auto_agent_fallback_when_deterministic_finds_no_tests(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    from wastech_orchestrator.checks.model import CheckCandidate

    root = make_repo()  # empty repo: deterministic detection finds no tests candidate
    config = make_checks_config(local_path=str(root), mode="auto")

    class _FakeDiscovery:
        def discover(self, repo_root: Path, evidence: object) -> tuple[CheckCandidate, ...]:
            return (CheckCandidate(name="tests", argv=("pytest",), source=CheckSource.AGENT),)

    resolver = _resolver(
        config,
        root,
        tmp_path / "art",
        which=lambda _name: "/usr/bin/pytest",  # the agent's candidate probes launchable
        discovery=_FakeDiscovery(),
    )
    profile = resolver.resolve(allow_agent=True)
    assert profile.ready is True
    assert profile.source is CheckSource.AGENT
    assert [c.argv for c in profile.checks] == [("pytest",)]


def test_cache_invalidated_after_manifest_change(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo({"go.mod": "module x\n"})
    config = make_checks_config(local_path=str(root), mode="deterministic")
    resolver = _resolver(config, root, tmp_path / "art", which=lambda name: "/usr/bin/go")
    first = resolver.resolve()
    assert [c.argv for c in first.checks] == [("go", "test", "./...")]

    # Switch ecosystems: the fingerprint changes, so resolution refreshes.
    (root / "go.mod").unlink()
    (root / "Cargo.toml").write_text("", encoding="utf-8")
    second = resolver.resolve()
    assert [c.argv for c in second.checks] == [("cargo", "test")]


def test_reresolve_stamps_reason_note(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    from wastech_orchestrator.checks.resolver import ReResolveReason

    root = make_repo({"go.mod": "module x\n"})
    config = make_checks_config(local_path=str(root), mode="deterministic")
    resolver = _resolver(config, root, tmp_path / "art", which=lambda _n: "/usr/bin/go")
    profile = resolver.reresolve(allow_agent=False, reason=ReResolveReason.LAUNCH_FAILED)
    assert any("re-resolved: launch_failed" in n for n in profile.notes)
    # The profile carries a stable signature over its selected commands.
    assert profile.commands_signature


def test_configured_pin_not_replaced_by_detection_when_unlaunchable(
    make_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    # auto mode: a configured `{name: types, argv: [mypy, src]}` pins the `types` slot. The bare
    # `mypy` is not on PATH, so the pin does not probe launchable — and detection's launchable
    # `.venv/bin/mypy` must NOT silently fill it. `tests` still resolves from detection.
    root = make_repo(
        {"pyproject.toml": "[project]\ndependencies=['pytest','mypy']\n"},
        venv=".venv",
        venv_tools=("pytest", "mypy"),
    )
    text = f"""
repo:
  local_path: {str(root)!r}
agents:
  allowed: [claude]
  providers:
    claude:
      command: "claude"
security:
  allowed_environment: [PATH]
  denied_commands: []
checks:
  discovery:
    mode: auto
  commands:
    - {{name: types, argv: [mypy, src]}}
"""
    from wastech_orchestrator.config.loader import loads_config

    config = loads_config(text).config
    # Nothing is on PATH, so the bare configured `mypy src` is NOT launchable; venv scripts (probed
    # by file existence) are.
    resolver = _resolver(config, root, tmp_path / "art", which=lambda _n: None)
    profile = resolver.resolve()
    names = {c.name for c in profile.checks}
    assert "tests" in names  # detection filled the test slot
    assert "types" not in names  # the configured pin was unlaunchable and was NOT replaced


def test_cache_invalidated_when_commands_change(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo()
    config1 = make_checks_config(local_path=str(root), mode="configured", commands=["ruff check ."])
    art = tmp_path / "art"
    first = _resolver(config1, root, art, which=lambda name: f"/usr/bin/{name}").resolve()
    assert [c.argv for c in first.checks] == [("ruff", "check", ".")]

    # Same repo, but commands changed — combined fingerprint differs, cache must be invalidated.
    config2 = make_checks_config(local_path=str(root), mode="configured", commands=["pytest"])
    second = _resolver(config2, root, art, which=lambda name: f"/usr/bin/{name}").resolve()
    assert [c.argv for c in second.checks] == [("pytest",)]


def test_cache_invalidated_when_mode_changes(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo({"go.mod": "module x\n"})
    art = tmp_path / "art"

    # First resolve: configured mode with an explicit command.
    config1 = make_checks_config(local_path=str(root), mode="configured", commands=["mycheck"])
    first = _resolver(config1, root, art, which=lambda name: f"/usr/bin/{name}").resolve()
    assert first.checks[0].argv == ("mycheck",)

    # Switch to deterministic — mode change must bust the cache and re-detect from go.mod.
    config2 = make_checks_config(local_path=str(root), mode="deterministic")
    second = _resolver(config2, root, art, which=lambda name: f"/usr/bin/{name}").resolve()
    assert any(c.argv == ("go", "test", "./...") for c in second.checks)
