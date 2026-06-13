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
