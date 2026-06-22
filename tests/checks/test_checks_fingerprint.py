"""Discovery-input fingerprint stability and invalidation (automatic check discovery)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from wastech_orchestrator.checks.fingerprint import compute_fingerprint


def test_fingerprint_is_stable(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"pyproject.toml": "a"})
    assert compute_fingerprint(root) == compute_fingerprint(root)


def test_fingerprint_changes_on_manifest_edit(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"pyproject.toml": "a"})
    before = compute_fingerprint(root)
    (root / "pyproject.toml").write_text("b", encoding="utf-8")
    assert compute_fingerprint(root) != before


def test_fingerprint_changes_when_lockfile_added(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"pyproject.toml": "a"})
    before = compute_fingerprint(root)
    (root / "uv.lock").write_text("", encoding="utf-8")
    assert compute_fingerprint(root) != before


def test_fingerprint_changes_when_venv_appears(make_repo: Callable[..., Path]) -> None:
    root = make_repo({"pyproject.toml": "a"})
    before = compute_fingerprint(root)
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("", encoding="utf-8")
    assert compute_fingerprint(root) != before
