"""Stop-hook docs-sync gate: the branch-aware notion of "documentation".

The repository has two documentation shapes (see BRANCHING_MODEL.md / .agents/rules/git-workflow.md
§A): ``main``/``release`` carry the derived ``docs/`` tree, ``dev`` does not. The gate therefore
decides what counts as a docs change from the presence of a marker file, and ``_should_block`` is
pure, so both shapes are testable without a real branch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "docs_sync_gate.py"


def _load_gate() -> ModuleType:
    """Import the hook by path — ``.claude`` is not an importable package name."""
    spec = importlib.util.spec_from_file_location("docs_sync_gate", _HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate() -> ModuleType:
    return _load_gate()


# --- scope detection ------------------------------------------------------------------------------


def test_doc_prefixes_on_main_are_the_derived_tree(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "docs" / "worc_architecture.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(gate, "_DERIVED_DOCS_MARKER", marker)

    assert gate._doc_prefixes() == ("docs/", ".agents/")


def test_doc_prefixes_on_dev_swap_in_the_rules_and_packaged_guide(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = tmp_path / "docs" / "worc_architecture.md"  # no derived tree -> dev shape
    monkeypatch.setattr(gate, "_DERIVED_DOCS_MARKER", absent)

    assert gate._doc_prefixes() == (
        ".agents/",
        "docs/backlog/",
        "src/wastech_orchestrator/packaged/",
    )


# --- main scope: the derived docs tree ------------------------------------------------------------

_MAIN = ("docs/", ".agents/")


def test_main_scope_blocks_code_only_change(gate: ModuleType) -> None:
    assert gate._should_block(["src/wastech_orchestrator/cli.py"], _MAIN) is True


def test_main_scope_accepts_a_derived_doc(gate: ModuleType) -> None:
    paths = ["src/wastech_orchestrator/cli.py", "docs/operations.md"]

    assert gate._should_block(paths, _MAIN) is False


# --- dev scope: no derived docs tree --------------------------------------------------------------

_DEV = (".agents/", "docs/backlog/", "src/wastech_orchestrator/packaged/")


def test_dev_scope_blocks_code_only_change(gate: ModuleType) -> None:
    assert gate._should_block(["src/wastech_orchestrator/cli.py"], _DEV) is True


def test_dev_scope_is_satisfied_by_the_packaged_operator_guide(gate: ModuleType) -> None:
    paths = ["src/wastech_orchestrator/cli.py", "src/wastech_orchestrator/packaged/guide/README.md"]

    assert gate._should_block(paths, _DEV) is False


def test_dev_scope_is_satisfied_by_the_backlog_and_the_rules(gate: ModuleType) -> None:
    assert gate._should_block(["src/a.py", "docs/backlog/adr-x.md"], _DEV) is False
    assert gate._should_block(["src/a.py", ".agents/rules/architecture.md"], _DEV) is False


def test_dev_scope_rejects_a_recreated_derived_doc(gate: ModuleType) -> None:
    # docs/configuration.md must not exist on dev, so writing it does not satisfy the gate.
    assert gate._should_block(["src/a.py", "docs/configuration.md"], _DEV) is True


# --- shape-independent behaviour ------------------------------------------------------------------


@pytest.mark.parametrize("prefixes", [_MAIN, _DEV])
def test_readme_satisfies_the_gate_on_either_branch(
    gate: ModuleType, prefixes: tuple[str, ...]
) -> None:
    assert gate._should_block(["src/a.py", "README.md"], prefixes) is False


@pytest.mark.parametrize("prefixes", [_MAIN, _DEV])
def test_a_change_set_without_src_never_blocks(gate: ModuleType, prefixes: tuple[str, ...]) -> None:
    assert gate._should_block(["tests/test_a.py", "pyproject.toml"], prefixes) is False
