"""Binding registry DoD: env-override dir, bind/lookup/unbind, path normalization, atomic JSON,
corrupt-file tolerance (backlog: interactive installer)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wastech_orchestrator.install import registry


@pytest.fixture(autouse=True)
def _registry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the registry into a temp dir so tests never touch the real user config."""
    home = tmp_path / "registry-home"
    monkeypatch.setenv(registry.HOME_ENV, str(home))
    return home


def test_env_override_locates_registry(_registry_home: Path) -> None:
    assert registry.registry_dir() == _registry_home
    assert registry.registry_path() == _registry_home / "registry.json"


def test_bind_then_lookup_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    config = root.parent / "repo-orchestrator" / "config.yaml"

    assert registry.lookup(root) is None
    registry.bind(root, config)
    assert registry.lookup(root) == str(config.resolve())


def test_unbind_removes_only_that_binding(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    registry.bind(a, tmp_path / "a-ws" / "config.yaml")
    registry.bind(b, tmp_path / "b-ws" / "config.yaml")

    registry.unbind(a)
    assert registry.lookup(a) is None
    assert registry.lookup(b) is not None
    registry.unbind(a)  # idempotent — no error when already absent


def test_keys_are_normalized_to_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    registry.bind(".", tmp_path / "repo-orchestrator" / "config.yaml")
    # A relative key and a trailing-separator variant both resolve to the same absolute root.
    assert registry.lookup(root) is not None
    assert registry.lookup(Path(str(root) + os.sep)) is not None


def test_written_file_is_versioned_json(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    registry.bind(root, tmp_path / "repo-orchestrator" / "config.yaml")

    data = json.loads(registry.registry_path().read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert isinstance(data["bindings"], dict)
    assert str(root.resolve()) in data["bindings"]


def test_corrupt_file_is_tolerated_and_overwritten(tmp_path: Path) -> None:
    registry.registry_path().parent.mkdir(parents=True, exist_ok=True)
    registry.registry_path().write_text("}{ not json", encoding="utf-8")

    assert registry.lookup(tmp_path) is None  # malformed file reads as empty
    root = tmp_path / "repo"
    root.mkdir()
    registry.bind(root, tmp_path / "repo-orchestrator" / "config.yaml")  # recovers
    assert registry.lookup(root) is not None
