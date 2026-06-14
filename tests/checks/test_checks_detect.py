"""Evidence parsing + deterministic candidate detection per ecosystem (check discovery §5)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from wastech_orchestrator.checks.detect import CheckCandidateDetector
from wastech_orchestrator.checks.inspect import RepositoryInspector


def _candidates(root: Path) -> dict[str, list[tuple[str, ...]]]:
    evidence = RepositoryInspector(root).collect()
    out: dict[str, list[tuple[str, ...]]] = {}
    for candidate in CheckCandidateDetector().detect(evidence):
        out.setdefault(candidate.name, []).append(candidate.argv)
    return out


def test_uv_lock_proposes_uv_run(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {
            "uv.lock": "",
            "pyproject.toml": "[project]\noptional-dependencies = {dev = ['pytest','ruff']}\n",
        }
    )
    cand = _candidates(root)
    assert ("uv", "run", "pytest") in cand["tests"]
    assert ("uv", "run", "ruff", "check", ".") in cand["lint"]


def test_poetry_lock_proposes_poetry_run(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {"poetry.lock": "", "pyproject.toml": "[project]\ndependencies = ['pytest']\n"}
    )
    assert ("poetry", "run", "pytest") in _candidates(root)["tests"]


def test_pnpm_lock_proposes_pnpm(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {"pnpm-lock.yaml": "", "package.json": '{"scripts": {"test": "vitest", "lint": "eslint"}}'}
    )
    cand = _candidates(root)
    assert ("pnpm", "test") in cand["tests"]
    assert ("pnpm", "run", "lint") in cand["lint"]


def test_package_lock_proposes_npm(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"package-lock.json": "", "package.json": '{"scripts": {"test": "jest"}}'})
    assert ("npm", "test") in _candidates(root)["tests"]


def test_package_json_without_test_script_proposes_nothing(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"package.json": '{"scripts": {"build": "tsc"}}'})
    assert "tests" not in _candidates(root)


def test_cargo_and_go(make_repo: Callable[..., Path]) -> None:
    assert ("cargo", "test") in _candidates(make_repo({"Cargo.toml": ""}))["tests"]
    assert ("go", "test", "./...") in _candidates(make_repo({"go.mod": "module x\n"}))["tests"]


def test_makefile_check_target_is_a_wrapper(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"Makefile": "check:\n\tpytest\n\nbuild:\n\tcc\n"})
    assert ("make", "check") in _candidates(root)["checks"]


def test_tox_and_nox_wrappers(make_repo: Callable[..., Path]) -> None:
    assert ("tox",) in _candidates(make_repo({"tox.ini": "[tox]\n"}))["checks"]
    assert ("nox",) in _candidates(make_repo({"noxfile.py": ""}))["checks"]


def test_posix_venv_prefers_bin_scripts(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {"pyproject.toml": "[project]\ndependencies=['pytest','ruff','mypy']\n"},
        venv=".venv",
        venv_tools=("pytest", "ruff", "mypy"),
    )
    cand = _candidates(root)
    assert (".venv/bin/pytest",) in cand["tests"]
    assert (".venv/bin/ruff", "check", ".") in cand["lint"]
    assert (".venv/bin/mypy", ".") in cand["types"]


def test_posix_venv_without_pytest_script_falls_back_to_python_dash_m(
    make_repo: Callable[..., Path],
) -> None:
    root = make_repo({"pyproject.toml": "[project]\ndependencies=['pytest']\n"}, venv=".venv")
    assert (".venv/bin/python", "-m", "pytest") in _candidates(root)["tests"]


def test_windows_venv_scripts(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {"pyproject.toml": "[project]\ndependencies=['pytest']\n"},
        venv=".venv",
        venv_tools=("pytest",),
        windows_venv=True,
    )
    assert (".venv/Scripts/pytest.exe",) in _candidates(root)["tests"]


def test_plain_pyproject_pytest_is_low_confidence_candidate(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"pyproject.toml": "[project]\ndependencies=['pytest']\n"})
    assert ("pytest",) in _candidates(root)["tests"]


# --- configured tool scope (§1.1): an explicit `.` overrides [tool.mypy]/[tool.ruff] scope --------

_MYPY_FILES = "[tool.mypy]\nfiles = ['src']\n"
_MYPY_EXCLUDE = "[tool.mypy]\nexclude = ['^tests/']\n"
_RUFF_SCOPE = "[tool.ruff]\nsrc = ['src']\n"


def test_configured_mypy_files_scope_replaces_dot(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {"pyproject.toml": "[project]\ndependencies=['mypy']\n" + _MYPY_FILES},
        venv=".venv",
        venv_tools=("mypy",),
    )
    cand = _candidates(root)
    assert (".venv/bin/mypy", "src") in cand["types"]
    assert (".venv/bin/mypy", ".") not in cand["types"]


def test_configured_mypy_exclude_only_emits_bare_mypy(make_repo: Callable[..., Path]) -> None:
    # `exclude` is a regex (not a pathspec): it proves a scope is configured, so we emit a bare
    # `mypy` that reads pyproject — never `mypy .` (which would re-include the excluded paths).
    root = make_repo({"pyproject.toml": "[project]\ndependencies=['mypy']\n" + _MYPY_EXCLUDE})
    cand = _candidates(root)
    assert ("mypy",) in cand["types"]
    assert ("mypy", ".") not in cand["types"]


def test_configured_ruff_scope_drops_dot(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"pyproject.toml": "[project]\ndependencies=['ruff']\n" + _RUFF_SCOPE})
    cand = _candidates(root)
    assert ("ruff", "check") in cand["lint"]
    assert ("ruff", "check", ".") not in cand["lint"]


def test_no_configured_scope_keeps_dot(make_repo: Callable[..., Path]) -> None:
    # Backward-compatible: with no [tool.*] scope, the historical `.` target is preserved.
    root = make_repo({"pyproject.toml": "[project]\ndependencies=['ruff','mypy']\n"})
    cand = _candidates(root)
    assert ("ruff", "check", ".") in cand["lint"]
    assert ("mypy", ".") in cand["types"]


def test_uv_run_honors_mypy_scope(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {"uv.lock": "", "pyproject.toml": "[project]\ndependencies=['mypy']\n" + _MYPY_FILES}
    )
    assert ("uv", "run", "mypy", "src") in _candidates(root)["types"]


def test_unsafe_mypy_files_entry_is_rejected(make_repo: Callable[..., Path]) -> None:
    # An absolute / traversal path in [tool.mypy] files must never reach argv; it drops to a bare
    # `mypy` (scope still configured) rather than being passed through (reject-don't-sanitize, §9).
    pyproject = "[project]\ndependencies=['mypy']\n[tool.mypy]\nfiles = ['/etc', '../evil']\n"
    root = make_repo({"pyproject.toml": pyproject})
    evidence = RepositoryInspector(root).collect()
    assert evidence.mypy_files == ()
    assert evidence.mypy_has_scope is True
    cand = _candidates(root)
    assert ("mypy",) in cand["types"]


def test_inspect_exposes_scope_fields(make_repo: Callable[..., Path]) -> None:
    root = make_repo(
        {"pyproject.toml": "[project]\ndependencies=['mypy','ruff']\n" + _MYPY_FILES + _RUFF_SCOPE}
    )
    evidence = RepositoryInspector(root).collect()
    assert evidence.mypy_files == ("src",)
    assert evidence.mypy_has_scope is True
    assert evidence.ruff_has_scope is True


def test_inspect_unparseable_pyproject_has_no_scope(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"pyproject.toml": "this is not = valid toml ]["})
    evidence = RepositoryInspector(root).collect()
    assert evidence.mypy_files == () and not evidence.mypy_has_scope and not evidence.ruff_has_scope
